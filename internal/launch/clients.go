package launch

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/tui/bar"
)

// The client names.
const (
	ClientClaude     = "claude"
	ClientMiniclaude = "miniclaude"
)

// The segment keys the launch reads values from.
const (
	segProfile     = "profile"
	segGitHub      = "github"
	segVersion     = "version"
	segModel       = "model"
	segDirectory   = "directory"
	segMCP         = "mcp"
	segPermissions = "permissions"
)

// Binaries is what the adapters find their client binaries with.
type Binaries struct {
	// Locator finds the installed Claude Code versions and the claude link.
	Locator install.Locator
	// Clients is config.json's clients section; nil when it has none.
	Clients *appconfig.ClientsConfig
}

// ClientContext is everything an adapter builds its argv from: the
// selections and the inputs the launch resolved from them.
type ClientContext struct {
	// Selections maps each segment with a value to it.
	Selections map[string]string
	// ModelID is the model id the model selection resolves to; empty when
	// no model is selected.
	ModelID string
	// DefaultFlags is config.json's default_flags.
	DefaultFlags []string
	// DisallowedTools is the guardrail's disallowed tool list.
	DisallowedTools []string
	// Session is the session the launch starts in.
	Session Session
	// ClientArgs are the arguments given after "--" on the command line.
	ClientArgs []string
	Binaries   Binaries
}

// Adapter is one client claudewheel can launch: what it builds its argv
// from, and which segments and values do not apply to it. Every surface
// that asks what applies to a client (the bar, the command line, the
// preflight) reads it from here.
type Adapter struct {
	Name string
	// HiddenSegments are the segments that do not apply to the client at
	// all: the bar hides them, an explicit value is refused, and a
	// remembered value is dropped.
	HiddenSegments []string
	// RejectedValues maps a segment key to the values the client does not
	// take; the segment's other values still apply.
	RejectedValues map[string][]string

	available func(Binaries) (bool, error)
	argv      func(ClientContext) ([]string, error)
}

// Adapters returns every client adapter, in the order the client picker
// offers them. Each call builds fresh values.
func Adapters() []Adapter {
	return []Adapter{
		{
			Name:      ClientClaude,
			available: claudeAvailable,
			argv:      claudeArgv,
		},
		{
			Name: ClientMiniclaude,
			// A version names a Claude Code binary claudewheel manages, which
			// miniclaude never runs; strict MCP is Claude Code's
			// --strict-mcp-config, which miniclaude has no equivalent of.
			HiddenSegments: []string{segVersion},
			RejectedValues: map[string][]string{segMCP: {"strict"}},
			available:      miniclaudeAvailable,
			argv:           miniclaudeArgv,
		},
	}
}

// AdapterNames returns the client names in registry order.
func AdapterNames() []string {
	all := Adapters()
	names := make([]string, len(all))
	for i, a := range all {
		names[i] = a.Name
	}
	return names
}

// LookupAdapter returns the adapter called name; an unknown name is an
// error listing the clients.
func LookupAdapter(name string) (Adapter, error) {
	for _, a := range Adapters() {
		if a.Name == name {
			return a, nil
		}
	}
	return Adapter{}, fmt.Errorf("unknown client %q; known: %s", name, strings.Join(AdapterNames(), ", "))
}

// ResolveDefaultClient returns config.json's default_client, refusing a name
// no adapter has.
func ResolveDefaultClient(cfg appconfig.Config) (string, error) {
	if _, err := LookupAdapter(cfg.DefaultClient); err != nil {
		return "", fmt.Errorf("config.json default_client: %w", err)
	}
	return cfg.DefaultClient, nil
}

// Available reports whether the client's binary can be found right now, by
// the same resolution its argv uses.
func (a Adapter) Available(b Binaries) (bool, error) {
	return a.available(b)
}

// Argv builds the argv the session runs.
func (a Adapter) Argv(ctx ClientContext) ([]string, error) {
	return a.argv(ctx)
}

// Hides reports whether segment key does not apply to the client.
func (a Adapter) Hides(key string) bool {
	return slices.Contains(a.HiddenSegments, key)
}

// Rejects reports whether the client does not take value for segment key.
func (a Adapter) Rejects(key, value string) bool {
	return slices.Contains(a.RejectedValues[key], value)
}

// CheckExplicit refuses a value given on the command line that does not
// apply to the client.
func (a Adapter) CheckExplicit(key, value string) error {
	if a.Hides(key) {
		return fmt.Errorf("the %s segment does not apply to the %s client, but -s %s=%s was given", key, a.Name, key, value)
	}
	if a.Rejects(key, value) {
		return fmt.Errorf("%s=%s does not apply to the %s client", key, value, a.Name)
	}
	return nil
}

// DropInapplicable removes from selections the remembered values that do
// not apply to the client: a hidden segment's value and a rejected value.
// Values given on the command line were checked by CheckExplicit first.
func (a Adapter) DropInapplicable(selections map[string]string) {
	for key, value := range selections {
		if a.Hides(key) || a.Rejects(key, value) {
			delete(selections, key)
		}
	}
}

// BarClients describes the registry to the bar: each client with its
// availability, hidden segments, and rejected values.
func BarClients(b Binaries, defaultClient, explicit string) (bar.Clients, error) {
	all := Adapters()
	out := bar.Clients{Registry: make([]bar.Client, len(all)), Default: defaultClient, Explicit: explicit}
	for i, a := range all {
		ok, err := a.Available(b)
		if err != nil {
			return bar.Clients{}, err
		}
		rejected := make(map[string][]string, len(a.RejectedValues))
		for k, v := range a.RejectedValues {
			rejected[k] = slices.Clone(v)
		}
		out.Registry[i] = bar.Client{
			Name:           a.Name,
			Available:      ok,
			HiddenSegments: slices.Clone(a.HiddenSegments),
			RejectedValues: rejected,
		}
	}
	return out, nil
}

// claudeAvailable reports whether the claude link a launch runs when no
// version is selected leads to a file.
func claudeAvailable(b Binaries) (bool, error) {
	_, err := os.Stat(b.Locator.Fallback())
	switch {
	case err == nil:
		return true, nil
	case errors.Is(err, fs.ErrNotExist):
		return false, nil
	}
	return false, err
}

// claudeArgv builds Claude Code's argv: the binary, default_flags, strict
// MCP, the permission mode, the model, the disallowed tools, the session
// flags, and the arguments given after "--".
func claudeArgv(ctx ClientContext) ([]string, error) {
	binary := ctx.Binaries.Locator.Fallback()
	if version := ctx.Selections[segVersion]; version != "" {
		if err := install.CheckVersionName(version); err != nil {
			return nil, err
		}
		binary = ctx.Binaries.Locator.BinaryFor(version)
		info, err := os.Stat(binary)
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			return nil, err
		}
		if err != nil || !info.Mode().IsRegular() {
			return nil, fmt.Errorf("Claude Code %s is not installed at %s; install it from the bar or with `claudewheel install %s`", version, binary, version)
		}
	}
	argv := []string{binary}
	argv = append(argv, ctx.DefaultFlags...)
	switch mcp := ctx.Selections[segMCP]; mcp {
	case "", "default":
	case "strict":
		argv = append(argv, "--strict-mcp-config")
	default:
		return nil, fmt.Errorf("mcp value %q is not one the claude client takes (default, strict)", mcp)
	}
	switch perm := ctx.Selections[segPermissions]; perm {
	case "":
	case "bypass":
		argv = append(argv, "--dangerously-skip-permissions")
	case "default", "plan", "auto":
		argv = append(argv, "--permission-mode="+perm)
	default:
		return nil, fmt.Errorf("permissions value %q is not one the claude client takes (bypass, default, plan, auto)", perm)
	}
	if ctx.ModelID != "" {
		argv = append(argv, "--model", ctx.ModelID)
	}
	if len(ctx.DisallowedTools) > 0 {
		argv = append(argv, "--disallowedTools")
		argv = append(argv, ctx.DisallowedTools...)
	}
	switch ctx.Session.Mode {
	case SessionNew:
	case SessionContinue:
		argv = append(argv, "--continue")
	case SessionResume:
		argv = append(argv, "--resume", ctx.Session.Value)
	case SessionPicker:
		argv = append(argv, "--resume")
	case SessionPrint:
		argv = append(argv, "--print", ctx.Session.Value)
	default:
		return nil, fmt.Errorf("unknown session mode %d", ctx.Session.Mode)
	}
	return append(argv, ctx.ClientArgs...), nil
}

// miniclaudeBinary is clients.miniclaude.binary from config.json when set,
// else miniclaude on PATH; "" when neither.
func miniclaudeBinary(b Binaries) (string, error) {
	if b.Clients != nil && b.Clients.Miniclaude != nil && b.Clients.Miniclaude.Binary != nil {
		return *b.Clients.Miniclaude.Binary, nil
	}
	path, ok, err := lookPath("miniclaude", os.Getenv("PATH"))
	if err != nil || !ok {
		return "", err
	}
	return path, nil
}

func miniclaudeAvailable(b Binaries) (bool, error) {
	binary, err := miniclaudeBinary(b)
	return binary != "", err
}

// miniclaudePermissions maps a permissions value to miniclaude's
// `repl --permission-mode` choice.
func miniclaudePermissions() map[string]string {
	return map[string]string{
		"bypass":  "bypassPermissions",
		"default": "default",
		"plan":    "plan",
		"auto":    "auto",
	}
}

// miniclaudeArgv builds `miniclaude repl --profile <profile> --model <id>
// --permission-mode <mode>` and the session flags miniclaude has; a launch
// with no model or no permission mode selected is refused. default_flags
// and the disallowed tools are Claude Code flags and do not apply.
func miniclaudeArgv(ctx ClientContext) ([]string, error) {
	profile := ctx.Selections[segProfile]
	if profile == "" {
		return nil, errors.New("the miniclaude client requires a claudewheel profile")
	}
	binary, err := miniclaudeBinary(ctx.Binaries)
	if err != nil {
		return nil, err
	}
	if binary == "" {
		return nil, errors.New("miniclaude binary not found on PATH; install it or set clients.miniclaude.binary in config.json")
	}
	if len(ctx.ClientArgs) > 0 {
		return nil, fmt.Errorf("the miniclaude client takes no arguments after '--'; got: %s", strings.Join(ctx.ClientArgs, " "))
	}
	// miniclaude repl requires both a model and a permission mode.
	if ctx.ModelID == "" {
		return nil, errors.New("the miniclaude client needs a model: the model segment has no value")
	}
	perm := ctx.Selections[segPermissions]
	if perm == "" {
		return nil, errors.New("the miniclaude client needs a permission mode: the permissions segment has no value")
	}
	mapped, ok := miniclaudePermissions()[perm]
	if !ok {
		return nil, fmt.Errorf("permission mode %q is not supported by the miniclaude client (supported: bypass, default, plan, auto)", perm)
	}
	argv := []string{binary, "repl", "--profile", profile, "--model", ctx.ModelID, "--permission-mode", mapped}
	switch ctx.Session.Mode {
	case SessionNew:
	case SessionContinue:
		argv = append(argv, "--continue-session")
	case SessionResume:
		argv = append(argv, "--resume", ctx.Session.Value)
	case SessionPicker:
		return nil, errors.New("the miniclaude client has no session picker: resume a session by its id instead")
	case SessionPrint:
		return nil, errors.New("print mode (--print-prompt) is not supported by the miniclaude client")
	default:
		return nil, fmt.Errorf("unknown session mode %d", ctx.Session.Mode)
	}
	return argv, nil
}

// lookPath finds an executable regular file called name on the
// colon-separated search list, as execvp does; an empty entry is the current
// directory. It reports false when there is none.
func lookPath(name, search string) (string, bool, error) {
	for _, dir := range filepath.SplitList(search) {
		if dir == "" {
			dir = "."
		}
		candidate := filepath.Join(dir, name)
		info, err := os.Stat(candidate)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) || errors.Is(err, fs.ErrPermission) || isNotDir(err) {
				continue
			}
			return "", false, err
		}
		if info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0 {
			return candidate, true, nil
		}
	}
	return "", false, nil
}
