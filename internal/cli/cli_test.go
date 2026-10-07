package cli

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/testkit"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// env is one test's isolated home: its workspace and a PATH holding only
// stubs and the system directories.
type env struct {
	t     *testing.T
	ws    workspace.Workspace
	stubs string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	ws := testkit.Workspace(t)
	stubs := testkit.StubPath(t)
	testkit.Stub(t, stubs, "systemctl", "exit 0")
	return &env{t: t, ws: ws, stubs: stubs}
}

func (e *env) run(args ...string) strictcli.Result {
	e.t.Helper()
	return NewApp("0.0.0-test").Test(args)
}

func (e *env) ok(args ...string) strictcli.Result {
	e.t.Helper()
	r := e.run(args...)
	if r.ExitCode != 0 {
		e.t.Fatalf("claudewheel %s: exit %d\nstdout: %s\nstderr: %s", strings.Join(args, " "), r.ExitCode, r.Stdout, r.Stderr)
	}
	return r
}

func (e *env) fails(args ...string) strictcli.Result {
	e.t.Helper()
	r := e.run(args...)
	if r.ExitCode == 0 {
		e.t.Fatalf("claudewheel %s succeeded\nstdout: %s", strings.Join(args, " "), r.Stdout)
	}
	return r
}

// setUp creates the workspace with deploy-hooks, which opens it as every
// mutating command does, and a profile directory for each name.
func (e *env) setUp(profiles ...string) {
	e.t.Helper()
	e.ok("deploy-hooks", "--all")
	for _, name := range profiles {
		testkit.WriteFile(e.t, filepath.Join(e.ws.ProfilesDir(), name, "settings.json"), "{}\n")
	}
}

func TestEveryCommandHasHelp(t *testing.T) {
	e := newEnv(t)
	app := NewApp("0.0.0-test")
	var names []string
	for name := range app.Commands() {
		names = append(names, name)
	}
	for name := range app.Groups() {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		e.ok(name, "--help")
	}
	e.ok("--help")
	e.ok("help", "--json")
	if r := e.ok("--version"); !strings.Contains(r.Stdout, "0.0.0-test") {
		t.Fatalf("version: %q", r.Stdout)
	}
}

func TestReadOnlyCommandsRefuseAMissingWorkspace(t *testing.T) {
	e := newEnv(t)
	for _, args := range [][]string{{"health"}, {"show"}, {"stats"}, {"probe", "list"}} {
		r := e.fails(args...)
		if !strings.Contains(r.Stderr, "claudewheel launch") {
			t.Errorf("%v: %s", args, r.Stderr)
		}
	}
	if _, err := os.Stat(e.ws.Root()); !os.IsNotExist(err) {
		t.Fatal("a read-only command created the workspace")
	}
}

func TestDeployHooksSetsUpTheWorkspace(t *testing.T) {
	e := newEnv(t)
	r := e.ok("deploy-hooks", "--all")
	for _, f := range []string{e.ws.ConfigFile(), e.ws.StateFile(), e.ws.SharedSettingsFile(), filepath.Join(e.ws.ScriptsDir(), "heavy")} {
		if _, err := os.Stat(f); err != nil {
			t.Errorf("%s: %v\n%s", f, err, r.Stdout)
		}
	}
	if link, err := os.Readlink(filepath.Join(e.ws.BinDir(), "heavy")); err != nil || link != filepath.Join(e.ws.ScriptsDir(), "heavy") {
		t.Errorf("heavy link %q %v", link, err)
	}
	unit := testkit.ReadFile(t, filepath.Join(e.ws.SystemdUserDir(), "claudewheel-probe-runner.service"))
	if !strings.Contains(unit, "probe run-service") {
		t.Errorf("unit:\n%s", unit)
	}
	e.fails("deploy-hooks")
	e.fails("deploy-hooks", "hook-timestamp", "--all")
	e.fails("deploy-hooks", "no-such-script")
	e.ok("deploy-hooks", "hook-timestamp")
}

func TestShowStatsAndResetOptions(t *testing.T) {
	e := newEnv(t)
	e.setUp()
	if r := e.ok("show"); !strings.Contains(r.Stdout, "claudewheel state:") || !strings.Contains(r.Stdout, "Theme: auto") {
		t.Errorf("show:\n%s", r.Stdout)
	}
	testkit.WriteFile(t, filepath.Join(e.ws.SharedDir(), "projects", "-p", "x.jsonl"), "{}\n")
	if r := e.ok("stats"); !strings.Contains(r.Stdout, "projects") {
		t.Errorf("stats:\n%s", r.Stdout)
	}
	testkit.WriteFile(t, e.ws.OptionsFile(), "{broken")
	e.fails("show")
	e.ok("reset-options")
	e.ok("show")
}

func TestHealthExitsOneWhenACheckFails(t *testing.T) {
	e := newEnv(t)
	e.setUp("work")
	r := e.run("health")
	if r.ExitCode != 1 || !strings.Contains(r.Stdout, "hook-drift") {
		t.Fatalf("health: exit %d\n%s\n%s", r.ExitCode, r.Stdout, r.Stderr)
	}
}

func TestUpgradeWorkspaceConvertsAPythonWorkspace(t *testing.T) {
	e := newEnv(t)
	testkit.CopyTree(t, "../appconfig/testdata/python-workspace/.claudewheel", e.ws.Root())
	r := e.fails("show")
	if !strings.Contains(r.Stderr, "claudewheel upgrade-workspace") {
		t.Fatalf("show before converting: %s", r.Stderr)
	}
	r = e.ok("upgrade-workspace", "--dry-run")
	if !strings.Contains(r.Stdout, "_schema_version") {
		t.Errorf("preview:\n%s", r.Stdout)
	}
	e.fails("show")
	e.ok("upgrade-workspace")
	e.ok("show")
}

func TestPatchProfiles(t *testing.T) {
	e := newEnv(t)
	e.setUp("work", "home")
	settings := filepath.Join(e.ws.ProfilesDir(), "work", "settings.json")
	e.fails("patch-profiles", "--approve-consequential")
	r := e.ok("patch-profiles", "--all-profiles", "--dry-run")
	if !strings.Contains(r.Stdout, "would reconcile") || testkit.ReadFile(t, settings) != "{}\n" {
		t.Fatalf("dry run:\n%s", r.Stdout)
	}
	// Consent is asked by App.Run only; the binary refuses a consequential
	// command without a terminal or --approve-consequential.
	r = e.ok("patch-profiles", "--profile", "work", "--approve-consequential")
	if !strings.Contains(testkit.ReadFile(t, settings), "disallowedTools") {
		t.Fatalf("not reconciled:\n%s", r.Stdout)
	}
	e.fails("patch-profiles", "--profile", "default", "--approve-consequential")
	e.fails("patch-profiles", "--profile", "nope", "--approve-consequential")
	r = e.ok("patch-profiles", "--all-profiles", "--approve-consequential")
	r = e.ok("patch-profiles", "--all-profiles", "--dry-run")
	if !strings.Contains(r.Stdout, "Everything already canonical.") {
		t.Fatalf("second run:\n%s", r.Stdout)
	}
}

func TestPermissionCommands(t *testing.T) {
	e := newEnv(t)
	e.setUp("work", "home")
	e.ok("permission", "add", "Read(//home/**)", "--profile", "work")
	e.ok("permission", "add", "Read(//home/**)", "--all-profiles")
	r := e.ok("permission", "list", "--profile", "work", "--format", "flat")
	if strings.Count(r.Stdout, "Read(//home/**)") != 1 {
		t.Fatalf("list:\n%s", r.Stdout)
	}
	r = e.ok("permission", "list", "--profile", "home", "--format", "grouped", "--category", "allow")
	if !strings.Contains(r.Stdout, "Read(//home/**)") {
		t.Fatalf("grouped:\n%s", r.Stdout)
	}
	e.fails("permission", "list", "--profile", "home", "--format", "grouped", "--category", "nonsense")
	e.ok("permission", "remove", "Read(//home/**)", "--all-profiles")
	r = e.ok("permission", "list", "--all-profiles", "--format", "flat")
	if strings.Contains(r.Stdout, "Read(//home/**)") {
		t.Fatalf("not removed:\n%s", r.Stdout)
	}
	e.fails("permission", "add", "Read(//home/**)")
	e.fails("permission", "add", "Bash(git add:*)", "--profile", "work")
	r = e.ok("permission", "list", "--profile", "work", "--format", "flat", "--json")
	if r.Data == nil && !strings.Contains(r.Stdout+r.Stderr, "work") {
		t.Fatalf("--json:\n%s\n%s", r.Stdout, r.Stderr)
	}
}

func TestProfileCommands(t *testing.T) {
	e := newEnv(t)
	e.setUp("work")
	r := e.ok("profile", "show", "work")
	if !strings.Contains(r.Stdout, "work") {
		t.Fatalf("show:\n%s", r.Stdout)
	}
	e.fails("profile", "show", "nothing-here")
	e.ok("profile", "set-plan", "work", "max-20x")
	token := testkit.ReadFile(t, filepath.Join(e.ws.ProfilesDir(), "work", ".claudewheel", "token.json"))
	if !strings.Contains(token, "rate_limit_tier") && !strings.Contains(token, "subscription") {
		t.Fatalf("plan not written:\n%s", token)
	}
	e.fails("profile", "set-plan", "work", "platinum")
	e.ok("profile", "rename", "work", "job")
	if _, err := os.Stat(filepath.Join(e.ws.ProfilesDir(), "job", "settings.json")); err != nil {
		t.Fatal(err)
	}
	e.fails("profile", "rename", "job", "Bad_Name")
	e.fails("profile", "rename", "job", "default")
	e.fails("profile", "exec", "--name", "job", "--dry-run", "--", "true")
	r = e.fails("profile", "exec", "--name", "nobody", "--", "true")
	if !strings.Contains(r.Stderr, "job") {
		t.Fatalf("unknown profile error does not list the profiles: %s", r.Stderr)
	}
	r = e.fails("profile", "fix-auth", "job")
	if !strings.Contains(r.Stderr, "No long-lived token for 'job', nothing to fix.") {
		t.Fatalf("fix-auth without a token: %s", r.Stderr)
	}
	dir := filepath.Join(e.ws.ProfilesDir(), "job")
	testkit.WriteFile(t, filepath.Join(dir, ".claudewheel", "token.json"), `{"token": "sk-ant-oat01-test-only"}`)
	testkit.WriteFile(t, filepath.Join(dir, ".credentials.json"), `{"claudeAiOauth": {"accessToken": "x"}, "other": 1}`)
	r = e.ok("profile", "fix-auth", "job")
	if creds := testkit.ReadFile(t, filepath.Join(dir, ".credentials.json")); strings.Contains(creds, "claudeAiOauth") || !strings.Contains(creds, "other") {
		t.Fatalf("credentials after fix-auth:\n%s\n%s", creds, r.Stdout)
	}
	if r = e.ok("profile", "fix-auth", "job"); !strings.Contains(r.Stdout, "No auth shadow detected") {
		t.Fatalf("second fix-auth: %s", r.Stdout)
	}
}

const sessionA = "11111111-1111-4111-8111-111111111111"

func TestSessionCommands(t *testing.T) {
	e := newEnv(t)
	e.setUp("p1", "p2")
	home := filepath.Dir(e.ws.Root())
	old := filepath.Join(home, "work", "a")
	testkit.WriteFile(t, filepath.Join(old, "f"), "x")
	store := filepath.Join(e.ws.SharedDir(), "projects", workspace.EncodePath(old))
	testkit.WriteFile(t, filepath.Join(store, sessionA+".jsonl"), `{"type":"user","sessionId":"`+sessionA+`","cwd":"`+old+`"}`+"\n")
	for _, p := range []string{"p1", "p2"} {
		if err := os.Symlink(filepath.Join(e.ws.SharedDir(), "projects"), filepath.Join(e.ws.ProfilesDir(), p, "projects")); err != nil {
			t.Fatal(err)
		}
	}
	renamed := filepath.Join(home, "work", "b")
	e.ok("mv", old, renamed, "--dry-run")
	if _, err := os.Stat(old); err != nil {
		t.Fatal("mv --dry-run renamed the directory")
	}
	e.ok("mv", old, renamed)
	moved := filepath.Join(e.ws.SharedDir(), "projects", workspace.EncodePath(renamed), sessionA+".jsonl")
	if !strings.Contains(testkit.ReadFile(t, moved), `"cwd":"`+renamed+`"`) {
		t.Fatalf("transcript after mv:\n%s", testkit.ReadFile(t, moved))
	}
	other := filepath.Join(home, "work", "c")
	testkit.WriteFile(t, filepath.Join(other, "f"), "x")
	e.fails("move-session", "1111", other)
	e.ok("move-session", sessionA, other)
	if _, err := os.Stat(filepath.Join(e.ws.SharedDir(), "projects", workspace.EncodePath(other), sessionA+".jsonl")); err != nil {
		t.Fatalf("move-session: %v", err)
	}
	e.fails("migrate", "p1", "p2", "--session", "1111")
	e.fails("migrate", "p1", "nobody", "--all-sessions")
	e.fails("import", filepath.Join(home, "nowhere"))
	e.fails("import", filepath.Join(home, "nowhere"), "--from", "/a")
}

func TestVersionsAndUninstall(t *testing.T) {
	e := newEnv(t)
	versions := filepath.Join(filepath.Dir(e.ws.Root()), ".local", "share", "claude", "versions")
	for _, v := range []string{"2.1.9", "2.1.10", "2.0.1"} {
		testkit.WriteFile(t, filepath.Join(versions, v), "binary")
	}
	if err := os.MkdirAll(e.ws.BinDir(), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(versions, "2.1.10"), filepath.Join(e.ws.BinDir(), "claude")); err != nil {
		t.Fatal(err)
	}
	r := e.ok("versions")
	if !strings.Contains(r.Stdout, "2.1.10") || strings.Index(r.Stdout, "2.1.10") > strings.Index(r.Stdout, "2.1.9") {
		t.Fatalf("versions (newest first expected):\n%s", r.Stdout)
	}
	e.fails("uninstall", "2.1.10")
	e.fails("uninstall", "../x")
	e.ok("uninstall", "2.1.9")
	if _, err := os.Stat(filepath.Join(versions, "2.1.9")); !os.IsNotExist(err) {
		t.Fatal("not uninstalled")
	}
	e.fails("uninstall", "2.1.9")
}

func TestPurgePlugins(t *testing.T) {
	e := newEnv(t)
	e.setUp("work")
	plugins := filepath.Join(e.ws.ProfilesDir(), "work", "plugins")
	testkit.WriteFile(t, filepath.Join(plugins, "marketplaces", "official", "x.json"), "{}")
	e.fails("purge-plugins")
	r := e.ok("purge-plugins", "--profile", "work", "--dry-run")
	if _, err := os.Stat(plugins); err != nil {
		t.Fatalf("dry run removed the tree: %s", r.Stdout)
	}
	e.ok("purge-plugins", "--all-profiles")
	if _, err := os.Stat(plugins); !os.IsNotExist(err) {
		t.Fatal("plugins not purged")
	}
	e.fails("purge-plugins", "--profile", "default")
}

func TestProbeCommandsOutsideASession(t *testing.T) {
	e := newEnv(t)
	e.setUp()
	e.ok("probe", "list")
	r := e.fails("probe", "create", "oom-kill", "--all-sessions", "--deadline", "1h")
	if !strings.Contains(r.Stderr, "session") {
		t.Fatalf("create outside a session: %s", r.Stderr)
	}
	e.fails("probe", "create", "oom-kill", "--all-sessions", "--deadline", "soon")
	e.fails("probe", "create", "oom-kill", "--all-sessions", "--deadline", "1h", "--", "sleep", "1")
	e.fails("probe", "run-service", "--dry-run")
}
