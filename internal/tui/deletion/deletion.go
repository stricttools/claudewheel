// Package deletion is the deletion checklist: it presents every live process
// holding a profile and stops the ones the user ticks.
//
// Deleting a profile directory does not free it. Every remaining Claude Code
// process still carries CLAUDE_CONFIG_DIR pointing at the removed path, and
// any invocation of the client, a read-only status query included, recreates
// its configuration directory first, so the deleted profile reappears as a
// husk. That is why deletion asks first: it shows every live process
// registered under the profile and lets the user decide, row by row, which
// ones to stop.
//
// The daemon and its workers are ticked when the screen opens, and nothing
// else: they exist because the profile was launched, while a background job
// is work someone started on purpose, so it is listed but never pre-selected.
// Nothing is stopped without a tick.
//
// The screen does not close on confirmation: each ticked row's state line
// goes from a green "running" to a red "stopped" as its process goes, and a
// key leaves afterwards.
//
// The caller stops holders before removing the directory, never the reverse:
// the daemon-stop command is itself an invocation of the client, so run
// against an already-deleted profile it recreates the directory it was asked
// to shut down. "Stop the thing, then delete its home" reads like an
// optimization, but the order is required.
package deletion

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/sessionsview"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// PreTickedCategories returns the categories ticked when the checklist
// opens: claudewheel's own daemon and the workers it supervises.
func PreTickedCategories() []string {
	return []string{sessions.CategoryDaemon, sessions.CategoryDaemonWorker}
}

// The states a row's indicator reads.
const (
	StateRunning  = "running"
	StateStopping = "stopping..."
	StateStopped  = "stopped"
)

// hintDone is the finished screen's hint: any key leaves it.
const hintDone = "any key: continue"

// hintSelect is the selecting screen's hint: the movement keys, then the
// confirmation answers in the shared y/n/esc spelling.
func hintSelect() string {
	return "up/down: move   space: toggle   " +
		widgets.ConfirmHint("stop the ticked ones", "cancel the deletion", "cancel the deletion")
}

// Holder is one live process holding the profile, and what the screen knows
// about it.
type Holder struct {
	Record sessions.SessionRecord
	Ticked bool
	// State and StateStyle are the row's indicator line and its color.
	State      string
	StateStyle sessionsview.ListStyle
	// RSSKiB is the measured resident memory, nil when ps did not report
	// the pid.
	RSSKiB *int64
}

// block is the holder as a row of the shared session list.
func (h Holder) block() sessionsview.SessionBlock {
	selection := sessionsview.Unselected
	if h.Ticked {
		selection = sessionsview.Selected
	}
	state := h.State
	return sessionsview.SessionBlock{
		Record:     h.Record,
		Selection:  selection,
		State:      &state,
		StateStyle: h.StateStyle,
		RSSKiB:     h.RSSKiB,
	}
}

// StopFailure is a ticked holder whose stop did not take, and why.
type StopFailure struct {
	Record sessions.SessionRecord
	// Err is the error the stop returned, nil when the stop ran and the
	// process did not go (a daemon-stop exiting nonzero, or a process still
	// up when the exit wait ended).
	Err error
}

// Outcome is what the screen decided and what it managed to stop.
type Outcome struct {
	// Answer is the confirmation answer: Accept stopped the ticked rows,
	// Decline and Skip stopped nothing.
	Answer widgets.Answer
	// Stopped are the holders whose processes went.
	Stopped []sessions.SessionRecord
	// Failed are the ticked holders whose stop did not take.
	Failed []StopFailure
	// StillHolding is every holder whose process is still up when the screen
	// closes, probed again rather than derived from the snapshot, so a holder
	// that exited on its own while the screen was open is not in it. It lets
	// the deletion say the directory may come back instead of claiming it is
	// gone.
	StillHolding []sessions.SessionRecord
}

// Confirmed reports whether the user accepted, so the deletion goes ahead.
func (o Outcome) Confirmed() bool {
	return o.Answer == widgets.Accept
}

// GatherHolders returns every live process registered under configDir, with
// its resident memory measured for all of them in one ps call.
func GatherHolders(fx *effects.FX, configDir string) ([]Holder, error) {
	records := sessions.LiveRecords(configDir)
	pids := make([]int, len(records))
	for i, r := range records {
		pids[i] = r.PID
	}
	memory, err := profiles.ResidentMemory(fx, pids)
	if err != nil {
		return nil, err
	}
	preTicked := map[string]bool{}
	for _, c := range PreTickedCategories() {
		preTicked[c] = true
	}
	holders := make([]Holder, len(records))
	for i, r := range records {
		holders[i] = Holder{
			Record:     r,
			Ticked:     preTicked[r.Category],
			State:      StateRunning,
			StateStyle: sessionsview.ListRunning,
		}
		if rss, ok := memory[r.PID]; ok {
			v := int64(rss)
			holders[i].RSSKiB = &v
		}
	}
	return holders, nil
}

// StopOrder returns the indices of the ticked holders, supervisor first: a
// worker stopped before its supervisor can simply be put back, while
// stopping the supervisor first makes the rest stay stopped.
func StopOrder(holders []Holder) []int {
	var daemons, others []int
	for i, h := range holders {
		if !h.Ticked {
			continue
		}
		if h.Record.Category == sessions.CategoryDaemon {
			daemons = append(daemons, i)
		} else {
			others = append(others, i)
		}
	}
	return append(daemons, others...)
}

// stillTheRegisteredProcess reports whether the record's pid still names the
// process that registered it. The checklist gathers its holders once and
// then waits on a human, so every pid is a snapshot of unbounded age by the
// time anything acts on it, and pids are recycled: this compares the kernel
// start token the record holds with the one the pid carries now.
func stillTheRegisteredProcess(r sessions.SessionRecord) bool {
	return sessions.IsLive(r.PID, r.ProcStart)
}

// stopper stops one holder by the mechanism its category calls for. The
// daemon-stop command is issued at most once however many daemon rows are
// ticked: it shuts the supervisor down, and a second invocation would only
// recreate the config directory it was pointed at.
type stopper struct {
	fx        *effects.FX
	binary    string
	configDir string
	environ   []string
	// daemonDone and daemonStopped remember the one daemon-stop's answer.
	daemonDone    bool
	daemonStopped bool
	daemonErr     error
}

// stop stops h and waits for it to go, reporting whether it went.
//
// Identity is checked again just before anything is signalled, and on every
// poll of the wait: a pid whose start token no longer matches belongs to
// another process now, so the one the row names is already gone. That counts
// as stopped, and nothing is signalled, because the signal would reach a
// stranger.
//
// A preview issues the stop, so the dry-run record gets it, but does not wait:
// the signal was recorded rather than sent, so the process is still there.
func (s *stopper) stop(ctx context.Context, h Holder) (bool, error) {
	if !stillTheRegisteredProcess(h.Record) {
		return true, nil
	}
	if h.Record.Category == sessions.CategoryDaemon {
		if !s.daemonDone {
			s.daemonDone = true
			s.daemonStopped, s.daemonErr = profiles.StopDaemon(s.fx, s.binary, s.configDir, s.environ)
		}
		if s.daemonErr != nil {
			return false, s.daemonErr
		}
		if !s.daemonStopped {
			return false, nil
		}
	} else if err := profiles.Terminate(s.fx, h.Record.PID); err != nil {
		return false, err
	}
	if s.fx.Previewing() {
		return true, nil
	}
	return waitGone(ctx, h.Record)
}

// waitGone polls until the record's process is gone, reporting false when it
// is still there after profiles.ExitTimeout. When ctx is done it returns the
// cancellation cause.
func waitGone(ctx context.Context, r sessions.SessionRecord) (bool, error) {
	deadline := time.Now().Add(profiles.ExitTimeout)
	ticker := time.NewTicker(profiles.ExitPoll)
	defer ticker.Stop()
	for {
		if !stillTheRegisteredProcess(r) {
			return true, nil
		}
		if !time.Now().Before(deadline) {
			return false, nil
		}
		select {
		case <-ctx.Done():
			return false, context.Cause(ctx)
		case <-ticker.C:
		}
	}
}

// Checklist is what the screen is about.
type Checklist struct {
	ProfileName string
	// ConfigDir is the profile's config directory, which the daemon-stop
	// command addresses.
	ConfigDir string
	// Binary is the Claude Code binary the daemon-stop command runs.
	Binary string
	// Environ is the environment (os.Environ form) the daemon-stop command
	// inherits, with CLAUDE_CONFIG_DIR forced to ConfigDir.
	Environ []string
	NowMS   int64
	// Identity marks the session this runs in, nil for none.
	Identity *sessionsview.Identity
}

// screen is the state of one checklist run.
type screen struct {
	t       *terminal.Terminal
	colors  widgets.Colors
	spec    Checklist
	holders []Holder
	focus   int
	hint    string
}

func (s *screen) render() error {
	blocks := make([]sessionsview.SessionBlock, len(s.holders))
	for i, h := range s.holders {
		blocks[i] = h.block()
	}
	frame, err := sessionsview.BuildFrame(sessionsview.ListSpec{
		Blocks:    blocks,
		Focus:     s.focus,
		NowMS:     s.spec.NowMS,
		Title:     fmt.Sprintf("Processes holding '%s'", s.spec.ProfileName),
		Hint:      s.hint,
		Height:    s.t.Rows,
		Width:     max(1, s.t.Cols-2),
		Identity:  s.spec.Identity,
		EmptyText: "Nothing holds this profile.",
	})
	if err != nil {
		return err
	}
	return sessionsview.RenderFrame(s.t, s.colors, frame, 2)
}

// Run runs the checklist over holders and returns what it decided. The
// terminal must already be in cbreak mode; restoring it is the caller's.
//
// While selecting, Up and Down move and Space toggles; the answer follows
// the confirmation keys (widgets.ConfirmAnswer): y stops the ticked rows, n
// and Escape cancel the deletion and stop nothing, and Enter does nothing.
// After y the screen stays, each ticked row is stopped in turn with its
// state line redrawn as it goes, and the screen then waits for any key.
//
// When ctx is done it returns the cancellation cause, with the outcome so
// far when stopping had begun. An error from a stop does not end the run: the
// row goes back to running and the error is in Outcome.Failed.
func Run(ctx context.Context, fx *effects.FX, t *terminal.Terminal, c widgets.Colors, holders []Holder, spec Checklist) (Outcome, error) {
	if !t.Raw() {
		return Outcome{}, errors.New("the terminal is not in cbreak mode: enter it (Terminal.EnterRaw) before showing the deletion checklist")
	}
	s := &screen{t: t, colors: c, spec: spec, holders: holders, focus: -1, hint: hintSelect()}
	if len(holders) > 0 {
		s.focus = 0
	}

	answer, err := s.choose(ctx)
	if err != nil {
		return Outcome{}, err
	}
	out := Outcome{Answer: answer}
	if answer != widgets.Accept {
		return out, nil
	}

	st := &stopper{fx: fx, binary: spec.Binary, configDir: spec.ConfigDir, environ: spec.Environ}
	stopped := map[int]bool{}
	for _, i := range StopOrder(holders) {
		h := &holders[i]
		h.State = StateStopping
		if err := s.render(); err != nil {
			return out, err
		}
		went, stopErr := st.stop(ctx, *h)
		switch {
		case went:
			h.State, h.StateStyle = StateStopped, sessionsview.ListStopped
			stopped[h.Record.PID] = true
			out.Stopped = append(out.Stopped, h.Record)
		case ctx.Err() != nil:
			h.State, h.StateStyle = StateRunning, sessionsview.ListRunning
		default:
			h.State, h.StateStyle = StateRunning, sessionsview.ListRunning
			out.Failed = append(out.Failed, StopFailure{Record: h.Record, Err: stopErr})
		}
		if ctx.Err() != nil {
			out.StillHolding = stillHolding(holders, stopped)
			return out, context.Cause(ctx)
		}
		if err := s.render(); err != nil {
			return out, err
		}
	}
	out.StillHolding = stillHolding(holders, stopped)

	s.hint = hintDone
	for {
		if err := s.render(); err != nil {
			return out, err
		}
		key, err := t.ReadKey(ctx)
		if err != nil {
			return out, err
		}
		if key == terminal.KeyResize || key == terminal.KeyThemeDark || key == terminal.KeyThemeLight {
			continue
		}
		return out, nil
	}
}

// choose runs the selecting key loop until the user answers.
func (s *screen) choose(ctx context.Context) (widgets.Answer, error) {
	for {
		if err := s.render(); err != nil {
			return 0, err
		}
		key, err := s.t.ReadKey(ctx)
		if err != nil {
			return 0, err
		}
		if answer, ok := widgets.ConfirmAnswer(key); ok {
			return answer, nil
		}
		switch key {
		case terminal.KeyDown:
			s.focus = sessionsview.MoveFocus(s.focus, len(s.holders), 1)
		case terminal.KeyUp:
			s.focus = sessionsview.MoveFocus(s.focus, len(s.holders), -1)
		case " ":
			if s.focus >= 0 && s.focus < len(s.holders) {
				s.holders[s.focus].Ticked = !s.holders[s.focus].Ticked
			}
		}
	}
}

// stillHolding probes every holder not stopped again: one nobody ticked may
// have exited on its own while the screen was open, and reporting it would
// have the deletion warn about a process that is no longer there.
func stillHolding(holders []Holder, stopped map[int]bool) []sessions.SessionRecord {
	var still []sessions.SessionRecord
	for _, h := range holders {
		if !stopped[h.Record.PID] && stillTheRegisteredProcess(h.Record) {
			still = append(still, h.Record)
		}
	}
	return still
}
