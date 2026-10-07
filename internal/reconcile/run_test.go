package reconcile

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/hookscripts"
	"github.com/stricttools/claudewheel/internal/testkit"
	"github.com/stricttools/claudewheel/internal/workspace"
)

func profileSettings(ws workspace.Workspace, name string) string {
	return filepath.Join(ws.ProfilesDir(), name, "settings.json")
}

func setUpProfiles(t *testing.T) workspace.Workspace {
	t.Helper()
	ws := testkit.Workspace(t)
	testkit.WriteFile(t, profileSettings(ws, "alpha"), `{"model": "opus", "permissions": {"deny": ["Bash(made up:*)"]}}`)
	testkit.WriteFile(t, profileSettings(ws, "broken"), `{"permissions": {"deny": null}}`)
	testkit.WriteFile(t, profileSettings(ws, "corrupt"), `{not json`)
	if err := os.MkdirAll(filepath.Join(ws.ProfilesDir(), "nosettings", ".claudewheel"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(ws.ProfilesDir(), "notaprofile"), 0o755); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, filepath.Join(ws.ClaudeDir(), "settings.json"), `{"untouched": true}`)
	return ws
}

func TestRunAllProfiles(t *testing.T) {
	ws := setUpProfiles(t)
	broken := testkit.ReadFile(t, profileSettings(ws, "broken"))
	report, err := Run(testkit.FX(), ws, AllProfiles())
	if err != nil {
		t.Fatal(err)
	}
	labels := []string{}
	for _, tr := range report.Targets {
		labels = append(labels, tr.Label)
	}
	if strings.Join(labels, ",") != "alpha,broken,corrupt,nosettings,"+SharedSettingsLabel {
		t.Fatalf("targets %v", labels)
	}
	byLabel := map[string]TargetReport{}
	for _, tr := range report.Targets {
		byLabel[tr.Label] = tr
	}
	if !byLabel["alpha"].Changed || !byLabel["alpha"].Written {
		t.Errorf("alpha: %+v", byLabel["alpha"])
	}
	if !strings.HasPrefix(byLabel["broken"].SkipReason, "malformed") || testkit.ReadFile(t, profileSettings(ws, "broken")) != broken {
		t.Errorf("broken: %+v", byLabel["broken"])
	}
	if !strings.HasPrefix(byLabel["corrupt"].SkipReason, "unreadable") {
		t.Errorf("corrupt: %+v", byLabel["corrupt"])
	}
	if byLabel["nosettings"].SkipReason != "no settings.json" || byLabel[SharedSettingsLabel].SkipReason != "no settings.json" {
		t.Errorf("missing files: %+v %+v", byLabel["nosettings"], byLabel[SharedSettingsLabel])
	}
	if len(report.Skipped()) != 4 {
		t.Errorf("skipped %d", len(report.Skipped()))
	}
	if testkit.ReadFile(t, filepath.Join(ws.ClaudeDir(), "settings.json")) != `{"untouched": true}` {
		t.Error("~/.claude/settings.json was touched")
	}
	alpha := testkit.ReadFile(t, profileSettings(ws, "alpha"))
	if !strings.Contains(alpha, `"model": "opus"`) || strings.Contains(alpha, "made up") || !strings.Contains(alpha, "hook-block-unsafe-commands") {
		t.Errorf("alpha not canonical:\n%s", alpha)
	}
	if len(report.ScriptsDeployed) == 0 {
		t.Error("no hook script was deployed")
	}
	missing, err := hookscripts.MissingScripts(ReferencedScripts(canonicalHooksTree(ws)), ws.ScriptsDir())
	if err != nil || len(missing) != 0 {
		t.Errorf("still missing %v %v", missing, err)
	}
	lines := strings.Join(report.Lines(), "\n")
	for _, want := range []string{"alpha: reconciled", "    deny -Bash(made up:*)", "nosettings: no settings.json", "Reconciled to canonical."} {
		if !strings.Contains(lines, want) {
			t.Errorf("report lacks %q:\n%s", want, lines)
		}
	}

	again, err := Run(testkit.FX(), ws, AllProfiles())
	if err != nil {
		t.Fatal(err)
	}
	if again.ChangedAny() {
		t.Errorf("second run changed something:\n%s", strings.Join(again.Lines(), "\n"))
	}
	if !strings.Contains(strings.Join(again.Lines(), "\n"), "alpha: already canonical, no changes") {
		t.Errorf("second run report:\n%s", strings.Join(again.Lines(), "\n"))
	}
}

func TestRunOneProfileLeavesTheOthersAndSharedSettings(t *testing.T) {
	ws := setUpProfiles(t)
	testkit.WriteFile(t, profileSettings(ws, "beta"), `{}`)
	testkit.WriteFile(t, ws.SharedSettingsFile(), `{}`)
	report, err := Run(testkit.FX(), ws, OneProfile("beta"))
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Targets) != 1 || report.Targets[0].Label != "beta" || !report.Targets[0].Changed {
		t.Fatalf("targets %+v", report.Targets)
	}
	if testkit.ReadFile(t, ws.SharedSettingsFile()) != `{}` || strings.Contains(testkit.ReadFile(t, profileSettings(ws, "alpha")), "hooks") {
		t.Fatal("another target was written")
	}
}

func TestRunRefusesBadSelectionsBeforeDeployingAnything(t *testing.T) {
	ws := setUpProfiles(t)
	for name, sel := range map[string]Selection{
		"zero":    {},
		"default": OneProfile("default"),
		"unknown": OneProfile("zzz"),
	} {
		_, err := Run(testkit.FX(), ws, sel)
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		if name == "unknown" && !strings.Contains(err.Error(), "alpha, broken, corrupt, nosettings") {
			t.Errorf("unknown: the error does not list the profiles: %v", err)
		}
	}
	if _, err := os.Stat(ws.ScriptsDir()); !os.IsNotExist(err) {
		t.Fatal("a refused selection deployed scripts")
	}
}

func TestRunStopsAtAFailedWrite(t *testing.T) {
	ws := setUpProfiles(t)
	if err := os.Chmod(filepath.Join(ws.ProfilesDir(), "alpha"), 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(filepath.Join(ws.ProfilesDir(), "alpha"), 0o755) })
	if os.Geteuid() == 0 {
		t.Skip("root writes into a read-only directory")
	}
	report, err := Run(testkit.FX(), ws, AllProfiles())
	if err == nil {
		t.Fatal("a failed write was not an error")
	}
	if len(report.Targets) != 1 || report.Targets[0].Label != "alpha" {
		t.Fatalf("the run went on after the failed write: %+v", report.Targets)
	}
}

func TestMergeHooksRefusesMalformedCanonicalHooks(t *testing.T) {
	ws := testkit.Workspace(t)
	_ = ws
	existing := canonicalHooksTree(ws)
	for _, bad := range []string{`{"Stop": {}}`, `{"Stop": [1]}`, `{"Stop": [{"hooks": {}}]}`, `{"Stop": [{"hooks": [2]}]}`} {
		canonical, err := decodeObject(bad)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := MergeHooks(existing, canonical); err == nil {
			t.Errorf("MergeHooks accepted %s", bad)
		}
	}
}
