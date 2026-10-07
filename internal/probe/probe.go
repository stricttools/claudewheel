// Package probe owns the probe store under shared/probes: probes that watch
// Claude Code sessions for OOM kills and report them to the sessions
// subscribed, and the reports every session gets of its own commands' kills.
//
// The store's layout:
//
//   - probes/<probe-id>.jsonl: one probe's log (probe-event schema)
//   - kills.jsonl: every kill the runner read (oom-kill-event schema)
//   - sessions/<session>.jsonl: a session's Bash calls and subagents, written
//     by the hook-deliver-probe-reports hook (probe-session-event schema)
//   - reports/<state>/<session>/<report-id>.<recipient>.json: one report
//     (probe-report schema)
//   - waiters/<session>.lock and <session>.fifo: the one waiter per session,
//     the hook-wait-for-probe-reports hook
//   - journal-cursor: where the runner resumes reading the journal
//
// A report's state is the directory it is in (pending, handed, delivered, or
// expired), and its file name names the conversation it is for: main,
// agent-<agent id>, or unbound-<subscription id> while the subscription waits
// to learn which conversation created it. Every move is a rename.
//
// The line and document shapes belong to the four .strictspec schemas; this
// package keeps what they cannot see: the state read from a probe's whole log,
// which session a scope belongs to, and the report texts.
package probe

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/lifecycle"
)

// ProbeOOMKill is the one thing a probe watches for: systemd's result term
// for a unit whose process the kernel's OOM killer killed.
const ProbeOOMKill = "oom-kill"

// ProbeTypes returns what a probe can watch for. A probe that would run an
// arbitrary command is refused.
func ProbeTypes() []string {
	return []string{ProbeOOMKill}
}

// FormatVersion is carried by every line and document written here, and is
// the only one read.
const FormatVersion = 1

// ServiceName is the user service that hosts every probe.
const ServiceName = "claudewheel-probe-runner.service"

// HookWaitSeconds is how long the hook labeling a tool call that ended with
// status 137 waits for the runner to record the kill.
const HookWaitSeconds = 3

// IDRE matches probe, subscription, report, and kill ids: 16 lowercase hex
// digits.
var IDRE = regexp.MustCompile(`^[0-9a-f]{16}$`)

// SessionScopeRE matches the scope claudewheel starts each session in: the
// Claude Code process id, then the launch time in whole seconds.
var SessionScopeRE = regexp.MustCompile(`^claudewheel-session-(\d+)-(\d+)\.scope$`)

// ToolScopeRE matches the scope each Bash command of a session runs in: the
// session scope's process id and launch time, then the wrapper's process id.
var ToolScopeRE = regexp.MustCompile(`^claudewheel-tool-(\d+)-(\d+)-(\d+)\.scope$`)

// HeavyScopeRE matches the scope heavy runs each job in.
var HeavyScopeRE = regexp.MustCompile(`^heavy-(\d+)-(\d+)\.scope$`)

// HeavyDescriptionRE matches heavy's scope description inside a session: the
// session scope heavy ran in, then the command.
var HeavyDescriptionRE = regexp.MustCompile(`(?s)^heavy job of (claudewheel-session-\d+-\d+\.scope): (.*)$`)

// HeavyOutsideDescriptionRE matches heavy's scope description outside any
// claudewheel session.
var HeavyOutsideDescriptionRE = regexp.MustCompile(`(?s)^heavy job outside any claudewheel session: (.*)$`)

// OOMKillFix is what to do about a command killed for its memory. heavy's
// kill message and every OOM report and label are built from it.
const OOMKillFix = "a command that outgrows its cap is a defect to fix at the source, so stop " +
	"this line of work at a clean committed point, find where the memory goes " +
	"(a heap profile, what is held at once, what is loaded that need not be), " +
	"and cut it"

// OOMKillLabel is how the label of an OOM-killed tool call begins.
const OOMKillLabel = "this command was OOM-killed: fix the memory at its source, do not rerun"

// OverlapSentence is added to a call's label when other Bash calls of the
// session were running at the kill; the hook fills in {calls}.
const OverlapSentence = " Other Bash calls of this session were running at the kill ({calls}), so " +
	"the killed process may have been theirs; each of them that ended " +
	"OOM-killed is told the same."

// BindLineRE finds the line BindLine prints in a tool call's output; the
// hook binding subscriptions uses the same pattern.
var BindLineRE = regexp.MustCompile(`subscription ([0-9a-f]{16}) of probe ([0-9a-f]{16}) waits to be bound`)

// BindLine is the line probe create and probe subscribe print for the hook
// that binds the subscription to the conversation whose tool call ran them.
func BindLine(subscription, probe string) string {
	return "subscription " + subscription + " of probe " + probe +
		" waits to be bound to the conversation that ran this command"
}

// VerifiedClientVersions are the Claude Code versions the delivery mechanics
// (asyncRewake, rewakeMessage, rewakeSummary, additionalContext on tool
// events) are verified against.
func VerifiedClientVersions() []string {
	return []string{"2.1.281"}
}

// The report states, each a directory under reports/.
const (
	ReportPending   = "pending"
	ReportHanded    = "handed"
	ReportDelivered = "delivered"
	ReportExpired   = "expired"
)

// ReportStates returns every report state, in order.
func ReportStates() []string {
	return []string{ReportPending, ReportHanded, ReportDelivered, ReportExpired}
}

// The reasons a probe ends.
const (
	EndedStopped      = "stopped"
	EndedDeadline     = "deadline"
	EndedCount        = "count"
	EndedWatchedEnded = "watched-ended"
	EndedFile         = "file"
)

// The scopes a kill is attributed to.
const (
	ScopeSession = "session"
	ScopeTool    = "tool"
	ScopeHeavy   = "heavy"
	ScopeOther   = "other"
)

// RecipientMain is the recipient part of a report for a session's main
// conversation.
const RecipientMain = "main"

// RecipientOf is the recipient part of a report's file name for a bound
// conversation: main for a nil agent, else agent-<agent>.
func RecipientOf(agent *string) string {
	if agent == nil {
		return RecipientMain
	}
	return "agent-" + *agent
}

// RecipientUnbound is the recipient part of a report for a subscription not
// bound to a conversation yet.
func RecipientUnbound(subscription string) string {
	return "unbound-" + subscription
}

// Error is a request against the probe store, or the store itself, that
// cannot be honored.
type Error struct {
	msg string
}

func (e *Error) Error() string { return e.msg }

func errorf(format string, args ...any) error {
	return &Error{msg: fmt.Sprintf(format, args...)}
}

// NewID returns a fresh 16-hex-digit id: the first half of a uuid4's hex.
func NewID() string {
	return lifecycle.NewUUID4Hex()[:16]
}

// Store computes the probe store's paths under its root, shared/probes. It
// reads and writes nothing.
type Store struct {
	root string
}

// NewStore returns the store rooted at root (workspace SharedStore.ProbesDir).
func NewStore(root string) Store {
	return Store{root: root}
}

// Root is the store's root directory.
func (s Store) Root() string { return s.root }

// ProbesDir holds one log per probe.
func (s Store) ProbesDir() string { return filepath.Join(s.root, "probes") }

// KillsFile records every kill the runner read.
func (s Store) KillsFile() string { return filepath.Join(s.root, "kills.jsonl") }

// SessionsDir holds each session's record of Bash calls and subagents.
func (s Store) SessionsDir() string { return filepath.Join(s.root, "sessions") }

// ReportsDir holds one directory per report state.
func (s Store) ReportsDir() string { return filepath.Join(s.root, "reports") }

// WaitersDir holds each session's waiter lock and FIFO.
func (s Store) WaitersDir() string { return filepath.Join(s.root, "waiters") }

// CursorFile is where the runner resumes reading the journal.
func (s Store) CursorFile() string { return filepath.Join(s.root, "journal-cursor") }

// ProbeFile is the log of probe, which must be a probe id.
func (s Store) ProbeFile(probe string) (string, error) {
	if !IDRE.MatchString(probe) {
		return "", errorf("not a probe id: '%s' (16 lowercase hex digits)", probe)
	}
	return filepath.Join(s.ProbesDir(), probe+".jsonl"), nil
}

// SessionFile is session's record of Bash calls and subagents.
func (s Store) SessionFile(session string) (string, error) {
	if err := requireSession(session); err != nil {
		return "", err
	}
	return filepath.Join(s.SessionsDir(), session+".jsonl"), nil
}

// ReportDir is the directory of session's reports in state.
func (s Store) ReportDir(state, session string) (string, error) {
	if !isReportState(state) {
		return "", fmt.Errorf("unknown report state '%s'", state)
	}
	if err := requireSession(session); err != nil {
		return "", err
	}
	return filepath.Join(s.ReportsDir(), state, session), nil
}

// FIFO is the FIFO session's waiter sleeps on.
func (s Store) FIFO(session string) (string, error) {
	if err := requireSession(session); err != nil {
		return "", err
	}
	return filepath.Join(s.WaitersDir(), session+".fifo"), nil
}

func requireSession(session string) error {
	if !lifecycle.SessionUUIDRE.MatchString(session) {
		return fmt.Errorf("not a Claude Code session uuid: '%s'", session)
	}
	return nil
}

func isReportState(state string) bool {
	for _, known := range ReportStates() {
		if state == known {
			return true
		}
	}
	return false
}

// validator is the ValidateBytes function of one generated schema package.
type validator func(input []byte, syntax string) (strictspec.Value, []strictspec.Diagnostic)

// validate runs one schema's validation, the format_version check first, and
// names where in every diagnostic.
func validate(v validator, data []byte, syntax, where string) error {
	_, diags := v(data, syntax)
	if len(diags) == 0 {
		return nil
	}
	return errorf("%s: %s", where, lifecycle.JoinDiagnostics(diags))
}

// jsonLine is one validated line of a JSONL file and its 1-based number.
type jsonLine struct {
	lineno int
	data   []byte
}

// readJSONL returns every line of path, validated. A final line cut off
// mid-write is dropped; an absent file has no lines.
func readJSONL(path string, v validator) ([]jsonLine, error) {
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
	lines, complete := lifecycle.SplitLines(string(data))
	var out []jsonLine
	for i, raw := range lines {
		lineno := i + 1
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		if _, err := jsonfile.Decode([]byte(line)); err != nil {
			if lineno == len(lines) && !complete {
				break
			}
			return nil, errorf("%s:%d: malformed JSON: %v", path, lineno, err)
		}
		if err := validate(v, []byte(line), "jsonl", fmt.Sprintf("%s:%d", path, lineno)); err != nil {
			return nil, err
		}
		out = append(out, jsonLine{lineno: lineno, data: []byte(line)})
	}
	return out, nil
}

// appendJSONL validates value as one compact ASCII line and appends it to
// path, leading with a newline when the file ends mid-line.
func appendJSONL(fx *effects.FX, path string, value any, v validator) error {
	line, err := jsonfile.MarshalCompactASCII(value)
	if err != nil {
		return err
	}
	if err := validate(v, line, "jsonl", path); err != nil {
		return err
	}
	return lifecycle.AppendLine(fx, path, line)
}

// decodeLine decodes a validated line into dst; keys without a field are
// skipped, the schema having accepted the line already.
func decodeLine(line jsonLine, path string, dst any) error {
	if err := json.Unmarshal(line.data, dst); err != nil {
		return errorf("%s:%d: %v", path, line.lineno, err)
	}
	return nil
}

// strOr returns *p, or Python's spelling of None for nil, as the Python
// formatted these values.
func strOr(p *string) string {
	if p == nil {
		return "None"
	}
	return *p
}
