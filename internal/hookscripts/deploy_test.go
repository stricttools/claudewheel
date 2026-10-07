package hookscripts

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/testkit"
)

func TestDeployScriptsWritesEveryScriptExecutable(t *testing.T) {
	ws := testkit.Workspace(t)
	results, err := DeployScripts(testkit.FX(), Names(), ws.ScriptsDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != len(Names()) {
		t.Fatalf("%d results", len(results))
	}
	for _, r := range results {
		if r.Action != Created {
			t.Errorf("%s: %s", r.Name, r.Action)
		}
		info, err := os.Stat(r.Path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o755 {
			t.Errorf("%s: mode %v", r.Name, info.Mode())
		}
		want, _ := Script(r.Name)
		if got := testkit.ReadFile(t, r.Path); got != want {
			t.Errorf("%s: content differs from the registry", r.Name)
		}
	}
}

func TestDeployScriptsLeavesAnExistingScriptUnlessForced(t *testing.T) {
	ws := testkit.Workspace(t)
	path := filepath.Join(ws.ScriptsDir(), "hook-timestamp")
	testkit.WriteFile(t, path, "edited\n")
	results, err := DeployScripts(testkit.FX(), []string{"hook-timestamp"}, ws.ScriptsDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	if results[0].Action != Exists || testkit.ReadFile(t, path) != "edited\n" {
		t.Fatalf("action %s, content %q", results[0].Action, testkit.ReadFile(t, path))
	}
	results, err = DeployScripts(testkit.FX(), []string{"hook-timestamp"}, ws.ScriptsDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := Script("hook-timestamp")
	if results[0].Action != Overwritten || testkit.ReadFile(t, path) != want {
		t.Fatalf("forced: action %s", results[0].Action)
	}
}

func TestDeployScriptsChecksEveryNameBeforeWriting(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := DeployScripts(testkit.FX(), []string{"hook-timestamp", "hook-unknown"}, ws.ScriptsDir(), false); err == nil {
		t.Fatal("an unknown name was accepted")
	}
	if _, err := os.Stat(filepath.Join(ws.ScriptsDir(), "hook-timestamp")); !os.IsNotExist(err) {
		t.Fatalf("a script was written before the unknown name was refused: %v", err)
	}
}

func TestEveryScriptIsValidBash(t *testing.T) {
	for _, name := range Names() {
		text, err := Script(name)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasPrefix(text, "#!/usr/bin/env bash\n") {
			t.Errorf("%s does not start with the bash shebang", name)
		}
		cmd := exec.Command("bash", "-n")
		cmd.Stdin = strings.NewReader(text)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Errorf("%s: bash -n: %v\n%s", name, err, out)
		}
	}
}

func TestMissingScripts(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := DeployScripts(testkit.FX(), []string{"hook-timestamp"}, ws.ScriptsDir(), false); err != nil {
		t.Fatal(err)
	}
	missing, err := MissingScripts([]string{"hook-timestamp", "hook-session-end", "not-a-script"}, ws.ScriptsDir())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(missing, ",") != "hook-session-end" {
		t.Fatalf("missing %v", missing)
	}
}

func TestLinkPathCommands(t *testing.T) {
	ws := testkit.Workspace(t)
	names := []string{"heavy", "hook-timestamp"}
	results, err := LinkPathCommands(testkit.FX(), names, ws.ScriptsDir(), ws.BinDir(), false)
	if err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(ws.BinDir(), "heavy")
	if len(results) != 1 || results[0].Action != Linked {
		t.Fatalf("results %+v", results)
	}
	if dest, _ := os.Readlink(link); dest != filepath.Join(ws.ScriptsDir(), "heavy") {
		t.Fatalf("link points at %q", dest)
	}
	results, _ = LinkPathCommands(testkit.FX(), names, ws.ScriptsDir(), ws.BinDir(), false)
	if results[0].Action != Exists {
		t.Fatalf("second run: %s", results[0].Action)
	}
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, link, "someone else's heavy\n")
	results, _ = LinkPathCommands(testkit.FX(), names, ws.ScriptsDir(), ws.BinDir(), false)
	if results[0].Action != Foreign || testkit.ReadFile(t, link) != "someone else's heavy\n" {
		t.Fatalf("foreign file: %s", results[0].Action)
	}
	results, err = LinkPathCommands(testkit.FX(), names, ws.ScriptsDir(), ws.BinDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	if results[0].Action != Relinked {
		t.Fatalf("forced: %s", results[0].Action)
	}
	if dest, _ := os.Readlink(link); dest != filepath.Join(ws.ScriptsDir(), "heavy") {
		t.Fatalf("relinked to %q", dest)
	}
	entries, _ := os.ReadDir(ws.BinDir())
	if len(entries) != 1 {
		t.Fatalf("staged link left behind: %v", entries)
	}
}

func TestCheckDeployed(t *testing.T) {
	ws := testkit.Workspace(t)
	if _, err := DeployScripts(testkit.FX(), []string{"hook-timestamp", "heavy"}, ws.ScriptsDir(), false); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, filepath.Join(ws.ScriptsDir(), "heavy"), "drifted\n")
	states, err := CheckDeployed(ws.ScriptsDir())
	if err != nil {
		t.Fatal(err)
	}
	byName := map[string]DeployedState{}
	for _, s := range states {
		byName[s.Name] = s.State
	}
	if byName["hook-timestamp"] != DeployedCurrent || byName["heavy"] != DeployedDiffers || byName["hook-session-end"] != DeployedAbsent {
		t.Fatalf("states %v", byName)
	}
}

func TestServiceUnit(t *testing.T) {
	unit, err := ServiceUnit("/opt/my tools/claudewheel")
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range []string{
		"ExecStart=\"/opt/my tools/claudewheel\" probe run-service\n",
		"SuccessExitStatus=143\n",
		"Restart=on-failure\n",
		"WantedBy=default.target\n",
	} {
		if !strings.Contains(unit, line) {
			t.Errorf("unit lacks %q:\n%s", line, unit)
		}
	}
	if strings.Contains(unit, "Environment") {
		t.Error("the unit sets an environment")
	}
	for _, bad := range []string{"relative/claudewheel", "/a\"b", "/a\\b", "/a%b", "/a$b", "/a\nb"} {
		if _, err := ServiceUnit(bad); err == nil {
			t.Errorf("ServiceUnit(%q) succeeded", bad)
		}
	}
}

func TestDeployServiceWritesEnablesAndRestarts(t *testing.T) {
	ws := testkit.Workspace(t)
	stubs := testkit.StubPath(t)
	testkit.Stub(t, stubs, "systemctl", "exit 0")
	path, action, err := DeployService(testkit.FX(), ws.SystemdUserDir(), "/opt/claudewheel", false)
	if err != nil {
		t.Fatal(err)
	}
	if action != Created || path != filepath.Join(ws.SystemdUserDir(), probe.ServiceName) {
		t.Fatalf("action %s path %s", action, path)
	}
	want := []string{"--user daemon-reload", "--user enable " + probe.ServiceName, "--user restart " + probe.ServiceName}
	if got := testkit.Calls(t, stubs, "systemctl"); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("systemctl calls %q", got)
	}
}

func TestDeployServiceLeavesAnExistingUnitButStartsIt(t *testing.T) {
	ws := testkit.Workspace(t)
	stubs := testkit.StubPath(t)
	testkit.Stub(t, stubs, "systemctl", "exit 0")
	path := filepath.Join(ws.SystemdUserDir(), probe.ServiceName)
	testkit.WriteFile(t, path, "custom\n")
	_, action, err := DeployService(testkit.FX(), ws.SystemdUserDir(), "/opt/claudewheel", false)
	if err != nil {
		t.Fatal(err)
	}
	if action != Exists || testkit.ReadFile(t, path) != "custom\n" {
		t.Fatalf("action %s", action)
	}
	if got := testkit.Calls(t, stubs, "systemctl"); strings.Join(got, "|") != "--user enable --now "+probe.ServiceName {
		t.Fatalf("systemctl calls %q", got)
	}
	_, action, err = DeployService(testkit.FX(), ws.SystemdUserDir(), "/opt/claudewheel", true)
	if err != nil || action != Overwritten {
		t.Fatalf("forced: %s %v", action, err)
	}
}

func TestDeployServiceFailsOnAFailingSystemctl(t *testing.T) {
	ws := testkit.Workspace(t)
	stubs := testkit.StubPath(t)
	testkit.Stub(t, stubs, "systemctl", "echo broken >&2; exit 1")
	if _, _, err := DeployService(testkit.FX(), ws.SystemdUserDir(), "/opt/claudewheel", false); err == nil {
		t.Fatal("a failing systemctl was not an error")
	}
}
