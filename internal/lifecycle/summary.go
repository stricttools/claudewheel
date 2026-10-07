package lifecycle

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
)

// SessionLifecycle is what one session's whole file says, read latest-wins.
//
// Ended is the session's current end: an ended line older than the newest
// started line belongs to a previous run and is dropped, or a resumed session
// would read as dead. Mark is the mark in force: the newest mark line governs,
// and a null state on it clears the mark. Moved is the newest move when no
// started line is newer than it.
type SessionLifecycle struct {
	Session string
	Started *StartedEvent
	Ended   *EndedEvent
	Name    *NamedEvent
	Mark    *MarkEvent
	Moved   *MovedEvent
	LastAt  string
}

// Cwd is the session's directory: where it was moved, else where it started.
// ok is false when neither is recorded.
func (l *SessionLifecycle) Cwd() (cwd string, ok bool) {
	if l.Moved != nil {
		return l.Moved.NewCwd, true
	}
	if l.Started != nil {
		return l.Started.Cwd, true
	}
	return "", false
}

// Transcript is the session's transcript path: where it was moved, else where
// it started. ok is false when neither is recorded.
func (l *SessionLifecycle) Transcript() (path string, ok bool) {
	if l.Moved != nil {
		return l.Moved.NewTranscript, true
	}
	if l.Started != nil && l.Started.Transcript != nil {
		return *l.Started.Transcript, true
	}
	return "", false
}

// order places an event by its at timestamp, a tie broken by file position.
type order struct {
	at    string
	index int
}

func (o order) after(other order) bool {
	if o.at != other.at {
		return o.at > other.at
	}
	return o.index > other.index
}

// Summarize reduces one session's events to the picture a reader uses.
// Events are ordered by at, lexically, which is time order for these
// fixed-width UTC timestamps; a tie goes to the later line.
func Summarize(events []Event, session string) SessionLifecycle {
	out := SessionLifecycle{Session: session}
	none := order{at: "", index: -1}
	startedAt, endedAt, nameAt, markAt, movedAt := none, none, none, none, none
	for index, ev := range events {
		key := order{at: ev.Head().At, index: index}
		if key.at > out.LastAt {
			out.LastAt = key.at
		}
		switch e := ev.(type) {
		case StartedEvent:
			if key.after(startedAt) {
				out.Started, startedAt = &e, key
			}
		case EndedEvent:
			if key.after(endedAt) {
				out.Ended, endedAt = &e, key
			}
		case NamedEvent:
			if key.after(nameAt) {
				out.Name, nameAt = &e, key
			}
		case MarkEvent:
			if key.after(markAt) {
				out.Mark, markAt = &e, key
			}
		case MovedEvent:
			if key.after(movedAt) {
				out.Moved, movedAt = &e, key
			}
		}
	}
	if out.Ended != nil && out.Started != nil && startedAt.after(endedAt) {
		out.Ended = nil
	}
	if out.Mark != nil && out.Mark.State == nil {
		out.Mark = nil
	}
	if out.Moved != nil && out.Started != nil && startedAt.after(movedAt) {
		out.Moved = nil
	}
	return out
}

// LoadAll summarizes every session file in dir, keyed by session uuid. An
// absent directory yields nothing. Only *.jsonl files are read, and one whose
// name is not a session uuid is an error: nothing else writes here, so such a
// file is damage that reading around would hide.
func LoadAll(dir string) (map[string]*SessionLifecycle, error) {
	loaded := map[string]*SessionLifecycle{}
	paths, err := SessionFiles(dir)
	if err != nil {
		return nil, err
	}
	for _, path := range paths {
		session := strings.TrimSuffix(filepath.Base(path), ".jsonl")
		if !SessionUUIDRE.MatchString(session) {
			return nil, errorf("%s: file name is not a session uuid; the lifecycle store holds one <session-uuid>.jsonl per session and nothing else", path)
		}
		events, err := ReadSession(path)
		if err != nil {
			return nil, err
		}
		summary := Summarize(events, session)
		loaded[session] = &summary
	}
	return loaded, nil
}

// SessionFiles lists the *.jsonl files in dir, sorted by name, without
// checking their names. An absent directory, or a path that is not a
// directory, lists nothing.
func SessionFiles(dir string) ([]string, error) {
	info, err := os.Stat(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return nil, nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, entry := range entries {
		if matched, _ := filepath.Match("*.jsonl", entry.Name()); matched {
			out = append(out, filepath.Join(dir, entry.Name()))
		}
	}
	return out, nil
}

// SweepCrashed records an ended line for every session that died without
// writing one: it has a started line, no current end, is not in live, and
// started more than SweepGraceMS before nowMS. It returns the events as
// written, in session order. The next sweep reads them and passes by.
func SweepCrashed(fx *effects.FX, dir string, lifecycles map[string]*SessionLifecycle, live map[string]bool, nowMS int64) ([]EndedEvent, error) {
	cutoff := nowMS - SweepGraceMS
	sessions := make([]string, 0, len(lifecycles))
	for session := range lifecycles {
		sessions = append(sessions, session)
	}
	sort.Strings(sessions)
	var written []EndedEvent
	for _, session := range sessions {
		state := lifecycles[session]
		if state.Started == nil || state.Ended != nil || live[session] {
			continue
		}
		startedMS, err := ParseTimestampMS(state.Started.At)
		if err != nil {
			return written, err
		}
		if startedMS >= cutoff {
			continue
		}
		detail := "no live process and no SessionEnd recorded"
		ev, err := AppendEvent(fx, dir, EndedEvent{
			Header:  Header{Session: session, Source: "sweep"},
			Outcome: "crashed",
			Reason:  nil,
			Detail:  &detail,
		})
		if err != nil {
			return written, err
		}
		written = append(written, ev)
	}
	return written, nil
}

// CaptureName records name for session when it is new or has changed, since
// Claude Code's registry holds a display name only while the process lives.
// It returns the event written, or nil when there was nothing to record: no
// name, or the same name from the same source as last time. lc may be nil.
func CaptureName(fx *effects.FX, dir string, lc *SessionLifecycle, session, name string, nameSource *string) (*NamedEvent, error) {
	if name == "" {
		return nil, nil
	}
	if lc != nil && lc.Name != nil && lc.Name.Name == name && equalOptional(lc.Name.NameSource, nameSource) {
		return nil, nil
	}
	ev, err := AppendEvent(fx, dir, NamedEvent{
		Header:     Header{Session: session, Source: "sweep"},
		Name:       name,
		NameSource: nameSource,
	})
	if err != nil {
		return nil, err
	}
	return &ev, nil
}

func equalOptional(a, b *string) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

// Observed is what can be seen about a session's process right now, from
// Claude Code's registry checked against the kernel.
type Observed struct {
	// Live: a registry record names a running process.
	Live bool
	// Verified: the process's kernel start token matched the record.
	Verified bool
	// Status is the registry's status value; empty when it states none.
	Status string
	// RegistryPresent: a registry file exists for the session.
	RegistryPresent bool
}

// DeriveState decides the one state a session is shown in; the first rule
// that matches wins. Observation beats the record, because a live process is
// a fact no recorded line can contradict. lc may be nil.
//
//  1. live but unverified: unverified.
//  2. live: the registry's status as a state, or running for one not known.
//  3. a mark in force: its state.
//  4. a recorded end: its outcome, exited or crashed.
//  5. a registry file with no process behind it: crashed.
//  6. a started line with no end: starting inside SweepGraceMS, crashed after.
//  7. otherwise: exited.
func DeriveState(lc *SessionLifecycle, obs Observed, nowMS int64) (string, error) {
	if obs.Live && !obs.Verified {
		return StateUnverified, nil
	}
	if obs.Live {
		if state, ok := liveStatusStates[obs.Status]; ok {
			return state, nil
		}
		return StateRunning, nil
	}
	if lc != nil && lc.Mark != nil {
		if lc.Mark.State == nil {
			return "", errorf("session %s: the mark in force has no state", lc.Session)
		}
		return *lc.Mark.State, nil
	}
	if lc != nil && lc.Ended != nil {
		return lc.Ended.Outcome, nil
	}
	if obs.RegistryPresent {
		return StateCrashed, nil
	}
	if lc != nil && lc.Started != nil {
		startedMS, err := ParseTimestampMS(lc.Started.At)
		if err != nil {
			return "", err
		}
		if startedMS >= nowMS-SweepGraceMS {
			return StateStarting, nil
		}
		return StateCrashed, nil
	}
	return StateExited, nil
}
