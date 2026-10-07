package launch

import (
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/testkit"
)

func binaries(t *testing.T) (Binaries, string) {
	t.Helper()
	ws := testkit.Workspace(t)
	loc := install.LocatorFor(ws)
	testkit.WriteFile(t, loc.BinaryFor("2.1.281"), "binary")
	mini := filepath.Join(t.TempDir(), "miniclaude")
	testkit.WriteFile(t, mini, "binary")
	return Binaries{Locator: loc, Clients: &appconfig.ClientsConfig{Miniclaude: &appconfig.ClientConfig{Binary: &mini}}}, mini
}

func TestClaudeArgv(t *testing.T) {
	b, _ := binaries(t)
	ctx := ClientContext{
		Selections:      map[string]string{segVersion: "2.1.281", segMCP: "strict", segPermissions: "bypass", segProfile: "work"},
		ModelID:         "claude-opus-5",
		DefaultFlags:    []string{"--verbose"},
		DisallowedTools: []string{"Artifact", "EnterPlanMode"},
		Session:         Session{Mode: SessionResume, Value: "abc"},
		ClientArgs:      []string{"--foo", "bar"},
		Binaries:        b,
	}
	claude, _ := LookupAdapter(ClientClaude)
	argv, err := claude.Argv(ctx)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{b.Locator.BinaryFor("2.1.281"), "--verbose", "--strict-mcp-config", "--dangerously-skip-permissions",
		"--model", "claude-opus-5", "--disallowedTools", "Artifact", "EnterPlanMode", "--resume", "abc", "--foo", "bar"}
	if !slices.Equal(argv, want) {
		t.Fatalf("argv\n got: %q\nwant: %q", argv, want)
	}
	for mode, flags := range map[SessionMode][]string{
		SessionNew:      nil,
		SessionContinue: {"--continue"},
		SessionPicker:   {"--resume"},
		SessionPrint:    {"--print", "p"},
	} {
		ctx.Session = Session{Mode: mode, Value: "p"}
		ctx.ClientArgs = nil
		argv, err := claude.Argv(ctx)
		if err != nil {
			t.Fatal(err)
		}
		tail := argv[len(argv)-len(flags):]
		if len(flags) > 0 && !slices.Equal(tail, flags) {
			t.Errorf("mode %d: argv ends %q", mode, tail)
		}
	}
	ctx.Session = Session{Mode: SessionNew}
	ctx.Selections[segPermissions] = "auto"
	if argv, _ := claude.Argv(ctx); !slices.Contains(argv, "--permission-mode=auto") {
		t.Errorf("auto: %q", argv)
	}
	for _, bad := range []map[string]string{
		{segPermissions: "plan"},
		{segPermissions: "yolo"},
		{segMCP: "loose"},
		{segVersion: "9.9.9"},
		{segVersion: "../x"},
	} {
		c := ctx
		c.Selections = map[string]string{}
		for k, v := range bad {
			c.Selections[k] = v
		}
		if _, err := claude.Argv(c); err == nil {
			t.Errorf("%v accepted", bad)
		}
	}
	ctx.Selections = map[string]string{}
	if argv, _ := claude.Argv(ctx); argv[0] != b.Locator.Fallback() {
		t.Errorf("no version: binary %s", argv[0])
	}
}

func TestMiniclaudeArgv(t *testing.T) {
	b, mini := binaries(t)
	adapter, _ := LookupAdapter(ClientMiniclaude)
	ctx := ClientContext{
		Selections:      map[string]string{segProfile: "work", segPermissions: "bypass"},
		ModelID:         "claude-opus-5",
		DefaultFlags:    []string{"--verbose"},
		DisallowedTools: []string{"Artifact"},
		Session:         Session{Mode: SessionContinue},
		Binaries:        b,
	}
	argv, err := adapter.Argv(ctx)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{mini, "repl", "--profile", "work", "--model", "claude-opus-5", "--permission-mode", "bypassPermissions", "--continue-session"}
	if !slices.Equal(argv, want) {
		t.Fatalf("argv\n got: %q\nwant: %q", argv, want)
	}
	ctx.Session = Session{Mode: SessionResume, Value: "id"}
	if argv, _ := adapter.Argv(ctx); !slices.Equal(argv[len(argv)-2:], []string{"--resume", "id"}) {
		t.Errorf("resume: %q", argv)
	}
	refusals := map[string]func(*ClientContext){
		"no profile":     func(c *ClientContext) { c.Selections = map[string]string{segPermissions: "bypass"} },
		"no model":       func(c *ClientContext) { c.ModelID = "" },
		"no permissions": func(c *ClientContext) { c.Selections = map[string]string{segProfile: "w"} },
		"plan":           func(c *ClientContext) { c.Selections = map[string]string{segProfile: "w", segPermissions: "plan"} },
		"client args":    func(c *ClientContext) { c.ClientArgs = []string{"x"} },
		"picker":         func(c *ClientContext) { c.Session = Session{Mode: SessionPicker} },
		"print":          func(c *ClientContext) { c.Session = Session{Mode: SessionPrint, Value: "p"} },
	}
	for name, change := range refusals {
		c := ctx
		c.Session = Session{Mode: SessionNew}
		change(&c)
		if _, err := adapter.Argv(c); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	if err := adapter.CheckExplicit(segVersion, "2.1.281"); err == nil {
		t.Error("an explicit version was accepted for miniclaude")
	}
	if err := adapter.CheckExplicit(segMCP, "strict"); err == nil {
		t.Error("strict MCP was accepted for miniclaude")
	}
	sel := map[string]string{segVersion: "1", segMCP: "strict", segModel: "m"}
	adapter.DropInapplicable(sel)
	if len(sel) != 1 || sel[segModel] != "m" {
		t.Errorf("after DropInapplicable: %v", sel)
	}
}

func TestMiniclaudeOnPath(t *testing.T) {
	stubs := testkit.StubPath(t)
	b := Binaries{}
	adapter, _ := LookupAdapter(ClientMiniclaude)
	if ok, err := adapter.Available(b); err != nil || ok {
		t.Fatalf("available without a binary: %v %v", ok, err)
	}
	testkit.Stub(t, stubs, "miniclaude", "exit 0")
	if ok, err := adapter.Available(b); err != nil || !ok {
		t.Fatalf("not available on PATH: %v %v", ok, err)
	}
}

func TestParsePresets(t *testing.T) {
	enabled := []string{segProfile, segModel}
	got, err := ParsePresets([]string{"profile=work", "model=a=b"}, enabled)
	if err != nil || got[segProfile] != "work" || got[segModel] != "a=b" {
		t.Fatalf("%v %v", got, err)
	}
	for _, bad := range [][]string{{"noequals"}, {"unknown=x"}, {"profile=a", "profile=b"}} {
		if _, err := ParsePresets(bad, enabled); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
	if _, err := LookupAdapter("vim"); err == nil || !strings.Contains(err.Error(), "claude, miniclaude") {
		t.Errorf("unknown client: %v", err)
	}
}
