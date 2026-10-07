package bar

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"time"
	"unicode"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/discover"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// Client is one launch client the bar can launch, as the launch's adapter
// registry describes it.
type Client struct {
	Name string
	// Available is false when the client's binary cannot be found; the
	// client picker still offers it, marked "(not installed)".
	Available bool
	// HiddenSegments are the segments that do not apply to the client: the
	// bar drops them once the client is chosen.
	HiddenSegments []string
	// RejectedValues maps a segment key to values the client does not
	// take: they are drawn unavailable, and launching with one is refused.
	RejectedValues map[string][]string
}

// Clients is the client choice the bar starts with.
type Clients struct {
	// Registry is every client, in the order the picker offers them.
	Registry []Client
	// Default is config.json's default client: the picker's first focus.
	Default string
	// Explicit is the client given on the command line, which skips the
	// picker; empty when none was given.
	Explicit string
}

func (c Clients) lookup(name string) (Client, error) {
	for _, client := range c.Registry {
		if client.Name == name {
			return client, nil
		}
	}
	names := make([]string, len(c.Registry))
	for i, client := range c.Registry {
		names[i] = client.Name
	}
	return Client{}, fmt.Errorf("unknown client %q; known: %s", name, strings.Join(names, ", "))
}

// authFlash is the message each outcome other than widgets.AuthSkipped leaves on the
// bar, which stays open.
func authFlash(o widgets.AuthOutcome) (string, error) {
	switch o {
	case widgets.AuthAuthenticated:
		return "Authenticated", nil
	case widgets.AuthUnverified:
		return "Saved unverified token", nil
	case widgets.AuthCancelled:
		return "Auth cancelled", nil
	case widgets.AuthFailed:
		return "Auth failed", nil
	}
	return "", fmt.Errorf("unknown authentication outcome %q", o)
}

// ChecklistOutcome is how the deletion checklist ended: whether the
// deletion goes ahead, and the sessions still holding the profile after it
// stopped what the user ticked.
type ChecklistOutcome struct {
	Confirmed    bool
	StillHolding []sessions.SessionRecord
}

// Flows are the screens the bar opens that live in packages above it. Each
// draws on the bar's terminal, already in cbreak mode on the alternate
// screen, and returns the cancellation cause when ctx is done. Every field
// is required.
type Flows struct {
	// CreateProfile runs the create-profile wizard, creates the profile,
	// offers to authenticate it, and shows the summary. It returns the new
	// profile's name, or false when the wizard was cancelled.
	CreateProfile func(ctx context.Context) (name string, created bool, err error)
	// Authenticate offers to authenticate profile before a launch, with a
	// choice to launch without it (widgets.AuthSkipped).
	Authenticate func(ctx context.Context, profile string) (widgets.AuthOutcome, error)
	// DeletionChecklist shows what holds profile and stops what the user
	// ticks.
	DeletionChecklist func(ctx context.Context, profile string) (ChecklistOutcome, error)
	// SetVanillaGuardrails adds (true) or removes (false) claudewheel's
	// guardrails on ~/.claude.
	SetVanillaGuardrails func(fx *effects.FX, enable bool) error
}

func (f Flows) check() error {
	if f.CreateProfile == nil || f.Authenticate == nil || f.DeletionChecklist == nil || f.SetVanillaGuardrails == nil {
		return errors.New("bar: every Flows field is required")
	}
	return nil
}

// Input is what Run needs besides the terminal, the colors, and the app
// config.
type Input struct {
	// Locator finds the installed Claude Code binaries and the claude link.
	Locator install.Locator
	// Home is the user's home directory.
	Home string
	// Now gives the current time.
	Now func() time.Time
	// Overrides are segment values given on the command line (segment key
	// -> value); see ApplyOverrides.
	Overrides map[string]string
	Clients   Clients
	Flows     Flows
}

// Outcome is what the bar ended with.
type Outcome struct {
	// Launch is false when the user quit without launching.
	Launch bool
	// Selections maps each segment with a value to it.
	Selections map[string]string
	// Client is the chosen client.
	Client string
	// Metadata is each segment's value metadata (a model option's model
	// id among it), keyed by segment key, for segments that have any.
	Metadata map[string]map[string]ValueMetadata
}

// action is what a key asks the loop to do next.
type action int

const (
	actNone action = iota
	actLaunch
	actQuit
)

// The key modes, by precedence: typing a new option, editing a freeform
// value, and everything else.
const (
	modeMain     = "main"
	modeCreating = "creating"
	modeFreeform = "freeform"
)

// keyContext is what binding conditions read.
type keyContext struct {
	mode            string
	segKey          string
	searchBuffer    string
	hasValue        bool
	searchable      bool
	freeform        bool
	freeformEditing bool
}

// binding is one key binding. keys nil matches any printable character;
// an empty label hides the binding from the hints; mode "" applies in every
// mode. Bindings are tried in list order; priority orders the hints.
type binding struct {
	keys      []terminal.Key
	label     string
	condition func(keyContext) bool
	handler   func(a *app, ctx context.Context, key terminal.Key) (action, error)
	priority  int
	mode      string
}

func (b binding) matches(kc keyContext, key terminal.Key) bool {
	if b.mode != "" && b.mode != kc.mode {
		return false
	}
	if b.keys != nil {
		if !slices.Contains(b.keys, key) {
			return false
		}
	} else if !printable(key) {
		return false
	}
	return b.condition == nil || b.condition(kc)
}

// printable reports whether key is one printable character.
func printable(key terminal.Key) bool {
	r, ok := key.Char()
	return ok && unicode.IsPrint(r)
}

// slowResult is what the background discovery sends back.
type slowResult struct {
	results map[string]discover.Result
	err     error
}

// app is one run of the bar.
type app struct {
	fx       *effects.FX
	t        *terminal.Terminal
	store    *appconfig.Store
	in       Input
	env      discover.Env
	profiles profiles.Store
	bar      *Bar
	renderer *Renderer

	flash          string
	showProvenance bool
	mode2031       bool
	client         string

	// slow delivers the background discovery once; nil after it arrived.
	slow chan slowResult
	// pending holds the background results of the segment that had the
	// focus when they arrived, until it loses the focus.
	pending  map[string]discover.Result
	bindings []binding
}

// Run shows the launch bar until the user launches or quits. The terminal
// must be open and not yet in cbreak mode: Run asks it about mode 2031,
// enters cbreak mode on the alternate screen, and leaves cbreak mode when
// the user launches or quits. On an error, the cancellation cause after
// Ctrl-C (terminal.ErrInterrupted) among them, it returns at once and
// leaves restoring the terminal to its opener's Close.
//
// Before drawing, Run builds the bar from store (BuildBar), runs to
// completion the slow discovery of each segment in.Overrides gives a value
// that is not freeform, applies in.Overrides (a value such a segment does
// not offer is refused), and starts the other slow discoveries
// (discover.RunSlow) on a copy of the state; their results are merged as
// they arrive, except the focused segment's, which wait until it loses the
// focus. The client is chosen first: in.Clients.Explicit, or the client
// picker.
//
// The session to start in (new, continued, resumed, or picked) is the
// launch command's choice and never the bar's; a print-prompt launch does
// not open the bar.
func Run(ctx context.Context, fx *effects.FX, t *terminal.Terminal, colors widgets.Colors, store *appconfig.Store, in Input) (Outcome, error) {
	if in.Home == "" || in.Now == nil {
		return Outcome{}, errors.New("bar: Input.Home and Input.Now are required")
	}
	if err := in.Flows.check(); err != nil {
		return Outcome{}, err
	}
	if _, err := in.Clients.lookup(in.Clients.Default); err != nil {
		return Outcome{}, fmt.Errorf("the default client: %w", err)
	}
	if in.Clients.Explicit != "" {
		if _, err := in.Clients.lookup(in.Clients.Explicit); err != nil {
			return Outcome{}, err
		}
	}
	if t.Raw() {
		return Outcome{}, errors.New("bar: the terminal is already in cbreak mode; Run enters it itself")
	}
	minimap, err := ParseMinimapMode(store.Config.Minimap)
	if err != nil {
		return Outcome{}, err
	}

	ps := profiles.New(store.Workspace())
	env := discover.Env{FX: fx, Home: in.Home, Now: in.Now, State: cloneState(store.State), Profiles: ps}
	built, err := BuildBar(fx, env, store)
	if err != nil {
		return Outcome{}, err
	}
	// A preset segment's slow discovery runs to completion before its value
	// is checked, so nothing launches with a value it does not offer.
	discoveredPresets, err := discoverPresets(fx, env, store, built.Bar, in.Overrides)
	if err != nil {
		return Outcome{}, err
	}
	if err := ApplyOverrides(built.Bar, in.Overrides, discoveredPresets); err != nil {
		return Outcome{}, err
	}
	flashes := built.RefreshErrors
	for _, key := range slices.Sorted(maps.Keys(discoveredPresets)) {
		if seg, ok := built.Bar.Segment(key); ok && discoveredPresets[key] != nil {
			flashes = append(flashes, fmt.Sprintf("%s: %v", seg.Label, discoveredPresets[key]))
		}
	}

	a := &app{
		fx:       fx,
		t:        t,
		store:    store,
		in:       in,
		env:      env,
		profiles: ps,
		bar:      built.Bar,
		renderer: &Renderer{Colors: colors, Minimap: minimap},
		flash:    strings.Join(flashes, "; "),
		slow:     make(chan slowResult, 1),
		pending:  map[string]discover.Result{},
	}
	a.bindings = bindings()

	// The background discovery reads its own copy of the state and options,
	// so nothing it reads changes while it runs.
	bgEnv := env
	bgEnv.State = cloneState(store.State)
	opts := maps.Clone(store.Options)
	for key := range discoveredPresets {
		delete(opts, key)
	}
	go func() {
		results, err := discover.RunSlow(bgEnv, opts)
		a.slow <- slowResult{results, err}
	}()

	scheme, err := terminal.DetectMode2031(ctx)
	if err != nil {
		return Outcome{}, err
	}
	a.mode2031 = scheme != terminal.SchemeUnknown
	if err := t.EnterRaw(true); err != nil {
		return Outcome{}, err
	}
	if a.mode2031 {
		if err := t.SubscribeMode2031(); err != nil {
			return Outcome{}, err
		}
	}

	out, err := a.loop(ctx)
	if err != nil {
		return Outcome{}, err
	}
	if err := t.ExitRaw(); err != nil {
		return Outcome{}, err
	}
	return out, nil
}

// loop chooses the client, then reads keys until the user launches or
// quits.
func (a *app) loop(ctx context.Context) (Outcome, error) {
	chosen, err := a.selectClient(ctx)
	if err != nil || !chosen {
		return Outcome{}, err
	}
	if err := a.redraw(); err != nil {
		return Outcome{}, err
	}
	for {
		key, err := a.t.ReadKey(ctx)
		if err != nil {
			return Outcome{}, err
		}
		if key != terminal.KeyResize {
			act, err := a.handleKey(ctx, key)
			if err != nil {
				return Outcome{}, err
			}
			switch act {
			case actLaunch:
				if err := a.promoteEphemeral(); err != nil {
					return Outcome{}, err
				}
				return a.outcome(), nil
			case actQuit:
				return Outcome{Client: a.client}, nil
			}
		}
		if err := a.collectSlow(); err != nil {
			return Outcome{}, err
		}
		if err := a.redraw(); err != nil {
			return Outcome{}, err
		}
		if key != terminal.KeyResize {
			a.flash = ""
		}
	}
}

// outcome is the launch the bar ends with.
func (a *app) outcome() Outcome {
	meta := map[string]map[string]ValueMetadata{}
	for _, seg := range a.bar.Segments {
		if m := seg.State.Metadata(); len(m) > 0 {
			meta[seg.Key] = m
		}
	}
	return Outcome{Launch: true, Selections: a.bar.Selections(), Client: a.client, Metadata: meta}
}

// redraw recomputes the unavailable options and draws the bar.
func (a *app) redraw() error {
	if err := EvaluateRequires(a.bar, a.in.Locator); err != nil {
		return err
	}
	return a.renderer.Render(a.t, a.bar, Frame{
		Flash:          a.flash,
		ShowProvenance: a.showProvenance,
		Hints:          a.hints(),
	})
}

// selectClient decides the client (the explicit one, or the picker's
// answer) and fits the bar to it: its hidden segments go, and its rejected
// values are marked. A command-line value for a hidden segment, or one the
// client rejects, is an error. It reports false when the picker was
// cancelled.
func (a *app) selectClient(ctx context.Context) (bool, error) {
	name := a.in.Clients.Explicit
	if name == "" {
		options := make([]widgets.Option, len(a.in.Clients.Registry))
		for i, c := range a.in.Clients.Registry {
			label := c.Name
			if !c.Available {
				label += " (not installed)"
			}
			options[i] = widgets.Option{Key: c.Name, Label: label}
		}
		key, chosen, err := widgets.RunSelection(ctx, a.t, a.renderer.Colors, "Client", options, a.in.Clients.Default)
		if err != nil || !chosen {
			return false, err
		}
		name = key
	}
	client, err := a.in.Clients.lookup(name)
	if err != nil {
		return false, err
	}
	a.client = name
	for _, key := range client.HiddenSegments {
		if v, ok := a.in.Overrides[key]; ok {
			return false, fmt.Errorf("the %s segment does not apply to %s, but %s=%s was given", key, name, key, v)
		}
		if err := a.bar.RemoveSegment(key); err != nil {
			return false, err
		}
	}
	for key, values := range client.RejectedValues {
		if v, ok := a.in.Overrides[key]; ok && slices.Contains(values, v) {
			return false, fmt.Errorf("%s=%s does not apply to %s", key, v, name)
		}
		seg, ok := a.bar.Segment(key)
		if !ok {
			continue
		}
		for _, v := range values {
			seg.Rejected[v] = name
		}
	}
	return true, nil
}

// collectSlow merges the background discovery when it has arrived.
func (a *app) collectSlow() error {
	select {
	case res := <-a.slow:
		a.slow = nil
		return a.applySlow(res)
	default:
		return nil
	}
}

// applySlow merges the background results: models are recorded in
// options.json, the focused segment's results wait until it loses the
// focus, every other segment's are merged now, and the refreshed caches
// are saved to state.json. A refresh failure is shown on the bar.
func (a *app) applySlow(res slowResult) error {
	if res.err != nil {
		return fmt.Errorf("background discovery: %w", res.err)
	}
	if r, ok := res.results[appconfig.SegmentKeyModel]; ok {
		if err := a.store.RecordDiscoveredModels(a.fx, r.Values, r.ModelRecord()); err != nil {
			return err
		}
	}
	focused := a.bar.Focused()
	immediate := map[string]discover.Result{}
	var failures []string
	for _, key := range slices.Sorted(maps.Keys(res.results)) {
		r := res.results[key]
		if r.RefreshError != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", key, r.RefreshError))
		}
		if key == focused.Key {
			a.pending[key] = r
			focused.HasPending = true
			continue
		}
		immediate[key] = r
	}
	if err := MergeResults(a.bar, immediate, a.env, a.store.Options, a.store.State.LastConfig); err != nil {
		return err
	}
	for _, key := range slices.Sorted(maps.Keys(res.results)) {
		r := res.results[key]
		r.ApplyToState(&a.store.State)
	}
	if err := a.store.SaveState(a.fx); err != nil {
		return err
	}
	if len(failures) > 0 {
		a.flash = "Refresh failed: " + strings.Join(failures, "; ")
	}
	return nil
}

// applyPending merges seg's held-back background results.
func (a *app) applyPending(seg *Segment) error {
	r, ok := a.pending[seg.Key]
	if !ok {
		return nil
	}
	delete(a.pending, seg.Key)
	seg.HasPending = false
	return MergeResults(a.bar, map[string]discover.Result{seg.Key: r}, a.env, a.store.Options, a.store.State.LastConfig)
}

// defocus readies the focused segment to lose the focus: its held-back
// results are merged, and what was typed into it is dropped.
func (a *app) defocus() error {
	focused := a.bar.Focused()
	if err := a.applyPending(focused); err != nil {
		return err
	}
	focused.SearchBuffer = ""
	focused.freeformEditing = false
	return nil
}

// advance merges seg's held-back results and moves the focus right.
func (a *app) advance(seg *Segment) error {
	if err := a.applyPending(seg); err != nil {
		return err
	}
	a.bar.MoveFocus(1)
	return nil
}

func (a *app) keyContext() keyContext {
	f := a.bar.Focused()
	mode := modeMain
	switch {
	case f.Creating:
		mode = modeCreating
	case f.Freeform && f.SearchBuffer != "" && f.freeformEditing:
		mode = modeFreeform
	}
	_, hasValue := f.Value()
	return keyContext{
		mode:            mode,
		segKey:          f.Key,
		searchBuffer:    f.SearchBuffer,
		hasValue:        hasValue,
		searchable:      f.Searchable,
		freeform:        f.Freeform,
		freeformEditing: f.freeformEditing,
	}
}

// hints are the labels of the bindings that apply now, by priority.
func (a *app) hints() []string {
	kc := a.keyContext()
	var shown []binding
	for _, b := range a.bindings {
		if b.label == "" || (b.mode != "" && b.mode != kc.mode) {
			continue
		}
		if b.condition != nil && !b.condition(kc) {
			continue
		}
		shown = append(shown, b)
	}
	slices.SortStableFunc(shown, func(x, y binding) int { return x.priority - y.priority })
	labels := make([]string, len(shown))
	for i, b := range shown {
		labels[i] = b.label
	}
	return labels
}

// handleKey runs the first binding matching key.
func (a *app) handleKey(ctx context.Context, key terminal.Key) (action, error) {
	kc := a.keyContext()
	for _, b := range a.bindings {
		if b.matches(kc, key) {
			return b.handler(a, ctx, key)
		}
	}
	return actNone, nil
}

func keys(k ...terminal.Key) []terminal.Key { return k }

// onProfileWithValue: the profile segment with a value and nothing typed.
func onProfileWithValue(kc keyContext) bool {
	return kc.segKey == appconfig.SegmentKeyProfile && kc.searchBuffer == "" && kc.hasValue
}

// bindings is the key map. Order decides which binding takes a key: the
// letter bindings stand before the two that take any printable character.
func bindings() []binding {
	return []binding{
		// Every mode.
		{keys: keys(terminal.KeyThemeDark, terminal.KeyThemeLight), handler: (*app).themeSwitch},

		// Typing a new option.
		{keys: keys(terminal.KeyEnter), label: "enter: confirm", handler: (*app).createEnter, priority: 20, mode: modeCreating},
		{keys: keys(terminal.KeyEsc), label: "esc: cancel", handler: (*app).createEsc, priority: 20, mode: modeCreating},
		{keys: keys(terminal.KeyBackspace), label: "bksp: delete", handler: (*app).createBackspace, priority: 20, mode: modeCreating},
		{keys: keys(terminal.KeyCtrlC), handler: (*app).createCtrlC, priority: 20, mode: modeCreating},
		{handler: (*app).createPrintable, priority: 99, mode: modeCreating},

		// Editing a freeform value.
		{keys: keys(terminal.KeyEnter), label: "enter: submit", handler: (*app).freeformEnter, priority: 20, mode: modeFreeform},
		{keys: keys(terminal.KeyTab), label: "tab: accept match", handler: (*app).freeformTab, priority: 20, mode: modeFreeform},
		{keys: keys(terminal.KeyBackspace), label: "bksp: delete", handler: (*app).freeformBackspace, priority: 20, mode: modeFreeform},
		{keys: keys(terminal.KeyLeft), handler: (*app).freeformLeft, priority: 40, mode: modeFreeform},
		{keys: keys(terminal.KeyRight), handler: (*app).freeformRight, priority: 40, mode: modeFreeform},
		{keys: keys(terminal.KeyEsc), label: "esc: cancel", handler: (*app).freeformEsc, priority: 20, mode: modeFreeform},
		{keys: keys(terminal.KeyCtrlC), handler: (*app).freeformCtrlC, priority: 20, mode: modeFreeform},
		{handler: (*app).freeformPrintable, priority: 99, mode: modeFreeform},

		// The bar.
		{keys: keys(terminal.KeyLeft, terminal.KeyShiftTab), handler: (*app).mainLeft, priority: 40, mode: modeMain},
		{keys: keys(terminal.KeyRight), label: "arrows: navigate", handler: (*app).mainRight, priority: 40, mode: modeMain},
		{keys: keys(terminal.KeyUp), handler: (*app).mainUp, priority: 40, mode: modeMain},
		{keys: keys(terminal.KeyDown), handler: (*app).mainDown, priority: 40, mode: modeMain},
		{keys: keys(terminal.KeyEnter), label: "enter: launch", handler: (*app).mainEnter, priority: 30, mode: modeMain},
		{keys: keys(terminal.KeyTab), label: "tab: next", handler: (*app).mainTab, priority: 40, mode: modeMain},
		{keys: keys(terminal.KeyBackspace), handler: (*app).mainBackspace, priority: 40, mode: modeMain},
		{keys: keys(terminal.KeyEsc), label: "esc: clear", handler: (*app).mainEsc, priority: 20, mode: modeMain},
		{keys: keys(terminal.KeyCtrlC), handler: (*app).quit, priority: 60, mode: modeMain},
		{keys: keys(terminal.KeyCtrlD, terminal.KeyDelete), label: "del: delete", condition: onProfileWithValue, handler: (*app).mainDelete, priority: 50, mode: modeMain},
		{keys: keys("?"), label: "?: sources", condition: func(kc keyContext) bool { return kc.searchBuffer == "" }, handler: (*app).mainSources, priority: 60, mode: modeMain},
		{keys: keys("i"), label: "i: inspect", condition: onProfileWithValue, handler: (*app).mainInspect, priority: 50, mode: modeMain},
		// Opens from anywhere on the bar while nothing is typed; a lowercase
		// s still searches.
		{keys: keys("S"), label: "S: sessions", condition: func(kc keyContext) bool { return kc.searchBuffer == "" }, handler: (*app).mainSessions, priority: 50, mode: modeMain},
		// The first character typed on a freeform segment with a value starts
		// editing that value.
		{condition: func(kc keyContext) bool { return kc.freeform && !kc.freeformEditing && kc.hasValue }, handler: (*app).mainFreeformSeed, priority: 70, mode: modeMain},
		{condition: func(kc keyContext) bool { return kc.searchable }, handler: (*app).mainSearchOrQuit, priority: 80, mode: modeMain},
		{keys: keys("q"), label: "q: quit", condition: func(kc keyContext) bool { return !kc.searchable }, handler: (*app).quit, priority: 60, mode: modeMain},
	}
}

func (a *app) quit(context.Context, terminal.Key) (action, error) { return actQuit, nil }

// themeSwitch follows a mode 2031 notification to the dark or light theme.
func (a *app) themeSwitch(_ context.Context, key terminal.Key) (action, error) {
	name := appconfig.ThemeLight
	if key == terminal.KeyThemeDark {
		name = appconfig.ThemeDark
	}
	theme, err := appconfig.LoadTheme(a.store.Workspace(), name)
	if err != nil {
		return actNone, err
	}
	colors, err := widgets.ParseTheme(theme)
	if err != nil {
		return actNone, err
	}
	a.renderer.Colors = colors
	return actNone, nil
}

func (a *app) mainLeft(context.Context, terminal.Key) (action, error) {
	if err := a.defocus(); err != nil {
		return actNone, err
	}
	a.bar.MoveFocus(-1)
	return actNone, nil
}

func (a *app) mainRight(context.Context, terminal.Key) (action, error) {
	if err := a.defocus(); err != nil {
		return actNone, err
	}
	a.bar.MoveFocus(1)
	return actNone, nil
}

func (a *app) cycleFocused(direction int) (action, error) {
	f := a.bar.Focused()
	f.SearchBuffer = ""
	f.freeformEditing = false
	f.Cycle(direction)
	return actNone, nil
}

func (a *app) mainUp(context.Context, terminal.Key) (action, error)   { return a.cycleFocused(-1) }
func (a *app) mainDown(context.Context, terminal.Key) (action, error) { return a.cycleFocused(1) }

// startCreating handles Enter or Tab on PlusEntry: the profile segment opens
// the create-profile wizard, any other segment starts typing a new option.
func (a *app) startCreating(ctx context.Context, f *Segment) (action, error) {
	if f.Key == appconfig.SegmentKeyProfile {
		return actNone, a.profileWizard(ctx, f)
	}
	f.Creating = true
	f.CreateBuffer = ""
	return actNone, nil
}

// mainEnter launches when every check passes: required segments have a
// value, the selected version is installed (else the install is offered),
// no selection is unavailable or rejected by the client, and the selected
// profile is authenticated (else authentication is offered; the managed
// default profile is not checked).
func (a *app) mainEnter(ctx context.Context, _ terminal.Key) (action, error) {
	f := a.bar.Focused()
	if f.IsOnPlus() {
		return a.startCreating(ctx, f)
	}
	var missing []string
	for _, s := range a.bar.Segments {
		if _, ok := s.Value(); s.Required && !ok {
			missing = append(missing, s.Label)
		}
	}
	if len(missing) > 0 {
		a.flash = "Required: " + strings.Join(missing, ", ")
		return actNone, nil
	}
	for _, s := range a.bar.Segments {
		if v, ok := s.Value(); ok && s.State.NotInstalled(v) {
			return actNone, a.installFlow(ctx, s, v)
		}
	}
	for _, s := range a.bar.Segments {
		if v, ok := s.Value(); ok && s.Unavailable[v] {
			a.flash = fmt.Sprintf("%s: %s not available for this version", s.Label, v)
			return actNone, nil
		}
	}
	for _, s := range a.bar.Segments {
		v, ok := s.Value()
		if !ok {
			continue
		}
		if client, rejected := s.Rejected[v]; rejected {
			a.flash = fmt.Sprintf("%s: %s does not apply to %s", s.Label, v, client)
			return actNone, nil
		}
	}
	if s, ok := a.bar.Segment(appconfig.SegmentKeyProfile); ok {
		if v, has := s.Value(); has && s.State.Unauthenticated(v) {
			outcome, err := a.interceptUnauthenticated(ctx, s, v)
			if err != nil {
				return actNone, err
			}
			if outcome != widgets.AuthSkipped {
				msg, err := authFlash(outcome)
				if err != nil {
					return actNone, err
				}
				a.flash = msg
				return actNone, nil
			}
		}
	}
	return actLaunch, nil
}

// mainTab takes the best search match, then moves to the next segment when
// the segment advances on Tab.
func (a *app) mainTab(ctx context.Context, _ terminal.Key) (action, error) {
	f := a.bar.Focused()
	if f.IsOnPlus() {
		return a.startCreating(ctx, f)
	}
	if f.Searchable && f.SearchBuffer != "" {
		if matches := f.FilteredOptions(); len(matches) > 0 {
			f.SelectValue(matches[0])
		}
		f.SearchBuffer = ""
	}
	if f.TabAdvances {
		return actNone, a.advance(f)
	}
	return actNone, nil
}

// mainBackspace starts editing a freeform segment's value (without its last
// character), or trims the search.
func (a *app) mainBackspace(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	v, hasValue := f.Value()
	switch {
	case f.Freeform && !f.freeformEditing && hasValue && v != "":
		if trimmed := dropLastRune(v); trimmed != "" {
			f.SearchBuffer = trimmed
			f.freeformEditing = true
		}
	case f.Searchable && f.SearchBuffer != "":
		f.SearchBuffer = dropLastRune(f.SearchBuffer)
	}
	return actNone, nil
}

func (a *app) mainEsc(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.SearchBuffer = ""
	f.freeformEditing = false
	return actNone, nil
}

func (a *app) mainDelete(ctx context.Context, _ terminal.Key) (action, error) {
	return actNone, a.deleteProfile(ctx, a.bar.Focused())
}

func (a *app) mainSources(context.Context, terminal.Key) (action, error) {
	a.showProvenance = !a.showProvenance
	return actNone, nil
}

func (a *app) mainInspect(ctx context.Context, _ terminal.Key) (action, error) {
	return actNone, a.inspectProfile(ctx, a.bar.Focused())
}

func (a *app) mainSessions(ctx context.Context, _ terminal.Key) (action, error) {
	return actNone, a.sessionsOverview(ctx)
}

// mainFreeformSeed starts editing the focused freeform value with the typed
// character appended.
func (a *app) mainFreeformSeed(_ context.Context, key terminal.Key) (action, error) {
	f := a.bar.Focused()
	v, _ := f.Value()
	f.SearchBuffer = v + string(key)
	f.freeformEditing = true
	return actNone, nil
}

// mainSearchOrQuit types into a searchable segment's search; q with nothing
// typed quits.
func (a *app) mainSearchOrQuit(_ context.Context, key terminal.Key) (action, error) {
	f := a.bar.Focused()
	if f.SearchBuffer == "" && key == "q" {
		return actQuit, nil
	}
	f.SearchBuffer += string(key)
	return actNone, nil
}

// freeformEnter takes the typed text as the value (a launch-only option),
// then moves on when the segment advances on Tab.
func (a *app) freeformEnter(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	if text := strings.TrimSpace(f.SearchBuffer); text != "" {
		f.State.AddEphemeral(text)
		f.SelectValue(text)
	}
	f.SearchBuffer = ""
	f.freeformEditing = false
	if f.TabAdvances {
		return actNone, a.advance(f)
	}
	return actNone, nil
}

// freeformTab takes the best match of the typed text.
func (a *app) freeformTab(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	if matches := f.FilteredOptions(); len(matches) > 0 {
		f.SelectValue(matches[0])
	}
	f.SearchBuffer = ""
	f.freeformEditing = false
	if f.TabAdvances {
		return actNone, a.advance(f)
	}
	return actNone, nil
}

func (a *app) freeformBackspace(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.SearchBuffer = dropLastRune(f.SearchBuffer)
	if f.SearchBuffer == "" {
		f.freeformEditing = false
	}
	return actNone, nil
}

// freeformMove drops the edit and moves the focus.
func (a *app) freeformMove(direction int) (action, error) {
	f := a.bar.Focused()
	f.SearchBuffer = ""
	f.freeformEditing = false
	if err := a.applyPending(f); err != nil {
		return actNone, err
	}
	a.bar.MoveFocus(direction)
	return actNone, nil
}

func (a *app) freeformLeft(context.Context, terminal.Key) (action, error)  { return a.freeformMove(-1) }
func (a *app) freeformRight(context.Context, terminal.Key) (action, error) { return a.freeformMove(1) }

func (a *app) freeformEsc(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.SearchBuffer = ""
	f.freeformEditing = false
	return actNone, nil
}

func (a *app) freeformCtrlC(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.SearchBuffer = ""
	f.freeformEditing = false
	return actQuit, nil
}

func (a *app) freeformPrintable(_ context.Context, key terminal.Key) (action, error) {
	a.bar.Focused().SearchBuffer += string(key)
	return actNone, nil
}

func (a *app) createEnter(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	if strings.TrimSpace(f.CreateBuffer) != "" {
		return actNone, a.confirmCreate(f)
	}
	return actNone, nil
}

func (a *app) createEsc(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.Creating = false
	f.CreateBuffer = ""
	return actNone, nil
}

func (a *app) createBackspace(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.CreateBuffer = dropLastRune(f.CreateBuffer)
	return actNone, nil
}

func (a *app) createCtrlC(context.Context, terminal.Key) (action, error) {
	f := a.bar.Focused()
	f.Creating = false
	f.CreateBuffer = ""
	return actQuit, nil
}

func (a *app) createPrintable(_ context.Context, key terminal.Key) (action, error) {
	a.bar.Focused().CreateBuffer += string(key)
	return actNone, nil
}

// confirmCreate adds the typed option as pinned, selects it, and records
// it in options.json; an empty name, PlusEntry, or an existing option is
// dropped.
func (a *app) confirmCreate(seg *Segment) error {
	name := strings.TrimSpace(seg.CreateBuffer)
	seg.Creating = false
	seg.CreateBuffer = ""
	if name == "" || name == PlusEntry || slices.Contains(seg.Options(), name) {
		return nil
	}
	seg.State.AddPinned(name)
	seg.SelectValue(name)
	return a.store.AddOption(a.fx, seg.Key, name)
}

// promoteEphemeral pins every selected launch-only option, on disk too,
// before the launch.
func (a *app) promoteEphemeral() error {
	for _, seg := range a.bar.Segments {
		v, ok := seg.Selected()
		if !ok {
			continue
		}
		if src, found := seg.State.SourceOf(v); !found || src != SourceEphemeral {
			continue
		}
		if err := a.store.AddOption(a.fx, seg.Key, v); err != nil {
			return err
		}
		seg.State.AddPinned(v)
	}
	return nil
}

// dropLastRune returns s without its last character.
func dropLastRune(s string) string {
	r := []rune(s)
	if len(r) == 0 {
		return s
	}
	return string(r[:len(r)-1])
}
