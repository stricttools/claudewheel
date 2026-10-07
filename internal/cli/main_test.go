package cli

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/testkit"
)

// childEnvKey makes the test binary run the claudewheel app on its own
// arguments instead of the tests, so a test can run a command that replaces
// its process (a launch) or asks for consent through App.Run.
const childEnvKey = "CLAUDEWHEEL_CLI_TEST_CHILD"

func TestMain(m *testing.M) {
	if os.Getenv(childEnvKey) == "1" {
		NewApp("0.0.0-test").Run()
		return
	}
	os.Exit(m.Run())
}

// runChild runs claudewheel args in a child process with dir as its working
// directory and stdin from /dev/null, returning its combined output and exit
// status.
func runChild(t *testing.T, dir string, args ...string) (string, int) {
	t.Helper()
	cmd := exec.Command(os.Args[0], args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), childEnvKey+"=1")
	out, err := cmd.CombinedOutput()
	code := 0
	if exitErr, ok := err.(*exec.ExitError); ok {
		code = exitErr.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	return string(out), code
}

func TestPrintModeLaunchExecsTheClientInItsSystemdScope(t *testing.T) {
	e := newEnv(t)
	home := filepath.Dir(e.ws.Root())
	testkit.Stub(t, e.stubs, "npm", `echo '["2.1.281"]'`)
	testkit.Stub(t, e.stubs, "gh", "exit 1")
	testkit.Stub(t, e.stubs, "systemd-run", `printf '%s\n' "$@" > "$HOME/argv"; env > "$HOME/env"`)
	e.setUp("work")
	claude := filepath.Join(home, ".local", "share", "claude", "versions", "2.1.281")
	testkit.WriteFile(t, claude, "#!/bin/sh\n")
	if err := os.Chmod(claude, 0o755); err != nil {
		t.Fatal(err)
	}
	project := filepath.Join(home, "proj")
	if err := os.MkdirAll(project, 0o755); err != nil {
		t.Fatal(err)
	}
	out, code := runChild(t, project, "launch", "-p", "hello", "-s", "profile=work", "-s", "version=2.1.281", "-s", "model=claude-opus-5", "--", "--extra")
	if code != 0 {
		t.Fatalf("launch exit %d:\n%s", code, out)
	}
	argv := strings.Split(strings.TrimSuffix(testkit.ReadFile(t, filepath.Join(home, "argv")), "\n"), "\n")
	sep := 0
	for i, a := range argv {
		if a == "--" {
			sep = i
			break
		}
	}
	client := argv[sep+1:]
	want := []string{claude, "--dangerously-skip-permissions", "--model", "claude-opus-5", "--disallowedTools"}
	if len(client) < len(want) || strings.Join(client[:len(want)], " ") != strings.Join(want, " ") {
		t.Fatalf("client argv %q", client)
	}
	if tail := strings.Join(client[len(client)-3:], " "); tail != "--print hello --extra" {
		t.Fatalf("client argv ends %q", tail)
	}
	if !strings.HasPrefix(strings.Join(argv[:sep], " "), "--user --scope") {
		t.Fatalf("systemd-run argv %q", argv[:sep])
	}
	env := testkit.ReadFile(t, filepath.Join(home, "env"))
	for _, line := range []string{
		"CLAUDE_CONFIG_DIR=" + filepath.Join(e.ws.ProfilesDir(), "work"),
		"CLAUDEWHEEL_LAUNCH_PROFILE=work",
		"CLAUDEWHEEL_LAUNCH_MODEL=claude-opus-5",
		"CLAUDE_CODE_SHELL_PREFIX=" + filepath.Join(e.ws.ScriptsDir(), "claudewheel-tool-scope"),
		"DISABLE_AUTOUPDATER=1",
	} {
		if !strings.Contains(env, line+"\n") {
			t.Errorf("client environment lacks %s", line)
		}
	}
	if strings.Contains(env, "CLAUDE_CODE_OAUTH_TOKEN=") {
		t.Error("a profile without a token passed an OAuth token")
	}
	settings := testkit.ReadFile(t, filepath.Join(e.ws.ProfilesDir(), "work", "settings.json"))
	if !strings.Contains(settings, "hook-block-unsafe-commands") {
		t.Error("the preflight did not reconcile the profile")
	}
}

func TestPrintModeLaunchRefusesAPresetTheSegmentDoesNotOffer(t *testing.T) {
	e := newEnv(t)
	home := filepath.Dir(e.ws.Root())
	testkit.Stub(t, e.stubs, "npm", `echo '["2.1.281"]'`)
	testkit.Stub(t, e.stubs, "gh", "exit 1")
	testkit.Stub(t, e.stubs, "systemd-run", `touch "$HOME/ran"`)
	e.setUp("work")
	for _, preset := range []string{"permissions=plan", "mcp=loose", "version=0.0.1"} {
		out, code := runChild(t, home, "launch", "-p", "hello", "-s", "profile=work", "-s", preset)
		if code == 0 {
			t.Errorf("-s %s accepted:\n%s", preset, out)
		}
	}
	if _, err := os.Stat(filepath.Join(home, "ran")); err == nil {
		t.Fatal("a refused launch ran the client")
	}
}

func TestConsequentialCommandsNeedConsentWithoutATerminal(t *testing.T) {
	e := newEnv(t)
	e.setUp("work")
	out, code := runChild(t, filepath.Dir(e.ws.Root()), "patch-profiles", "--all-profiles")
	if code == 0 || !strings.Contains(out, "stdin is not interactive") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
	if strings.Contains(testkit.ReadFile(t, filepath.Join(e.ws.ProfilesDir(), "work", "settings.json")), "hooks") {
		t.Fatal("patch-profiles wrote without consent")
	}
}
