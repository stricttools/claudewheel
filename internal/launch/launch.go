// Package launch starts a Claude Code session, or another client's: it
// takes the session choice and the segment values given on the command line,
// shows the launch bar unless they settle every required segment, then runs
// the health check, the user's pre-launch hooks, and the preflight steps,
// records the launch, builds the client's argv and environment, and
// replaces this process with the client started in the session's systemd
// units.
package launch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/discover"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/health"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/projecthooks"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/tui/bar"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Env is what a launch runs with.
type Env struct {
	// Ctx is cancelled on Ctrl-C (cause terminal.ErrInterrupted) and on
	// SIGTERM or SIGHUP; every prompt returns its cause.
	Ctx context.Context
	FX  *effects.FX
	// Store is the workspace's app config, opened by the caller.
	Store *appconfig.Store
	// Executable is the absolute path of the claudewheel binary, which the
	// health check compares the probe runner's unit with.
	Executable string
	// Stderr receives the launch's reports; print mode keeps stdout for the
	// client's answer.
	Stderr io.Writer
}

// Request is what the command line asked for.
type Request struct {
	Session Session
	// Presets are the -s KEY=VALUE values, in the order given.
	Presets []string
	// Client is the client given with --client; empty when none was.
	Client string
	// ClientArgs are the arguments given after "--", passed to the client.
	ClientArgs []string
}

// launcher is one launch.
type launcher struct {
	env      Env
	ws       workspace.Workspace
	store    *appconfig.Store
	binaries Binaries
	screens  *screens
}

// Run performs the launch req asks for. It returns nil without launching
// when the user quits the bar, and does not return once the client starts
// (under --dry-run the exec is recorded and it returns nil).
func Run(env Env, req Request) error {
	store := env.Store
	ws := store.Workspace()
	l := &launcher{
		env:      env,
		ws:       ws,
		store:    store,
		binaries: Binaries{Locator: install.LocatorFor(ws), Clients: store.Config.Clients},
		screens:  &screens{ws: ws, theme: store.Config.Theme},
	}
	defaultClient, err := ResolveDefaultClient(store.Config)
	if err != nil {
		return err
	}
	planned := defaultClient
	if req.Client != "" {
		if _, err := LookupAdapter(req.Client); err != nil {
			return err
		}
		planned = req.Client
	}
	plannedAdapter, err := LookupAdapter(planned)
	if err != nil {
		return err
	}

	enabled := enabledSegments(store)
	presets, err := ParsePresets(req.Presets, segmentKeys(enabled))
	if err != nil {
		return err
	}
	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	if _, set := presets[segDirectory]; !set && slices.ContainsFunc(enabled, func(s appconfig.Segment) bool { return s.Key == segDirectory }) {
		presets[segDirectory] = cwd
	}
	sessionDir := cwd
	if d := presets[segDirectory]; d != "" {
		if sessionDir, err = projecthooks.TargetDirectory(d); err != nil {
			return err
		}
	}

	session := req.Session
	if session.Mode == SessionResume && session.Value == "" {
		// An empty --resume opens Claude Code's own picker, as --picker does.
		session = Session{Mode: SessionPicker}
	}
	interactive := interactiveTerminal(session)
	if session.Mode == SessionResume || session.Mode == SessionContinue {
		mover, err := l.sessionMover(interactive)
		if err != nil {
			return err
		}
		if session.Mode == SessionResume {
			if session.Value, err = resolveResume(ws, session.Value, sessionDir); err != nil {
				return err
			}
			if err := mover.checkResume(session.Value, sessionDir); err != nil {
				return err
			}
		} else if err := mover.checkContinue(sessionDir); err != nil {
			return err
		}
	}

	// The segments a launch needs, other than those the client has no use
	// for.
	required := requiredKeys(enabled, plannedAdapter, func(appconfig.Segment) bool { return true })
	skipBar := session.Mode == SessionPrint || (len(required) > 0 && allPreset(required, presets))
	if skipBar {
		return l.launchWithoutBar(plannedAdapter, presets, enabled, session, req.ClientArgs, interactive)
	}
	return l.launchFromBar(req.Client, defaultClient, presets, session, req.ClientArgs)
}

// launchWithoutBar launches with the values given on the command line over
// those of the last launch. Print mode takes only the segments that apply to
// it, and every one of those that is required must have a value.
func (l *launcher) launchWithoutBar(adapter Adapter, presets map[string]string, enabled []appconfig.Segment, session Session, clientArgs []string, interactive bool) error {
	for _, key := range slices.Sorted(maps.Keys(presets)) {
		if err := adapter.CheckExplicit(key, presets[key]); err != nil {
			return err
		}
	}
	selections := maps.Clone(l.store.State.LastConfig)
	if selections == nil {
		selections = map[string]string{}
	}
	maps.Copy(selections, presets)
	adapter.DropInapplicable(selections)
	if session.Mode == SessionPrint {
		printable := map[string]bool{}
		for _, s := range enabled {
			if s.PrintMode {
				printable[s.Key] = true
			}
		}
		maps.DeleteFunc(selections, func(k, _ string) bool { return !printable[k] })
		var missing []string
		for _, key := range requiredKeys(enabled, adapter, func(s appconfig.Segment) bool { return s.PrintMode }) {
			if selections[key] == "" {
				missing = append(missing, key)
			}
		}
		if len(missing) > 0 {
			return fmt.Errorf("print mode needs a value for every required segment, and these have none: %s; give them with -s KEY=VALUE, or launch once from the bar so the last launch's values are remembered", strings.Join(missing, ", "))
		}
	}
	maps.DeleteFunc(selections, func(_, v string) bool { return v == "" })
	modelID := ""
	if model := selections[segModel]; model != "" {
		modelID = model
		if m, ok := l.store.Options[segModel].MetadataFor(model); ok && m.ModelID != nil && *m.ModelID != "" {
			modelID = *m.ModelID
		}
	}
	return l.sequence(launchPlan{
		adapter:     adapter,
		selections:  selections,
		modelID:     modelID,
		session:     session,
		clientArgs:  clientArgs,
		interactive: interactive,
	})
}

// launchFromBar shows the launch bar, preset with the command line's values,
// and launches what the user chose there.
func (l *launcher) launchFromBar(explicitClient, defaultClient string, presets map[string]string, session Session, clientArgs []string) error {
	ctx := l.env.Ctx
	clients, err := BarClients(l.binaries, defaultClient, explicitClient)
	if err != nil {
		return err
	}
	t, err := l.screens.openTerminal(ctx, "the launch bar (shown unless -s presets every required segment or --print-prompt runs one prompt)")
	if err != nil {
		return err
	}
	// Close is idempotent: the deferred one restores the terminal when the
	// bar ends by a panic, such as a --dry-run reaching a recorded result.
	defer t.Close()
	colors := l.screens.colors
	out, err := bar.Run(ctx, l.env.FX, t, colors, l.store, bar.Input{
		Locator:   l.binaries.Locator,
		Home:      filepath.Dir(l.ws.Root()),
		Now:       time.Now,
		Overrides: presets,
		Clients:   clients,
		Flows:     BarFlows(l.env.FX, l.ws, l.binaries.Locator, t, colors),
	})
	if err = errors.Join(err, t.Close()); err != nil || !out.Launch {
		return err
	}
	adapter, err := LookupAdapter(out.Client)
	if err != nil {
		return err
	}
	modelID := ""
	if model := out.Selections[segModel]; model != "" {
		modelID = model
		if m, ok := out.Metadata[segModel][model]; ok && m.ModelID != "" {
			modelID = m.ModelID
		}
	}
	return l.sequence(launchPlan{
		adapter:     adapter,
		selections:  out.Selections,
		modelID:     modelID,
		session:     session,
		clientArgs:  clientArgs,
		interactive: true,
	})
}

// launchPlan is a launch whose values are settled.
type launchPlan struct {
	adapter Adapter
	// selections maps each segment with a value to it.
	selections map[string]string
	// modelID is the model id the model selection resolves to.
	modelID     string
	session     Session
	clientArgs  []string
	interactive bool
}

// sequence runs what comes between the values being settled and the client
// starting: the health check (when someone can be asked whether to launch
// anyway), the user's pre-launch hooks, recording the launch, the preflight,
// and building and starting the client.
func (l *launcher) sequence(p launchPlan) error {
	fx := l.env.FX
	if p.interactive && l.store.Config.HealthCheckOnLaunch {
		if err := l.healthCheck(); err != nil {
			return err
		}
	}
	if err := RunUserHooks(fx, l.ws.HooksDir(), preLaunchStage, p.selections); err != nil {
		return fmt.Errorf("pre-launch hook failed, launch aborted: %w", err)
	}
	directory, err := projecthooks.TargetDirectory(p.selections[segDirectory])
	if err != nil {
		return err
	}
	// Recorded only after the hooks passed, so an aborted launch is not
	// counted; a launch nobody could see is not remembered either.
	if p.interactive {
		if err := l.store.RecordLaunch(fx, p.selections); err != nil {
			return err
		}
		if err := appconfig.RecordInode(fx, l.ws.Shared(), directory); err != nil {
			return err
		}
	}
	if err := RunPreflight(PreflightContext{
		Ctx:         l.env.Ctx,
		FX:          fx,
		Workspace:   l.ws,
		Locator:     l.binaries.Locator,
		Selections:  p.selections,
		Client:      p.adapter.Name,
		Interactive: p.interactive,
		Stderr:      l.env.Stderr,
		screens:     l.screens,
	}, PreflightSteps()); err != nil {
		return err
	}

	env, secrets, err := l.clientEnv(p)
	if err != nil {
		return err
	}
	argv, err := p.adapter.Argv(ClientContext{
		Selections:      p.selections,
		ModelID:         p.modelID,
		DefaultFlags:    l.store.Config.DefaultFlags,
		DisallowedTools: guardrail.DisallowedToolNames(),
		Session:         p.session,
		ClientArgs:      p.clientArgs,
		Binaries:        l.binaries,
	})
	if err != nil {
		return err
	}
	toolCap, err := ToolCapFrom(l.store.Config)
	if err != nil {
		return err
	}
	return Exec(fx, directory, argv, env, toolCap, l.ws.ScriptsDir(), l.env.Stderr, secrets)
}

// clientEnv builds the client's environment from this process's: the
// profile's launch environment (the default profile removes every profile
// variable), GH_TOKEN from the selected GitHub account, and the launch facts
// the lifecycle hooks read. It returns the secrets it put there, for
// redaction. A GitHub token that cannot be fetched refuses the launch.
func (l *launcher) clientEnv(p launchPlan) ([]string, []string, error) {
	profile := p.selections[segProfile]
	if profile == "" {
		profile = profiles.DefaultName
	}
	pe, err := profiles.New(l.ws).LaunchEnv(profile)
	if err != nil {
		return nil, nil, err
	}
	env := pe.Apply(os.Environ())
	var secrets []string
	if pe.Token != "" {
		secrets = append(secrets, pe.Token)
	}
	if account := p.selections[segGitHub]; account != "" {
		token, err := discover.GitHubToken(l.env.FX, account)
		if err != nil {
			return nil, nil, fmt.Errorf("launch refused: the token of GitHub account %s cannot be fetched: %w", account, err)
		}
		env = setEnv(env, "GH_TOKEN", token)
		secrets = append(secrets, token)
	}
	// claudewheel's own statements about the launch, which the vanilla
	// default carries too. A fact claudewheel did not choose is removed, so
	// a launch started inside a launched session never passes its parent's
	// facts off as its own.
	facts := []struct{ key, value string }{
		{"CLAUDEWHEEL_LAUNCH_PROFILE", p.selections[segProfile]},
		{"CLAUDEWHEEL_LAUNCH_VERSION", p.selections[segVersion]},
		{"CLAUDEWHEEL_LAUNCH_MODEL", p.modelID},
		{"CLAUDEWHEEL_LAUNCH_PERMISSIONS", p.selections[segPermissions]},
		{"CLAUDEWHEEL_LIFECYCLE_DIR", l.ws.Shared().LifecycleDir()},
	}
	for _, f := range facts {
		if f.value != "" {
			env = setEnv(env, f.key, f.value)
		} else {
			env = unsetEnv(env, f.key)
		}
	}
	return env, secrets, nil
}

// healthCheck runs the health checks and, when any is not OK, asks whether
// to launch anyway.
func (l *launcher) healthCheck() error {
	results := health.Run(health.Inputs{FX: l.env.FX, Workspace: l.ws, Executable: l.env.Executable, Today: time.Now()})
	var lines []string
	for _, r := range results {
		if !r.OK {
			lines = append(lines, r.Line())
		}
	}
	if len(lines) == 0 {
		return nil
	}
	answer, err := l.screens.confirm(l.env.Ctx, widgets.Confirmation{
		Title:   "Health warnings",
		Lines:   lines,
		Accept:  "launch anyway",
		Decline: "abort the launch",
		Skip:    "abort the launch",
	})
	if err != nil {
		return err
	}
	if answer != widgets.Accept {
		return errors.New("launch aborted after the health warnings; `claudewheel health` lists them")
	}
	return nil
}

// sessionMover builds the mover of renamed projects' sessions.
func (l *launcher) sessionMover(interactive bool) (*sessionMover, error) {
	store := profiles.New(l.ws)
	if err := store.CheckPendingRenames(); err != nil {
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
	return &sessionMover{
		ctx:         l.env.Ctx,
		fx:          l.env.FX,
		ws:          l.ws,
		profiles:    dirs,
		screens:     l.screens,
		stderr:      l.env.Stderr,
		interactive: interactive,
	}, nil
}

// enabledSegments returns the segments config.json enables, in bar order.
func enabledSegments(store *appconfig.Store) []appconfig.Segment {
	var out []appconfig.Segment
	for _, s := range store.Segments {
		if slices.Contains(store.Config.EnabledSegments, s.Key) {
			out = append(out, s)
		}
	}
	return out
}

func segmentKeys(segs []appconfig.Segment) []string {
	keys := make([]string, len(segs))
	for i, s := range segs {
		keys[i] = s.Key
	}
	return keys
}

// requiredKeys returns the keys of the required segments among segs that
// keep selects and the adapter does not hide.
func requiredKeys(segs []appconfig.Segment, adapter Adapter, keep func(appconfig.Segment) bool) []string {
	var keys []string
	for _, s := range segs {
		if s.Required && keep(s) && !adapter.Hides(s.Key) {
			keys = append(keys, s.Key)
		}
	}
	return keys
}

// allPreset reports whether every key has a preset value.
func allPreset(keys []string, presets map[string]string) bool {
	for _, k := range keys {
		if _, ok := presets[k]; !ok {
			return false
		}
	}
	return true
}

// ParsePresets reads -s KEY=VALUE values into a map. A value without "=", a
// key no enabled segment has, and a key given twice are errors.
func ParsePresets(raw []string, enabled []string) (map[string]string, error) {
	out := map[string]string{}
	for _, item := range raw {
		key, value, ok := strings.Cut(item, "=")
		if !ok {
			return nil, fmt.Errorf("invalid -s value %q: expected KEY=VALUE", item)
		}
		if !slices.Contains(enabled, key) {
			return nil, fmt.Errorf("unknown segment %q in -s %s (the enabled segments: %s)", key, item, strings.Join(enabled, ", "))
		}
		if prior, dup := out[key]; dup {
			return nil, fmt.Errorf("segment %q is given twice with -s: %q and %q", key, prior, value)
		}
		out[key] = value
	}
	return out, nil
}
