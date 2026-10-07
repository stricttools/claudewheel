package sessionsview

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// HScrollStep is how far a left or right key scrolls the column strip.
const HScrollStep = 8

// markStates is what a key means in mark mode; a nil state clears the mark
// in force.
func markStates() map[terminal.Key]*string {
	state := func(s string) *string { return &s }
	return map[terminal.Key]*string{
		"h": state(lifecycle.StateOnHold),
		"b": state(lifecycle.StateBlocked),
		"d": state(lifecycle.StateDone),
		"c": nil,
	}
}

// The states drawn dim (a session held behind something else, and one that
// is over) and bold (a session at a prompt waiting for the user).
func dimStates() map[string]bool {
	return map[string]bool{lifecycle.StateBlocked: true, lifecycle.StateExited: true}
}

func boldStates() map[string]bool {
	return map[string]bool{lifecycle.StateWaiting: true}
}

const (
	hintDefault = "↑↓ move  ←→ scroll  enter: details  m: mark  " +
		"a: show all  p: prune crashed  r: refresh  q: close"
	hintShowAll = "↑↓ move  ←→ scroll  enter: details  m: mark  " +
		"a: loose ends  p: prune crashed  r: refresh  q: close"
	hintMark = "mark: h on-hold  b blocked  d done  c clear  esc cancel"
)

// Outcome is what the overview changed while it was open: every registry
// record whose file it deleted, how many mark events the user wrote, and how
// many ended events the gathering passes recorded for sessions that died
// without one.
type Outcome struct {
	Pruned []sessions.SessionRecord
	Marked int
	Swept  int
}

// StyleSequence is the escape sequence style is drawn in under c, the only
// place a table style becomes color. Bold and dim are applied directly on
// top of a state's hue: "this one wants you" and "this one is over". An
// unknown style, or a State style naming no lifecycle state, is an error: a
// misspelled style is a layout bug, not something to draw in the row color.
func StyleSequence(c widgets.Colors, style TableStyle) (string, error) {
	prefix, state, found := strings.Cut(string(style), ":")
	if found && (prefix == stateStylePrefix || prefix == stateFocusStylePrefix) {
		seq, err := c.StateFg(state)
		if err != nil {
			return "", fmt.Errorf("session table style %q: %w", style, err)
		}
		if boldStates()[state] {
			seq = terminal.Bold + seq
		} else if dimStates()[state] {
			seq = terminal.Dim + seq
		}
		if prefix == stateFocusStylePrefix {
			seq = c.SessionsFocusBg + seq
		}
		return seq, nil
	}
	switch style {
	case StyleFrame:
		return c.SessionsFrameFg, nil
	case StyleHeader:
		return terminal.Bold + c.SessionsHeaderFg, nil
	case StyleRow:
		return c.SessionsRowFg, nil
	case StyleRowFocus:
		return c.SessionsFocusBg + c.SessionsFocusFg, nil
	case StyleDetail, StyleEmpty:
		return c.SessionsDetailFg, nil
	}
	return "", fmt.Errorf("unknown session table style: %q", style)
}

// Draw draws frame over a cleared screen, with footer on the last row in the
// message color when message is set and the hint color otherwise. The footer
// is clipped one column short of the width: filling the last cell of the last
// row would leave the cursor in a pending wrap, and the next write would
// scroll the screen.
func Draw(t *terminal.Terminal, c widgets.Colors, frame Frame, footer string, message bool, rows, cols int) error {
	var buf strings.Builder
	buf.WriteString(terminal.ClearScreen)
	for i, line := range frame.Lines {
		buf.WriteString(terminal.MoveTo(i+1, 1))
		for _, span := range line {
			seq, err := StyleSequence(c, span.Style)
			if err != nil {
				return err
			}
			buf.WriteString(seq + span.Text)
		}
		buf.WriteString(terminal.Reset)
	}
	if rows > 0 && cols > 1 {
		color := c.SessionsHintFg
		if message {
			color = c.SessionsMessageFg
		}
		buf.WriteString(terminal.MoveTo(rows, 1) + color + widgets.RunePrefix(footer, cols-1) + terminal.Reset)
	}
	return t.Write(buf.String())
}

// MemoryReader measures the resident memory of pids in KiB, one ps call for
// all of them; a pid absent from the result is gone.
type MemoryReader func(pids []int) (map[int]int64, error)

// Sources is where the overview reads the machine from and writes its
// gathering passes and marks to.
type Sources struct {
	Workspace workspace.Workspace
	// Profiles are every profile the workspace discovers (the default
	// profile included) with its config directory, in enumeration order.
	Profiles []sessions.ProfileConfigDir
	Memory   MemoryReader
	// Clock returns the wall clock in milliseconds; uptimes are measured
	// against the clock of the last gather.
	Clock func() int64
	// Identity marks the session the user sits in; nil marks none.
	Identity *Identity
	// Home is written as ~ in paths.
	Home string
}

// verified reports whether record's process identity could be checked: the
// record carries a start token and the kernel still offers one for the pid.
// Where either is missing the process may be the recorded one or whatever
// took over its number, and the row says unverified rather than picking.
func verified(record sessions.SessionRecord) bool {
	if !record.Live || sessions.RecordedToken(record.ProcStart) == "" {
		return false
	}
	_, ok := sessions.ProcessStartToken(record.PID)
	return ok
}

// recordable reports whether session can be written to the lifecycle store,
// whose files are named after session uuids.
func recordable(session string) bool {
	return session != "" && lifecycle.SessionUUIDRE.MatchString(session)
}

func categoryLabel(category string) string {
	if label, ok := CategoryLabels()[category]; ok {
		return label
	}
	return category
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// rowForRecord builds a row from a registry record, filled out from its
// lifecycle (nil when the store has none). The registry is the authority on
// what it carries; the lifecycle supplies the model and transcript and
// stands in for a lost name.
func rowForRecord(pr sessions.ProfileRecord, life *lifecycle.SessionLifecycle, rssKiB *int64, identity *Identity, nowMS int64, undelivered int) (SessionRow, error) {
	record := pr.Record
	var started *lifecycle.StartedEvent
	var named *lifecycle.NamedEvent
	movedTo := ""
	if life != nil {
		started = life.Started
		named = life.Name
		// A dead process's registry file names the directory it ran in; a
		// move recorded since then is where the session is now.
		if life.Moved != nil {
			movedTo = life.Moved.NewCwd
		}
	}
	state, err := lifecycle.DeriveState(life, lifecycle.Observed{
		Live:            record.Live,
		Verified:        verified(record),
		Status:          record.Status,
		RegistryPresent: true,
	}, nowMS)
	if err != nil {
		return SessionRow{}, err
	}
	startedMS := record.StartedAt
	if startedMS == nil && started != nil {
		ms, err := lifecycle.ParseTimestampMS(started.At)
		if err != nil {
			return SessionRow{}, err
		}
		startedMS = &ms
	}
	name, nameSource := "", ""
	if named != nil {
		name, nameSource = named.Name, deref(named.NameSource)
	}
	cwd := record.Cwd
	if !record.Live && movedTo != "" {
		cwd = movedTo
	}
	if cwd == "" && life != nil {
		cwd, _ = life.Cwd()
	}
	version, model := record.Version, ""
	if started != nil {
		version = firstNonEmpty(version, deref(started.ClaudeVersion))
		model = deref(started.Model)
	}
	transcript := ""
	if life != nil {
		transcript, _ = life.Transcript()
	}
	pid := int64(record.PID)
	return SessionRow{
		Session:     record.SessionID,
		Name:        firstNonEmpty(record.Name, name, Unnamed),
		NameSource:  nameSource,
		State:       state,
		Category:    categoryLabel(record.Category),
		Cwd:         cwd,
		Profile:     pr.Profile,
		Version:     version,
		Model:       model,
		StartedMS:   startedMS,
		RSSKiB:      rssKiB,
		PID:         &pid,
		Current:     IsCurrent(record, identity),
		ConfigDir:   pr.ConfigDir,
		Transcript:  transcript,
		Record:      &record,
		Lifecycle:   life,
		Undelivered: undelivered,
	}, nil
}

// rowForLifecycle builds a row for a session no registry record answers for:
// its process is gone, or was never registered under a known profile, so
// everything comes from the store, and the category cell says nothing ever
// recorded one. profiles maps config directories to profile names.
func rowForLifecycle(life *lifecycle.SessionLifecycle, profiles map[string]string, nowMS int64, undelivered int) (SessionRow, error) {
	started := life.Started
	state, err := lifecycle.DeriveState(life, lifecycle.Observed{}, nowMS)
	if err != nil {
		return SessionRow{}, err
	}
	row := SessionRow{
		Session:     life.Session,
		Name:        Unnamed,
		State:       state,
		Category:    CategoryUnknown,
		Lifecycle:   life,
		Undelivered: undelivered,
	}
	if life.Name != nil {
		row.Name = life.Name.Name
		row.NameSource = deref(life.Name.NameSource)
	}
	row.Cwd, _ = life.Cwd()
	row.Transcript, _ = life.Transcript()
	if started != nil {
		row.ConfigDir = started.ConfigDir
		if started.Profile != nil {
			row.Profile = *started.Profile
		} else if name, ok := profiles[started.ConfigDir]; ok && name != "" {
			row.Profile = name
		} else if started.ConfigDir != "" {
			row.Profile = filepath.Base(started.ConfigDir)
		}
		row.Version = deref(started.ClaudeVersion)
		row.Model = deref(started.Model)
		ms, err := lifecycle.ParseTimestampMS(started.At)
		if err != nil {
			return SessionRow{}, err
		}
		row.StartedMS = &ms
		row.PID = started.PID
	}
	return row, nil
}

// Gathered is one read of the whole machine: the sorted rows, and the two
// writes the pass performed (ended events recorded for crashed sessions, and
// live names copied into the lifecycle store).
type Gathered struct {
	Rows  []SessionRow
	Swept int
	Named int
}

// Gather reads every session on the machine into sorted rows: the registry
// of every profile in src.Profiles and every session the lifecycle store has
// a file for, each row's state derived over both. It writes through fx, by
// the lifecycle store's own operations: an ended event for each session that
// died without one (lifecycle.SweepCrashed), and each live session's display
// name, which only the registry holds while the process lives
// (lifecycle.CaptureName). When either wrote, the store is read again so the
// rows show it as it now stands.
func Gather(fx *effects.FX, src Sources, nowMS int64) (Gathered, error) {
	lifecycleDir := src.Workspace.Shared().LifecycleDir()
	lifecycles, err := lifecycle.LoadAll(lifecycleDir)
	if err != nil {
		return Gathered{}, err
	}

	profiles := make(map[string]string, len(src.Profiles))
	for _, p := range src.Profiles {
		profiles[p.ConfigDir] = p.Name
	}
	found, err := sessions.ReadProfileRecords(src.Profiles)
	if err != nil {
		return Gathered{}, err
	}
	records := make([]sessions.SessionRecord, len(found))
	for i, pr := range found {
		records[i] = pr.Record
	}
	swept, err := lifecycle.SweepCrashed(fx, lifecycleDir, lifecycles, sessions.LiveSessionIDs(records), nowMS)
	if err != nil {
		return Gathered{}, err
	}
	named := 0
	for _, r := range records {
		if !r.Live || !recordable(r.SessionID) {
			continue
		}
		ev, err := lifecycle.CaptureName(fx, lifecycleDir, lifecycles[r.SessionID], r.SessionID, r.Name, nil)
		if err != nil {
			return Gathered{}, err
		}
		if ev != nil {
			named++
		}
	}
	if len(swept) > 0 || named > 0 {
		if lifecycles, err = lifecycle.LoadAll(lifecycleDir); err != nil {
			return Gathered{}, err
		}
	}

	var livePIDs []int
	for _, r := range records {
		if r.Live {
			livePIDs = append(livePIDs, r.PID)
		}
	}
	memory, err := src.Memory(livePIDs)
	if err != nil {
		return Gathered{}, err
	}
	undelivered, err := probe.UndeliveredCounts(probe.NewStore(src.Workspace.Shared().ProbesDir()))
	if err != nil {
		return Gathered{}, err
	}

	var rows []SessionRow
	registered := map[string]bool{}
	for _, pr := range found {
		record := pr.Record
		if record.SessionID != "" {
			registered[record.SessionID] = true
		}
		var rss *int64
		if record.Live {
			if kib, ok := memory[record.PID]; ok {
				rss = &kib
			}
		}
		row, err := rowForRecord(pr, lifecycles[record.SessionID], rss, src.Identity, nowMS, undelivered[record.SessionID])
		if err != nil {
			return Gathered{}, err
		}
		rows = append(rows, row)
	}
	ids := make([]string, 0, len(lifecycles))
	for id := range lifecycles {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if registered[id] {
			continue
		}
		row, err := rowForLifecycle(lifecycles[id], profiles, nowMS, undelivered[id])
		if err != nil {
			return Gathered{}, err
		}
		rows = append(rows, row)
	}
	return Gathered{Rows: SortRows(rows), Swept: len(swept), Named: named}, nil
}

// crashedRecords are the registry files the prune key offers, from the rows
// on screen.
func crashedRecords(rows []SessionRow) []sessions.SessionRecord {
	var out []sessions.SessionRecord
	for _, row := range rows {
		if row.State == lifecycle.StateCrashed && row.Record != nil {
			out = append(out, *row.Record)
		}
	}
	return out
}

// refocus places the focus after a re-gather by session id rather than
// index, since rows come and go between gathers; a session no longer listed
// falls back to the clamped index.
func refocus(rows []SessionRow, session string, focus int) int {
	if len(rows) == 0 {
		return -1
	}
	if session != "" {
		for i, row := range rows {
			if row.Session == session {
				return i
			}
		}
	}
	return max(0, min(len(rows)-1, focus))
}

// rowKey identifies row across a re-gather: its session id, else the path
// of the registry file it was read from, else nothing ("").
func rowKey(row SessionRow) string {
	if row.Session != "" {
		return row.Session
	}
	if row.Record != nil {
		return row.Record.Path
	}
	return ""
}

// reExpand finds the row the details belong to after a re-gather, by the
// same key the focus is kept by; a session no longer listed collapses (-1).
func reExpand(rows []SessionRow, key string) int {
	if key == "" {
		return -1
	}
	for i, row := range rows {
		if rowKey(row) == key {
			return i
		}
	}
	return -1
}

// pruneConfirmation asks before the crashed rows' registry files are
// deleted, listing as many of them as fit on the page.
func pruneConfirmation(crashed []sessions.SessionRecord, rows int, home string) widgets.Confirmation {
	lines := []string{fmt.Sprintf("%d registry file(s) of crashed sessions, whose processes are gone:", len(crashed)), ""}
	room := max(1, rows-6)
	for i, r := range crashed {
		if i == room-1 && len(crashed) > room {
			lines = append(lines, fmt.Sprintf("… and %d more", len(crashed)-i))
			break
		}
		lines = append(lines, fmt.Sprintf("pid %d · %s · %s", r.PID, firstNonEmpty(r.Name, Unnamed), Tildify(r.Path, home)))
	}
	return widgets.Confirmation{
		Title:   "Prune crashed session records?",
		Lines:   lines,
		Accept:  "delete these registry files",
		Decline: "keep them",
		Skip:    "back to the table",
	}
}

// overview is the state of one RunOverview.
type overview struct {
	fx      *effects.FX
	t       *terminal.Terminal
	colors  widgets.Colors
	src     Sources
	now     int64
	rows    []SessionRow
	focus   int
	expand  int
	hscroll int
	showAll bool
	mark    bool
	message string
	window  int
	outcome Outcome
}

func (o *overview) visible() []SessionRow {
	return VisibleRows(o.rows, o.showAll)
}

func (o *overview) focused() *SessionRow {
	shown := o.visible()
	if o.focus >= 0 && o.focus < len(shown) {
		return &shown[o.focus]
	}
	return nil
}

func (o *overview) focusedSession() string {
	if row := o.focused(); row != nil {
		return row.Session
	}
	return ""
}

func (o *overview) render() error {
	rows, cols := o.t.Rows, o.t.Cols
	frame, err := Layout(TableSpec{
		Rows:     o.rows,
		Focus:    o.focus,
		Expanded: o.expand,
		Height:   rows,
		Width:    cols,
		HScroll:  o.hscroll,
		NowMS:    o.now,
		Home:     o.src.Home,
		ShowAll:  o.showAll,
	})
	if err != nil {
		return err
	}
	o.hscroll = frame.HScroll
	o.window = max(1, frame.Window)
	footer := hintDefault
	switch {
	case o.message != "":
		footer = o.message
	case o.mark:
		footer = hintMark
	case o.showAll:
		footer = hintShowAll
	}
	return Draw(o.t, o.colors, frame, footer, o.message != "", rows, cols)
}

func (o *overview) regather() error {
	before := o.visible()
	keep := o.focusedSession()
	openOn := ""
	if o.expand >= 0 && o.expand < len(before) {
		openOn = rowKey(before[o.expand])
	}
	o.now = o.src.Clock()
	g, err := Gather(o.fx, o.src, o.now)
	if err != nil {
		return err
	}
	o.rows = g.Rows
	o.outcome.Swept += g.Swept
	shown := o.visible()
	o.focus = refocus(shown, keep, o.focus)
	o.expand = reExpand(shown, openOn)
	return nil
}

// markFocused handles the key after m: it records the user's mark on the
// focused session, or does nothing for a key that is not a mark key.
func (o *overview) markFocused(key terminal.Key) error {
	state, ok := markStates()[key]
	row := o.focused()
	if !ok || row == nil || row.Session == "" {
		return nil
	}
	name := row.Name
	if _, err := lifecycle.AppendEvent(o.fx, o.src.Workspace.Shared().LifecycleDir(), lifecycle.MarkEvent{
		Header: lifecycle.Header{Session: row.Session, Source: "user"},
		State:  state,
		Note:   nil,
	}); err != nil {
		return err
	}
	o.outcome.Marked++
	if err := o.regather(); err != nil {
		return err
	}
	if state != nil {
		o.message = fmt.Sprintf("Marked %s %s", name, *state)
	} else {
		o.message = "Cleared mark on " + name
	}
	return nil
}

// prune asks, through the confirm component, before deleting the registry
// files of the crashed rows on screen.
func (o *overview) prune(ctx context.Context) error {
	crashed := crashedRecords(o.visible())
	if len(crashed) == 0 {
		o.message = "No crashed records to prune"
		return nil
	}
	answer, err := widgets.Confirm(ctx, o.t, o.colors, pruneConfirmation(crashed, o.t.Rows, o.src.Home))
	if err != nil {
		return err
	}
	switch answer {
	case widgets.Accept:
	case widgets.Decline:
		o.message = "Pruned nothing"
		return nil
	default:
		return nil
	}
	gone, err := sessions.Prune(o.fx, crashed)
	o.outcome.Pruned = append(o.outcome.Pruned, gone...)
	if err != nil {
		return err
	}
	if err := o.regather(); err != nil {
		return err
	}
	o.message = fmt.Sprintf("Pruned %d crashed record(s)", len(gone))
	return nil
}

// RunOverview shows every Claude Code session on the machine until the user
// leaves, and reports what changed. The terminal must already be in cbreak
// mode; restoring it is the caller's.
//
// Up and Down move the focus, Page Up and Page Down by a window, Home and
// End to the ends, Left and Right scroll the columns, Enter expands the
// focused row into its details, a reveals what is finished, p prunes the
// registry files of the crashed rows after a confirmation, r gathers again,
// and m enters mark mode, where one more key records the user's own word
// about the session (h on hold, b blocked, d done, c cleared). Escape, q,
// and a Ctrl-C byte close it; every other key is ignored.
//
// The machine is gathered when the screen opens and again only on r or a
// key that changed it (a prune, a mark): a snapshot, never a poll, so
// nothing renumbers rows under the cursor. The frame is rebuilt at the
// terminal's size on every draw, and a resize redraws it.
//
// When ctx is done it returns the cancellation cause with what changed so
// far; any other error also returns what changed so far.
func RunOverview(ctx context.Context, fx *effects.FX, t *terminal.Terminal, c widgets.Colors, src Sources) (Outcome, error) {
	if !t.Raw() {
		return Outcome{}, errors.New("the terminal is not in cbreak mode: enter it (Terminal.EnterRaw) before showing the sessions overview")
	}
	if src.Clock == nil || src.Memory == nil {
		return Outcome{}, errors.New("the sessions overview needs a clock and a memory reader")
	}
	o := &overview{fx: fx, t: t, colors: c, src: src, expand: -1, window: 1}
	o.now = src.Clock()
	g, err := Gather(fx, src, o.now)
	if err != nil {
		return o.outcome, err
	}
	o.rows = g.Rows
	o.outcome.Swept = g.Swept

	for {
		if err := o.render(); err != nil {
			return o.outcome, err
		}
		key, err := t.ReadKey(ctx)
		if err != nil {
			return o.outcome, err
		}
		if key == terminal.KeyResize || key == terminal.KeyThemeDark || key == terminal.KeyThemeLight {
			continue
		}
		o.message = ""
		count := len(o.visible())

		if o.mark {
			o.mark = false
			if err := o.markFocused(key); err != nil {
				return o.outcome, err
			}
			continue
		}

		page := max(1, o.window-1)
		switch key {
		case terminal.KeyEsc, terminal.KeyCtrlC, "q", "Q":
			return o.outcome, nil
		case terminal.KeyDown:
			o.focus = MoveFocus(o.focus, count, 1)
		case terminal.KeyUp:
			o.focus = MoveFocus(o.focus, count, -1)
		case terminal.KeyPageDown:
			o.focus = MoveFocus(o.focus, count, page)
		case terminal.KeyPageUp:
			o.focus = MoveFocus(o.focus, count, -page)
		case terminal.KeyHome:
			o.focus = MoveFocus(o.focus, count, -count)
		case terminal.KeyEnd:
			o.focus = MoveFocus(o.focus, count, count)
		case terminal.KeyLeft:
			o.hscroll = max(0, o.hscroll-HScrollStep)
		case terminal.KeyRight:
			o.hscroll += HScrollStep
		case terminal.KeyEnter:
			if o.expand == o.focus {
				o.expand = -1
			} else {
				o.expand = o.focus
			}
		case "a", "A":
			keep := o.focusedSession()
			o.showAll = !o.showAll
			o.focus = refocus(o.visible(), keep, o.focus)
			o.expand = -1
		case "r", "R":
			if err := o.regather(); err != nil {
				return o.outcome, err
			}
		case "p", "P":
			if err := o.prune(ctx); err != nil {
				return o.outcome, err
			}
		case "m", "M":
			if row := o.focused(); row == nil || row.Session == "" {
				o.message = "No session id; cannot mark"
			} else {
				o.mark = true
			}
		}
	}
}
