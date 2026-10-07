// Package lifecycle is the per-session lifecycle store: what happened to one
// Claude Code session.
//
// Claude Code writes a per-session registry entry only while its process
// lives, and says nothing once the process is gone. This package owns the
// record that remains: an append-only JSONL file per session,
// shared/lifecycle/<session-uuid>.jsonl, one line per event.
//
// Events are started (a session began), ended (it stopped, by exiting or by
// dying), named (it carried a display name), mark (the user marked it, or
// cleared the mark), and moved (the user moved it to another directory's
// session store). Events are never edited or deleted: a mark is removed by
// appending one whose state is null, and a session that starts again gets a
// second started line.
//
// The line shape belongs to .strictspec/lifecycle-event.schema.toml and its
// generated validator. This package keeps what the schema cannot see: ordering
// events by their fixed-width UTC timestamps, the latest-wins reading of a file
// (Summarize), and the state a reader derives from a lifecycle plus what it
// observes about the process (DeriveState).
//
// One damage is tolerated: a final line with no terminating newline that does
// not parse as JSON is an interrupted write, and ReadSession drops it. Every
// other unreadable line is an *Error naming the file and the 1-based line.
// Every write goes through an *effects.FX.
package lifecycle

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/schema/lifecycleevent"
)

// FormatVersion is the per-line format_version every line written here
// carries, and the only one read.
const FormatVersion = 1

// SessionUUIDPattern is the spelling of a Claude Code session uuid: lowercase
// 8-4-4-4-12 hex. A session string also becomes a file name, so it is matched
// before any path is built from it.
const SessionUUIDPattern = `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`

// SessionUUIDRE matches SessionUUIDPattern.
var SessionUUIDRE = regexp.MustCompile(SessionUUIDPattern)

// SweepGraceMS is how long after a started line the observation pass leaves a
// session alone: the line is written by a SessionStart hook before Claude
// Code registers its process, so an immediate sweep would call a launching
// session crashed.
const SweepGraceMS = 60_000

// The states DeriveState returns.
const (
	StateWorking    = "working"
	StateShell      = "shell"
	StateIdle       = "idle"
	StateWaiting    = "waiting"
	StateRunning    = "running"
	StateUnverified = "unverified"
	StateStarting   = "starting"
	StateOnHold     = "on-hold"
	StateBlocked    = "blocked"
	StateDone       = "done"
	StateCrashed    = "crashed"
	StateExited     = "exited"
)

// States returns every state DeriveState can return, in display order. The
// three sets below partition it.
func States() []string {
	return []string{
		StateWorking, StateShell, StateIdle, StateWaiting, StateRunning, StateUnverified,
		StateStarting, StateOnHold, StateBlocked, StateDone, StateCrashed, StateExited,
	}
}

// LiveStates returns the states a reader shows as running sessions.
func LiveStates() map[string]bool {
	return setOf(StateWorking, StateShell, StateIdle, StateWaiting, StateRunning, StateUnverified)
}

// LooseEndStates returns the states a reader shows as wanting attention.
func LooseEndStates() map[string]bool {
	return setOf(StateStarting, StateOnHold, StateBlocked, StateCrashed)
}

// HiddenByDefaultStates returns the states a reader hides until asked.
func HiddenByDefaultStates() map[string]bool {
	return setOf(StateDone, StateExited)
}

func setOf(items ...string) map[string]bool {
	out := make(map[string]bool, len(items))
	for _, item := range items {
		out[item] = true
	}
	return out
}

// liveStatusStates maps the status values Claude Code's registry writes to
// the state a live session is shown in. An unknown status reads as running.
var liveStatusStates = map[string]string{
	"busy":    StateWorking,
	"shell":   StateShell,
	"idle":    StateIdle,
	"waiting": StateWaiting,
}

// Error is a lifecycle file, or a line in one, that cannot be read or
// written.
type Error struct {
	msg string
}

func (e *Error) Error() string { return e.msg }

func errorf(format string, args ...any) error {
	return &Error{msg: fmt.Sprintf(format, args...)}
}

// Header holds the fields every event carries, in schema order. ID and At may
// be left empty when building an event: AppendEvent stamps them.
type Header struct {
	ID      string `json:"id"`
	At      string `json:"at"`
	Session string `json:"session"`
	Source  string `json:"source"`
}

// Head returns the common fields of an event.
func (h Header) Head() Header { return h }

// Event is one lifecycle line: a StartedEvent, EndedEvent, NamedEvent,
// MarkEvent, or MovedEvent. Every nullable field is a pointer, and every
// field is written, a nil one as null.
type Event interface {
	Head() Header
	// EventType is the discriminator value the line carries.
	EventType() string
	withHead(h Header) Event
}

// StartedEvent records that a session began running, under a stated config
// directory and working directory.
type StartedEvent struct {
	Header
	Cwd           string  `json:"cwd"`
	ConfigDir     string  `json:"config_dir"`
	Profile       *string `json:"profile"`
	ClaudeVersion *string `json:"claude_version"`
	Model         *string `json:"model"`
	Permissions   *string `json:"permissions"`
	Entry         string  `json:"entry"`
	Transcript    *string `json:"transcript"`
	PID           *int64  `json:"pid"`
}

// EndedEvent records that a session stopped running, by exiting or by dying.
type EndedEvent struct {
	Header
	Outcome string  `json:"outcome"`
	Reason  *string `json:"reason"`
	Detail  *string `json:"detail"`
}

// NamedEvent records that a session carried a display name.
type NamedEvent struct {
	Header
	Name       string  `json:"name"`
	NameSource *string `json:"name_source"`
}

// MarkEvent records that the user marked the session; a nil State clears the
// mark in force.
type MarkEvent struct {
	Header
	State *string `json:"state"`
	Note  *string `json:"note"`
}

// MovedEvent records that the user moved the session to another directory's
// session store.
type MovedEvent struct {
	Header
	OldCwd        *string `json:"old_cwd"`
	NewCwd        string  `json:"new_cwd"`
	OldTranscript string  `json:"old_transcript"`
	NewTranscript string  `json:"new_transcript"`
}

// EventType implements Event.
func (StartedEvent) EventType() string { return "started" }

// EventType implements Event.
func (EndedEvent) EventType() string { return "ended" }

// EventType implements Event.
func (NamedEvent) EventType() string { return "named" }

// EventType implements Event.
func (MarkEvent) EventType() string { return "mark" }

// EventType implements Event.
func (MovedEvent) EventType() string { return "moved" }

func (e StartedEvent) withHead(h Header) Event { e.Header = h; return e }
func (e EndedEvent) withHead(h Header) Event   { e.Header = h; return e }
func (e NamedEvent) withHead(h Header) Event   { e.Header = h; return e }
func (e MarkEvent) withHead(h Header) Event    { e.Header = h; return e }
func (e MovedEvent) withHead(h Header) Event   { e.Header = h; return e }

// discriminatorKey is the JSON key that selects an event's type.
const discriminatorKey = "kind"

// commonKeys are the Header's keys, written before the discriminator.
var commonKeys = []string{"id", "at", "session", "source"}

// NewUUID4Hex returns the 32 hex digits of a random version 4 UUID, as
// Python's uuid.uuid4().hex spells it.
func NewUUID4Hex() string {
	var b [16]byte
	// crypto/rand.Read never returns an error; it crashes the program instead.
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return hex.EncodeToString(b[:])
}

// NewEventID generates a unique event id: 16 hex digits of the nanosecond
// clock, then a uuid4's 32 hex digits. Ids sort by creation order.
func NewEventID() string {
	return fmt.Sprintf("%016x", time.Now().UnixNano()) + NewUUID4Hex()
}

// NowMS returns the wall clock in milliseconds since the epoch.
func NowMS() int64 {
	return time.Now().UnixMilli()
}

// TimestampAt renders ms as RFC 3339 UTC with three fractional digits. The
// one fixed-width spelling makes lexical order over these timestamps time
// order, which Summarize sorts by.
func TimestampAt(ms int64) string {
	return time.UnixMilli(ms).UTC().Format("2006-01-02T15:04:05.000Z")
}

// NowTimestamp renders the current time as TimestampAt does.
func NowTimestamp() string {
	return TimestampAt(NowMS())
}

// ParseTimestampMS reads an at timestamp back into milliseconds since the
// epoch. Any RFC 3339 offset is accepted, Z or +00:00 alike.
func ParseTimestampMS(at string) (int64, error) {
	stamp, err := time.Parse(time.RFC3339Nano, at)
	if err != nil {
		return 0, errorf("not an RFC 3339 timestamp: '%s'", at)
	}
	return stamp.UnixMilli(), nil
}

// SessionFile returns the lifecycle file of session under dir. The session is
// matched against SessionUUIDRE first, so no path is built out of junk.
func SessionFile(dir, session string) (string, error) {
	if !SessionUUIDRE.MatchString(session) {
		return "", fmt.Errorf("not a Claude Code session uuid: '%s' (lowercase 8-4-4-4-12 hex expected)", session)
	}
	return filepath.Join(dir, session+".jsonl"), nil
}

// EventToJSON serializes ev as one compact ASCII JSON line without a newline.
// Keys come out in schema order: format_version, the common fields, the
// discriminator, then the event's own fields, every one of them written.
func EventToJSON(ev Event) ([]byte, error) {
	tree, err := jsonfile.Normalize(ev)
	if err != nil {
		return nil, err
	}
	fields, ok := tree.(*jsonfile.Object)
	if !ok {
		return nil, fmt.Errorf("lifecycle event %T is not a JSON object", ev)
	}
	out := jsonfile.NewObject()
	out.Set("format_version", json.Number(strconv.Itoa(FormatVersion)))
	common := map[string]bool{}
	for _, key := range commonKeys {
		value, _ := fields.Get(key)
		out.Set(key, value)
		common[key] = true
	}
	out.Set(discriminatorKey, ev.EventType())
	for _, key := range fields.Keys() {
		if !common[key] {
			value, _ := fields.Get(key)
			out.Set(key, value)
		}
	}
	return jsonfile.MarshalCompactASCII(out)
}

// where is the file:line prefix of every diagnostic; lineno 0 means no line.
func where(path string, lineno int) string {
	if lineno == 0 {
		return path
	}
	return fmt.Sprintf("%s:%d", path, lineno)
}

// validateLine runs the schema validation, the format_version check first.
func validateLine(line []byte, at string) error {
	_, diags := lifecycleevent.ValidateBytes(line, "jsonl")
	if len(diags) > 0 {
		return errorf("%s: %s", at, joinDiagnostics(diags))
	}
	return nil
}

func joinDiagnostics(diags []strictspec.Diagnostic) string {
	messages := make([]string, len(diags))
	for i, d := range diags {
		messages[i] = d.Message
	}
	return strings.Join(messages, "; ")
}

// ParseEvent parses one JSON line into the event its discriminator selects.
// path and lineno name the line in every diagnostic.
func ParseEvent(line []byte, path string, lineno int) (Event, error) {
	tree, err := jsonfile.Decode(line)
	if err != nil {
		return nil, errorf("%s: malformed JSON: %v", where(path, lineno), err)
	}
	obj, ok := tree.(*jsonfile.Object)
	if !ok {
		return nil, errorf("%s: line is not a JSON object", where(path, lineno))
	}
	if err := validateLine(line, where(path, lineno)); err != nil {
		return nil, err
	}
	discriminator, _ := obj.Get(discriminatorKey)
	var ev Event
	switch discriminator {
	case "started":
		ev, err = decodeAs[StartedEvent](line)
	case "ended":
		ev, err = decodeAs[EndedEvent](line)
	case "named":
		ev, err = decodeAs[NamedEvent](line)
	case "mark":
		ev, err = decodeAs[MarkEvent](line)
	case "moved":
		ev, err = decodeAs[MovedEvent](line)
	default:
		return nil, errorf("%s: no lifecycle event '%v'", where(path, lineno), discriminator)
	}
	if err != nil {
		return nil, errorf("%s: %v", where(path, lineno), err)
	}
	return ev, nil
}

// decodeAs decodes a validated line into the event E; the format_version and
// discriminator keys have no field and are skipped.
func decodeAs[E Event](line []byte) (Event, error) {
	var e E
	if err := json.Unmarshal(line, &e); err != nil {
		return nil, err
	}
	return e, nil
}

// splitLines splits text as the Python readers did: on "\n", dropping the
// empty string after a final newline. complete reports that final newline.
func splitLines(text string) (lines []string, complete bool) {
	lines = strings.Split(text, "\n")
	complete = strings.HasSuffix(text, "\n")
	if complete {
		lines = lines[:len(lines)-1]
	}
	return lines, complete
}

// ReadSession reads one session's lifecycle file in file order. An absent or
// empty file yields no events, and an interrupted final write is dropped.
func ReadSession(path string) ([]Event, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, errorf("%s: cannot be read: %v", path, err)
	}
	if !utf8.Valid(data) {
		return nil, errorf("%s: cannot be read: not valid UTF-8", path)
	}
	if len(data) == 0 {
		return nil, nil
	}
	lines, complete := splitLines(string(data))
	var events []Event
	for i, raw := range lines {
		lineno := i + 1
		stripped := strings.TrimSpace(raw)
		if stripped == "" {
			continue
		}
		if lineno == len(lines) && !complete {
			if _, err := jsonfile.Decode([]byte(stripped)); err != nil {
				break
			}
		}
		ev, err := ParseEvent([]byte(stripped), path, lineno)
		if err != nil {
			return nil, err
		}
		events = append(events, ev)
	}
	return events, nil
}

// NeedsSeparator reports whether path ends mid-line, so an append must lead
// with a newline. An absent file needs none.
func NeedsSeparator(path string) (bool, error) {
	f, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return false, err
	}
	if info.Size() == 0 {
		return false, nil
	}
	last := make([]byte, 1)
	if _, err := f.ReadAt(last, info.Size()-1); err != nil {
		return false, err
	}
	return last[0] != '\n', nil
}

// AppendLine appends line and a newline to path, creating its directory,
// leading with a newline when the file ends mid-line so the new line never
// joins a damaged one. Prior content is never rewritten. The probe store's
// JSONL files are appended the same way.
func AppendLine(fx *effects.FX, path string, line []byte) error {
	if err := fx.MkdirAll(filepath.Dir(path)); err != nil {
		return err
	}
	lead, err := NeedsSeparator(path)
	if err != nil {
		return err
	}
	var buf bytes.Buffer
	if lead {
		buf.WriteByte('\n')
	}
	buf.Write(line)
	buf.WriteByte('\n')
	return fx.AppendFile(path, buf.Bytes())
}

// AppendEvent appends ev to its session's file under dir and returns the copy
// as written, stamped with an id and an at when it carries neither. The event
// is validated before anything is written, so an invalid one leaves the file
// as it was.
func AppendEvent[E Event](fx *effects.FX, dir string, ev E) (E, error) {
	var zero E
	h := ev.Head()
	if h.ID == "" {
		h.ID = NewEventID()
	}
	if h.At == "" {
		h.At = NowTimestamp()
	}
	stamped, ok := ev.withHead(h).(E)
	if !ok {
		return zero, fmt.Errorf("lifecycle event %T changed type when stamped", ev)
	}
	path, err := SessionFile(dir, h.Session)
	if err != nil {
		return zero, err
	}
	line, err := EventToJSON(stamped)
	if err != nil {
		return zero, err
	}
	if err := validateLine(line, path); err != nil {
		return zero, err
	}
	if err := AppendLine(fx, path, line); err != nil {
		return zero, err
	}
	return stamped, nil
}
