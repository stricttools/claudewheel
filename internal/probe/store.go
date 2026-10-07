package probe

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"syscall"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/schema/oomkillevent"
	"github.com/stricttools/claudewheel/internal/schema/probeevent"
	"github.com/stricttools/claudewheel/internal/schema/probereport"
	"github.com/stricttools/claudewheel/internal/schema/probesessionevent"
)

// Subscription is one subscription to a probe, as its log reads.
type Subscription struct {
	ID      string
	Session string
	// Bound is false until a hook binds the subscription to the conversation
	// that created it.
	Bound bool
	// Agent is the subagent a bound subscription reports to; nil means the
	// main conversation.
	Agent  *string
	Active bool
}

// ProbeState is one probe, read from its whole log.
type ProbeState struct {
	ID               string
	Session          string
	ProbeType        string
	WatchSession     *string
	Deadline         string
	UntilCount       *int64
	UntilWatchedEnds bool
	UntilFile        *string
	CreatedAt        string
	// Subscriptions are in the order their subscribed lines appear.
	Subscriptions []Subscription
	// Ended is the reason the probe ended, nil while it is live.
	Ended   *string
	EndedAt *string
}

// Active reports whether the probe has not ended.
func (p *ProbeState) Active() bool { return p.Ended == nil }

// Subscription returns the subscription with id, or nil.
func (p *ProbeState) Subscription(id string) *Subscription {
	for i := range p.Subscriptions {
		if p.Subscriptions[i].ID == id {
			return &p.Subscriptions[i]
		}
	}
	return nil
}

// lineHead holds the fields every probe log line starts with, in schema
// order.
type lineHead struct {
	FormatVersion int    `json:"format_version"`
	ID            string `json:"id"`
	At            string `json:"at"`
	Probe         string `json:"probe"`
	Type          string `json:"kind"`
}

func newHead(probe, lineType string) lineHead {
	return lineHead{
		FormatVersion: FormatVersion,
		ID:            lifecycle.NewEventID(),
		At:            lifecycle.NowTimestamp(),
		Probe:         probe,
		Type:          lineType,
	}
}

type createdLine struct {
	lineHead
	Session          string  `json:"session"`
	ProbeType        string  `json:"probe_kind"`
	WatchSession     *string `json:"watch_session"`
	Deadline         string  `json:"deadline"`
	UntilCount       *int64  `json:"until_count"`
	UntilWatchedEnds bool    `json:"until_watched_ends"`
	UntilFile        *string `json:"until_file"`
}

type subscribedLine struct {
	lineHead
	Subscription string `json:"subscription"`
	Session      string `json:"session"`
}

type unsubscribedLine struct {
	lineHead
	Subscription string `json:"subscription"`
}

type endedLine struct {
	lineHead
	Reason string `json:"reason"`
}

// probeLine is any probe log line as read; each type fills its own fields.
type probeLine struct {
	At               string  `json:"at"`
	Probe            string  `json:"probe"`
	Type             string  `json:"kind"`
	Session          string  `json:"session"`
	ProbeType        string  `json:"probe_kind"`
	WatchSession     *string `json:"watch_session"`
	Deadline         string  `json:"deadline"`
	UntilCount       *int64  `json:"until_count"`
	UntilWatchedEnds bool    `json:"until_watched_ends"`
	UntilFile        *string `json:"until_file"`
	Subscription     string  `json:"subscription"`
	Agent            *string `json:"agent"`
	Reason           string  `json:"reason"`
}

// appendProbeLine validates and appends one line to probe's log.
func appendProbeLine(fx *effects.FX, store Store, probe string, line any) error {
	path, err := store.ProbeFile(probe)
	if err != nil {
		return err
	}
	return appendJSONL(fx, path, line, diagnosticsOf(probeevent.ValidateBytes))
}

// AppendEnded appends the line ending probe for reason (one of the Ended
// constants).
func AppendEnded(fx *effects.FX, store Store, probe, reason string) error {
	return appendProbeLine(fx, store, probe, endedLine{lineHead: newHead(probe, "ended"), Reason: reason})
}

// ReadProbe reads probe's whole log into its state; an absent probe is
// refused.
func ReadProbe(store Store, probe string) (*ProbeState, error) {
	path, err := store.ProbeFile(probe)
	if err != nil {
		return nil, err
	}
	lines, err := readJSONL(path, diagnosticsOf(probeevent.ValidateBytes))
	if err != nil {
		return nil, err
	}
	if len(lines) == 0 {
		return nil, errorf("no probe %s (no %s)", probe, path)
	}
	events := make([]probeLine, len(lines))
	for i, line := range lines {
		if err := decodeLine(line, path, &events[i]); err != nil {
			return nil, err
		}
	}
	first := events[0]
	if first.Type != "created" {
		return nil, errorf("%s:1: a probe log starts with its created line", path)
	}
	state := &ProbeState{
		ID:               probe,
		Session:          first.Session,
		ProbeType:        first.ProbeType,
		WatchSession:     first.WatchSession,
		Deadline:         first.Deadline,
		UntilCount:       first.UntilCount,
		UntilWatchedEnds: first.UntilWatchedEnds,
		UntilFile:        first.UntilFile,
		CreatedAt:        first.At,
	}
	for i, ev := range events {
		if ev.Probe != probe {
			return nil, errorf("%s:%d: line names probe %s", path, i+1, ev.Probe)
		}
		switch ev.Type {
		case "subscribed":
			sub := Subscription{ID: ev.Subscription, Session: ev.Session, Active: true}
			if existing := state.Subscription(ev.Subscription); existing != nil {
				*existing = sub
			} else {
				state.Subscriptions = append(state.Subscriptions, sub)
			}
		case "bound":
			if sub := state.Subscription(ev.Subscription); sub != nil && !sub.Bound {
				sub.Bound = true
				sub.Agent = ev.Agent
			}
		case "unsubscribed":
			if sub := state.Subscription(ev.Subscription); sub != nil {
				sub.Active = false
			}
		case "ended":
			if state.Ended == nil {
				reason, at := ev.Reason, ev.At
				state.Ended, state.EndedAt = &reason, &at
			}
		}
	}
	return state, nil
}

// LoadProbes reads every probe in the store, sorted by id.
func LoadProbes(store Store) ([]*ProbeState, error) {
	dir := store.ProbesDir()
	info, err := os.Stat(dir)
	if pathstat.NotFoundOrParentNotDirectory(err) {
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
	var out []*ProbeState
	for _, entry := range entries {
		name := entry.Name()
		if matched, _ := filepath.Match("*.jsonl", name); !matched {
			continue
		}
		id := name[:len(name)-len(".jsonl")]
		if !IDRE.MatchString(id) {
			return nil, errorf("%s: file name is not a probe id", filepath.Join(dir, name))
		}
		state, err := ReadProbe(store, id)
		if err != nil {
			return nil, err
		}
		out = append(out, state)
	}
	return out, nil
}

// FindSubscription returns the probe holding subscription and the
// subscription itself; it is refused when no probe holds it.
func FindSubscription(probes []*ProbeState, subscription string) (*ProbeState, *Subscription, error) {
	for _, state := range probes {
		if sub := state.Subscription(subscription); sub != nil {
			return state, sub, nil
		}
	}
	return nil, nil, errorf("no subscription %s in any probe", subscription)
}

// Kill is one OOM kill the runner read, a line of kills.jsonl, its fields in
// schema order.
type Kill struct {
	FormatVersion int      `json:"format_version"`
	ID            string   `json:"id"`
	At            string   `json:"at"`
	KilledAtUS    int64    `json:"killed_at_us"`
	Unit          string   `json:"unit"`
	Cursor        string   `json:"cursor"`
	Scope         string   `json:"scope"`
	Session       *string  `json:"session"`
	Command       *string  `json:"command"`
	Unattributed  *string  `json:"unattributed"`
	Reports       []string `json:"reports"`
	Probes        []string `json:"probes"`
	Label         string   `json:"label"`
}

// AppendKill validates kill and appends it to kills.jsonl.
func AppendKill(fx *effects.FX, store Store, kill Kill) error {
	if kill.Reports == nil {
		kill.Reports = []string{}
	}
	if kill.Probes == nil {
		kill.Probes = []string{}
	}
	return appendJSONL(fx, store.KillsFile(), kill, diagnosticsOf(oomkillevent.ValidateBytes))
}

// ReadKills returns every kill recorded, in file order.
func ReadKills(store Store) ([]Kill, error) {
	path := store.KillsFile()
	lines, err := readJSONL(path, diagnosticsOf(oomkillevent.ValidateBytes))
	if err != nil {
		return nil, err
	}
	out := make([]Kill, len(lines))
	for i, line := range lines {
		if err := decodeLine(line, path, &out[i]); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// AgentInfo is one subagent a session launched, from the hook's record.
type AgentInfo struct {
	Agent    string
	Task     *string
	Finished bool
}

// sessionLine is any line of a session's record; only the subagent lines'
// fields are read.
type sessionLine struct {
	Type  string  `json:"kind"`
	Agent *string `json:"agent"`
	Task  *string `json:"task"`
}

// ReadSessionAgents returns the subagents session launched, keyed by agent
// id, from the record the hook-deliver-probe-reports hook writes.
func ReadSessionAgents(store Store, session string) (map[string]*AgentInfo, error) {
	path, err := store.SessionFile(session)
	if err != nil {
		return nil, err
	}
	lines, err := readJSONL(path, diagnosticsOf(probesessionevent.ValidateBytes))
	if err != nil {
		return nil, err
	}
	out := map[string]*AgentInfo{}
	for _, line := range lines {
		var ev sessionLine
		if err := decodeLine(line, path, &ev); err != nil {
			return nil, err
		}
		if ev.Agent == nil {
			continue
		}
		switch ev.Type {
		case "agent-launched":
			out[*ev.Agent] = &AgentInfo{Agent: *ev.Agent, Task: ev.Task}
		case "agent-finished":
			info, ok := out[*ev.Agent]
			if !ok {
				info = &AgentInfo{Agent: *ev.Agent}
				out[*ev.Agent] = info
			}
			info.Finished = true
		}
	}
	return out, nil
}

// Report is one report, as its document reads; its fields are in schema
// order after format_version.
type Report struct {
	ID           string  `json:"id"`
	At           string  `json:"at"`
	Session      string  `json:"session"`
	Agent        *string `json:"agent"`
	Task         *string `json:"task"`
	Probe        *string `json:"probe"`
	Subscription *string `json:"subscription"`
	Kill         string  `json:"kill"`
	Text         string  `json:"text"`
}

// reportDocument is a report as written: format_version, then the report.
type reportDocument struct {
	FormatVersion int `json:"format_version"`
	Report
}

// ToJSON returns the report document, indented by two spaces with a final
// newline.
func (r Report) ToJSON() ([]byte, error) {
	return jsonfile.MarshalIndented(reportDocument{FormatVersion: FormatVersion, Report: r})
}

// ReportFile is a report where it lies: its state, its recipient, and its
// path.
type ReportFile struct {
	State     string
	Recipient string
	Path      string
	Report    Report
}

// WriteReport validates report and writes it as pending for recipient,
// returning its path.
func WriteReport(fx *effects.FX, store Store, report Report, recipient string) (string, error) {
	text, err := report.ToJSON()
	if err != nil {
		return "", err
	}
	if err := validate(diagnosticsOf(probereport.ValidateBytes), text, "json", "report "+report.ID); err != nil {
		return "", err
	}
	dir, err := store.ReportDir(ReportPending, report.Session)
	if err != nil {
		return "", err
	}
	if err := fx.MkdirAll(dir); err != nil {
		return "", err
	}
	path := filepath.Join(dir, report.ID+"."+recipient+".json")
	if err := fx.WriteFileAtomic(path, text); err != nil {
		return "", err
	}
	return path, nil
}

// ReadReport reads and validates the report at path.
func ReadReport(path string) (Report, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Report{}, errorf("%s: cannot be read: %v", path, err)
	}
	if err := validate(diagnosticsOf(probereport.ValidateBytes), data, "json", path); err != nil {
		return Report{}, err
	}
	var doc reportDocument
	if err := json.Unmarshal(data, &doc); err != nil {
		return Report{}, errorf("%s: %v", path, err)
	}
	return doc.Report, nil
}

// reportNameRE matches a report's file name, capturing the recipient.
var reportNameRE = regexp.MustCompile(`^([0-9a-f]{16})\.((?:main)|(?:agent-[0-9a-zA-Z_-]+)|(?:unbound-[0-9a-f]{16}))\.json$`)

// ListReports returns every report in states, state by state, then by
// session directory and file name.
func ListReports(store Store, states []string) ([]ReportFile, error) {
	var out []ReportFile
	for _, state := range states {
		if !isReportState(state) {
			return nil, errorf("unknown report state '%s'", state)
		}
		base := filepath.Join(store.ReportsDir(), state)
		sessionDirs, err := listDir(base)
		if err != nil {
			return nil, err
		}
		for _, sessionDir := range sessionDirs {
			info, err := os.Stat(sessionDir)
			if pathstat.NotFoundOrParentNotDirectory(err) {
				continue
			}
			if err != nil {
				return nil, err
			}
			if !info.IsDir() {
				continue
			}
			files, err := listDir(sessionDir)
			if err != nil {
				return nil, err
			}
			for _, path := range files {
				name := filepath.Base(path)
				if matched, _ := filepath.Match("*.json", name); !matched {
					continue
				}
				match := reportNameRE.FindStringSubmatch(name)
				if match == nil {
					return nil, errorf("%s: not a report file name (<report-id>.<recipient>.json)", path)
				}
				report, err := ReadReport(path)
				if err != nil {
					return nil, err
				}
				out = append(out, ReportFile{State: state, Recipient: match[2], Path: path, Report: report})
			}
		}
	}
	return out, nil
}

// listDir returns the paths of dir's entries sorted by name; a dir that is
// absent or not a directory has none.
func listDir(dir string) ([]string, error) {
	info, err := os.Stat(dir)
	if pathstat.NotFoundOrParentNotDirectory(err) {
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
	out := make([]string, len(entries))
	for i, entry := range entries {
		out[i] = filepath.Join(dir, entry.Name())
	}
	return out, nil
}

// MoveReport renames item into state, keeping its file name, and returns its
// new path.
func MoveReport(fx *effects.FX, store Store, item ReportFile, state string) (string, error) {
	dir, err := store.ReportDir(state, item.Report.Session)
	if err != nil {
		return "", err
	}
	if err := fx.MkdirAll(dir); err != nil {
		return "", err
	}
	target := filepath.Join(dir, filepath.Base(item.Path))
	if err := fx.Rename(item.Path, target); err != nil {
		return "", err
	}
	return target, nil
}

// WakeWaiter tells session's waiter, if one is waiting, that its reports
// changed, by writing one byte to its FIFO without blocking. No FIFO, or no
// reader on it, means no waiter and nothing to wake; a full pipe already
// holds wake-ups. Under --dry-run nothing is woken: the reports it would
// announce were only recorded. A path that is not a FIFO is a
// *NotFIFOError.
func WakeWaiter(fx *effects.FX, store Store, session string) error {
	fifo, err := store.FIFO(session)
	if err != nil {
		return err
	}
	info, err := os.Stat(fifo)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&fs.ModeNamedPipe == 0 {
		return &NotFIFOError{Path: fifo}
	}
	if fx.Previewing() {
		return nil
	}
	// A raw descriptor, not an *os.File: the runtime poller would turn a
	// write to a full pipe into a wait instead of EAGAIN.
	fd, err := syscall.Open(fifo, syscall.O_WRONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err == syscall.ENXIO || pathstat.NotFoundOrParentNotDirectory(err) {
		return nil
	}
	if err != nil {
		return &os.PathError{Op: "open", Path: fifo, Err: err}
	}
	_, werr := syscall.Write(fd, []byte("x"))
	cerr := syscall.Close(fd)
	if werr != nil && werr != syscall.EAGAIN {
		return &os.PathError{Op: "write", Path: fifo, Err: werr}
	}
	if cerr != nil {
		return &os.PathError{Op: "close", Path: fifo, Err: cerr}
	}
	return nil
}
