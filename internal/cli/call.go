package cli

// The foundation every command handler stands on. A command is registered
// with readOnlyCommand or mutatingCommand, which fix its effect and hand its
// handler a *call: the dispatch context, the effects handle built for that
// effect, and the workspace. A handler returns an error; a non-nil error ends
// the command with exit status 1 and the error text on stderr, so no failure
// path can exit 0. Two errors are read differently:
//
//   - exitStatus(n): the handler has already reported what went wrong (say,
//     fail) and the command ends with status n and no further message;
//   - an error wrapping terminal.ErrInterrupted (Ctrl-C): the command ends
//     with status 130 and no message, since strictcli reports the signal.
//
// strictcli watches SIGINT and SIGTERM itself while a handler runs: the
// first one cancels ctx.Done(), and the exit status becomes 128 + the
// signal's number whatever the handler returns. signalContext adds SIGHUP
// and the cancellation causes the terminal and long-running code read.
//
// A handler that acts on the workspace opens it through appConfig before
// anything else, so a workspace that needs `claudewheel upgrade-workspace`
// (or was never set up) is refused before the command touches it. Profile
// work goes through profileStore, which refuses a leftover rename
// breadcrumb.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// interruptedStatus is the exit status of a command the user interrupted
// with Ctrl-C (128 + SIGINT).
const interruptedStatus = 130

// registrar is an App or a Group: anything commands are registered on.
type registrar interface {
	Command(name, help string, handler func(ctx *strictcli.Context, kwargs map[string]interface{}) strictcli.Outcome, opts ...strictcli.CmdOption)
}

// handler is a command's body. kw holds its flag and argument values, keyed
// by name with dashes turned into underscores (read them with the kw
// helpers below).
type handler func(c *call, kw map[string]interface{}) error

// call is one command dispatch.
type call struct {
	ctx *strictcli.Context
	// fx is effects.New(ctx) for a mutating command and effects.ReadOnly(ctx)
	// for a read-only one.
	fx *effects.FX
	ws workspace.Workspace
	// mutating is the command's declared effect.
	mutating bool
}

// readOnlyCommand registers a command that changes nothing. Its handler's
// FX refuses every mutation.
func readOnlyCommand(g registrar, name, help string, h handler, opts ...strictcli.CmdOption) {
	opts = append(opts, strictcli.WithEffect(strictcli.EffectReadOnly))
	g.Command(name, help, dispatch(false, h), opts...)
}

// mutatingCommand registers a command that changes something. Its
// handler's FX performs every mutation, or records it under --dry-run.
// Consequential, grants, and a refused --dry-run are passed in opts.
func mutatingCommand(g registrar, name, help string, h handler, opts ...strictcli.CmdOption) {
	opts = append(opts, strictcli.WithEffect(strictcli.EffectMutating))
	g.Command(name, help, dispatch(true, h), opts...)
}

// dispatch builds the call and runs h, mapping its error to the exit status.
func dispatch(mutating bool, h handler) func(*strictcli.Context, map[string]interface{}) strictcli.Outcome {
	return func(ctx *strictcli.Context, kw map[string]interface{}) strictcli.Outcome {
		c, err := newCall(ctx, mutating)
		if err == nil {
			err = h(c, kw)
		}
		return finish(ctx, err)
	}
}

// newCall builds the call of one dispatch: the FX matching the effect and
// the workspace under $HOME.
func newCall(ctx *strictcli.Context, mutating bool) (*call, error) {
	ws, err := workspace.Default()
	if err != nil {
		return nil, err
	}
	fx := effects.ReadOnly(ctx)
	if mutating {
		fx = effects.New(ctx)
	}
	return &call{ctx: ctx, fx: fx, ws: ws, mutating: mutating}, nil
}

// finish maps a handler's error to the command's exit status.
func finish(ctx *strictcli.Context, err error) strictcli.Outcome {
	var status *statusError
	switch {
	case err == nil:
		return strictcli.Exit(0)
	case errors.As(err, &status):
		return strictcli.Exit(status.code)
	case errors.Is(err, terminal.ErrInterrupted):
		return strictcli.Exit(interruptedStatus)
	default:
		ctx.Error(err.Error())
		return strictcli.Exit(1)
	}
}

// statusError ends a command with a status after the handler reported why.
type statusError struct {
	code int
}

func (e *statusError) Error() string { return fmt.Sprintf("exit status %d", e.code) }

// exitStatus is the error a handler returns when it has already reported
// what went wrong and the command should end with code and nothing more.
func exitStatus(code int) error {
	return &statusError{code: code}
}

// previewing reports whether this dispatch records mutations instead of
// performing them (--dry-run).
func (c *call) previewing() bool {
	return c.fx.Previewing()
}

// say writes one line of the command's answer to stdout; --quiet does not
// hide it, and under --json it goes into the envelope's output.
func (c *call) say(line string) {
	c.ctx.Out(line)
}

// sayf is say with formatting.
func (c *call) sayf(format string, args ...any) {
	c.ctx.Out(fmt.Sprintf(format, args...))
}

// info writes a progress line to stdout, hidden by --quiet.
func (c *call) info(line string) {
	c.ctx.Info(line)
}

// fail reports one error on stderr ("error: " is prefixed) without ending
// the command; return exitStatus afterwards.
func (c *call) fail(msg string) {
	c.ctx.Error(msg)
}

// appConfig opens the workspace's app config: appconfig.Ensure for a
// mutating command (it creates what a first run needs, recorded under
// --dry-run), appconfig.Load for a read-only one (a missing file is an
// error naming `claudewheel launch`). Both refuse a workspace that needs
// `claudewheel upgrade-workspace`.
func (c *call) appConfig() (*appconfig.Store, error) {
	if c.mutating {
		return appconfig.Ensure(c.fx, c.ws)
	}
	return appconfig.Load(c.ws)
}

// profileStore returns the profile store, refusing a rename breadcrumb an
// interrupted `claudewheel profile rename` left behind.
func (c *call) profileStore() (profiles.Store, error) {
	store := profiles.New(c.ws)
	if err := store.CheckPendingRenames(); err != nil {
		return profiles.Store{}, err
	}
	return store, nil
}

// profileDirs returns every profile's name and config directory, sorted by
// name, reading no token file: what the session operations scan.
func (c *call) profileDirs() ([]sessions.ProfileConfigDir, error) {
	store, err := c.profileStore()
	if err != nil {
		return nil, err
	}
	names, err := store.Names()
	if err != nil {
		return nil, err
	}
	dirs := make([]sessions.ProfileConfigDir, len(names))
	for i, name := range names {
		dirs[i] = sessions.ProfileConfigDir{Name: name, ConfigDir: store.PathFor(name)}
	}
	return dirs, nil
}

// signalContext returns a context cancelled on SIGINT (cause
// terminal.ErrInterrupted), SIGTERM or SIGHUP (cause
// terminal.ErrTerminated), and when the dispatch ends. Call stop when done;
// until then those signals do not kill the process. A handler returning the
// cause gets the matching exit status from finish.
func (c *call) signalContext() (ctx context.Context, stop func()) {
	base, cancel := context.WithCancel(context.Background())
	go func() {
		select {
		case <-c.ctx.Done():
			cancel()
		case <-base.Done():
		}
	}()
	ctx, stopSignals := terminal.WithSignals(base)
	return ctx, func() {
		stopSignals()
		cancel()
	}
}

// The readers of a handler's keyword arguments, by the declaration's shape.

// kwString is a required string argument's or flag's value.
func kwString(kw map[string]interface{}, name string) string {
	return strictcli.Get[string](kw, name)
}

// kwOptString is an optional string argument's or flag's value, and whether
// it was given.
func kwOptString(kw map[string]interface{}, name string) (string, bool) {
	return strictcli.GetOpt[string](kw, name)
}

// kwOptInt is an optional int flag's value, and whether it was given.
func kwOptInt(kw map[string]interface{}, name string) (int, bool) {
	return strictcli.GetOpt[int](kw, name)
}

// kwSwitch is an optional bool flag's value, false when it was not given:
// the fallback its help states.
func kwSwitch(kw map[string]interface{}, name string) bool {
	v, _ := strictcli.GetOpt[bool](kw, name)
	return v
}

// kwStrings is a repeatable string flag's values in the order given; empty
// when none were.
func kwStrings(kw map[string]interface{}, name string) []string {
	raw, _ := strictcli.GetOpt[[]interface{}](kw, name)
	out := make([]string, len(raw))
	for i, v := range raw {
		out[i] = v.(string)
	}
	return out
}

// kwBool is a required bool flag's value (the caller passed --flag or
// --no-flag).
func kwBool(kw map[string]interface{}, name string) bool {
	return strictcli.Get[bool](kw, name)
}

// openScreen loads the colors of the configured theme (asking the terminal
// for its background first when the theme is "auto"), then opens the
// terminal in cbreak mode on the alternate screen. The caller defers
// t.Close, which restores the terminal. No terminal to open (no /dev/tty)
// is an error.
func (c *call) openScreen(ctx context.Context, cfg *appconfig.Store) (*terminal.Terminal, widgets.Colors, error) {
	colors, err := widgets.LoadColors(ctx, c.ws, cfg.Config.Theme)
	if err != nil {
		return nil, widgets.Colors{}, err
	}
	t, err := terminal.Open()
	if err != nil {
		return nil, widgets.Colors{}, err
	}
	if err := t.EnterRaw(true); err != nil {
		return nil, widgets.Colors{}, errors.Join(err, t.Close())
	}
	return t, colors, nil
}

// ownExecutable is the path of the claudewheel binary running now, with
// symbolic links resolved: what the probe runner's unit and its health
// check name.
func ownExecutable() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("cannot find this claudewheel binary: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return "", fmt.Errorf("cannot resolve this claudewheel binary: %w", err)
	}
	return resolved, nil
}
