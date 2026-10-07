package proberunner

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// systemd's catalog ids: a unit's process was OOM-killed; a job finished
// (the "Started <unit> - <description>" line).
const (
	OOMKillMessageID = "fe6faa94e7774663a0da52717891d8ef"
	JobDoneMessageID = "39f53479d3a045ac8e11786248231fbf"
)

// describeTimeout bounds the journal query for a heavy scope's description.
const describeTimeout = 30 * time.Second

// digestID is a 16-hex-digit id derived from parts, so reprocessing an entry
// repeats its ids.
func digestID(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:])[:16]
}

// Describer returns the description the journal kept for a heavy scope; ok
// is false when the journal holds none.
type Describer func(unit string) (description string, ok bool, err error)

// HeavyDescriber returns the Describer that asks the user journal for a
// heavy scope's "Started" line.
func HeavyDescriber(fx *effects.FX) Describer {
	return func(unit string) (string, bool, error) {
		return HeavyDescription(fx, unit)
	}
}

// HeavyDescription returns the description the journal kept for a heavy
// scope, from its "Started" line; the last such line wins.
func HeavyDescription(fx *effects.FX, unit string) (string, bool, error) {
	result, err := fx.Run(effects.Cmd{
		Argv: []string{
			"journalctl", "--user", "-o", "json", "--no-pager",
			"MESSAGE_ID=" + JobDoneMessageID,
			"USER_UNIT=" + unit,
		},
		Capture: true,
		Read:    true,
		Timeout: describeTimeout,
	})
	if err != nil {
		return "", false, err
	}
	prefix := "Started " + unit + " - "
	description, found := "", false
	lines, _ := lifecycle.SplitLines(result.Stdout())
	for _, line := range lines {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal([]byte(line), &fields); err != nil {
			continue
		}
		message, ok := stringField(fields, "MESSAGE")
		if !ok {
			continue
		}
		if rest, ok := strings.CutPrefix(message, prefix); ok {
			description, found = strings.TrimSuffix(rest, "."), true
		}
	}
	return description, found, nil
}

// Attribution is which session's command a kill hit, or why none can be
// named. Scope is one of probe's Scope constants.
type Attribution struct {
	Scope        string
	Session      *string
	Command      *string
	Unattributed *string
}

func strPtr(s string) *string { return &s }

// optional is nil for an empty s.
func optional(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// Attribute returns which session's command the kill in unit hit, or why
// none can be named.
func Attribute(unit string, killedAtMS int64, lifecycleDir string, describe Describer) (Attribution, error) {
	if probe.SessionScopeRE.MatchString(unit) {
		session, why, err := probe.SessionForScope(lifecycleDir, unit, killedAtMS)
		if err != nil {
			return Attribution{}, err
		}
		return Attribution{Scope: probe.ScopeSession, Session: optional(session), Unattributed: optional(why)}, nil
	}
	if probe.ToolScopeRE.MatchString(unit) {
		// A Bash command's scope names its session scope.
		scope, _ := probe.SessionScopeOfUnit(unit)
		session, why, err := probe.SessionForScope(lifecycleDir, scope, killedAtMS)
		if err != nil {
			return Attribution{}, err
		}
		return Attribution{Scope: probe.ScopeTool, Session: optional(session), Unattributed: optional(why)}, nil
	}
	if probe.HeavyScopeRE.MatchString(unit) {
		description, ok, err := describe(unit)
		if err != nil {
			return Attribution{}, err
		}
		if !ok {
			return Attribution{
				Scope:        probe.ScopeHeavy,
				Unattributed: strPtr("the journal holds no description for " + unit),
			}, nil
		}
		if m := probe.HeavyDescriptionRE.FindStringSubmatch(description); m != nil {
			session, why, err := probe.SessionForScope(lifecycleDir, m[1], killedAtMS)
			if err != nil {
				return Attribution{}, err
			}
			return Attribution{Scope: probe.ScopeHeavy, Session: optional(session), Command: strPtr(m[2]), Unattributed: optional(why)}, nil
		}
		if m := probe.HeavyOutsideDescriptionRE.FindStringSubmatch(description); m != nil {
			return Attribution{
				Scope:        probe.ScopeHeavy,
				Command:      strPtr(m[1]),
				Unattributed: strPtr("heavy ran outside any claudewheel session"),
			}, nil
		}
		return Attribution{
			Scope: probe.ScopeHeavy,
			Unattributed: strPtr(unit + "'s description names no claudewheel session: the heavy that " +
				"started it does not record one"),
		}, nil
	}
	return Attribution{
		Scope:        probe.ScopeOther,
		Unattributed: strPtr(unit + " is neither a claudewheel session or tool scope nor a heavy scope"),
	}, nil
}

// Entry is one journal entry as journalctl -o json prints it, its fields
// left undecoded.
type Entry map[string]json.RawMessage

// ParseEntry decodes one line of journalctl -o json output.
func ParseEntry(line []byte) (Entry, error) {
	var entry Entry
	if err := json.Unmarshal(line, &entry); err != nil {
		return nil, err
	}
	if entry == nil {
		return nil, errors.New("not a JSON object")
	}
	return entry, nil
}

// String returns the field key when it is a JSON string.
func (e Entry) String(key string) (string, bool) {
	return stringField(e, key)
}

func stringField(fields map[string]json.RawMessage, key string) (string, bool) {
	raw, ok := fields[key]
	if !ok {
		return "", false
	}
	var s string
	if err := json.Unmarshal(raw, &s); err != nil {
		return "", false
	}
	return s, true
}

// realtimeUS reads __REALTIME_TIMESTAMP, which journalctl prints as a string
// of decimal microseconds.
func (e Entry) realtimeUS() (int64, error) {
	raw, ok := e["__REALTIME_TIMESTAMP"]
	if !ok {
		return 0, errors.New("journal entry has no __REALTIME_TIMESTAMP")
	}
	text := string(raw)
	if s, ok := stringField(e, "__REALTIME_TIMESTAMP"); ok {
		text = s
	}
	us, err := strconv.ParseInt(strings.TrimSpace(text), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("journal entry's __REALTIME_TIMESTAMP is not whole microseconds: %s", raw)
	}
	return us, nil
}

// reportExists reports whether a report with reportID lies in any state for
// session.
func reportExists(store probe.Store, session, reportID string) (bool, error) {
	for _, state := range probe.ReportStates() {
		dir, err := store.ReportDir(state, session)
		if err != nil {
			return false, err
		}
		entries, err := os.ReadDir(dir)
		if pathstat.NotFoundOrNotDirectory(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		for _, entry := range entries {
			name := entry.Name()
			if strings.HasPrefix(name, reportID+".") && strings.HasSuffix(name, ".json") &&
				len(name) > len(reportID)+len(".json") {
				return true, nil
			}
		}
	}
	return false, nil
}

// watches reports whether state takes a kill of session at killedAtMS.
func watches(state *probe.ProbeState, session *string, killedAtMS int64) (bool, error) {
	if !state.Active() || state.ProbeType != probe.ProbeOOMKill {
		return false, nil
	}
	deadline, err := lifecycle.ParseTimestampMS(state.Deadline)
	if err != nil {
		return false, err
	}
	if deadline < killedAtMS {
		return false, nil
	}
	created, err := lifecycle.ParseTimestampMS(state.CreatedAt)
	if err != nil {
		return false, err
	}
	if created > killedAtMS {
		return false, nil
	}
	if state.WatchSession == nil {
		return true, nil
	}
	return session != nil && *state.WatchSession == *session, nil
}

// ProcessEntry turns one journal entry into its reports and its kill record,
// then wakes the waiter of every session given a new report. It returns the
// kill record, or nil when the entry is not an OOM kill of a unit or was
// already recorded (a restarted runner may read it twice).
func ProcessEntry(fx *effects.FX, ws workspace.Workspace, entry Entry, describe Describer) (*probe.Kill, error) {
	if id, _ := entry.String("MESSAGE_ID"); id != OOMKillMessageID {
		return nil, nil
	}
	unit, unitOK := entry.String("USER_UNIT")
	cursor, cursorOK := entry.String("__CURSOR")
	if !unitOK || !cursorOK || unit == "" {
		return nil, nil
	}
	store := probe.NewStore(ws.Shared().ProbesDir())
	lifecycleDir := ws.Shared().LifecycleDir()
	killID := digestID("kill", cursor)
	kills, err := probe.ReadKills(store)
	if err != nil {
		return nil, err
	}
	for _, k := range kills {
		if k.ID == killID {
			return nil, nil
		}
	}
	killedAtUS, err := entry.realtimeUS()
	if err != nil {
		return nil, err
	}
	killedAtMS := killedAtUS / 1000
	who, err := Attribute(unit, killedAtMS, lifecycleDir, describe)
	if err != nil {
		return nil, err
	}
	kill := probe.Kill{
		FormatVersion: probe.FormatVersion,
		ID:            killID,
		At:            lifecycle.NowTimestamp(),
		KilledAtUS:    killedAtUS,
		Unit:          unit,
		Cursor:        cursor,
		Scope:         who.Scope,
		Session:       who.Session,
		Command:       who.Command,
		Unattributed:  who.Unattributed,
		Reports:       []string{},
		Probes:        []string{},
	}
	kill.Label = probe.KillLabel(kill)
	woken := map[string]bool{}

	queue := func(session, recipient string, agent, task, probeID, subscription *string, textFor func(string) string) error {
		reportID := digestID("report", cursor, session, recipient)
		kill.Reports = append(kill.Reports, reportID)
		exists, err := reportExists(store, session, reportID)
		if err != nil || exists {
			return err
		}
		report := probe.Report{
			ID:           reportID,
			At:           lifecycle.NowTimestamp(),
			Session:      session,
			Agent:        agent,
			Task:         task,
			Probe:        probeID,
			Subscription: subscription,
			Kill:         killID,
			Text:         textFor(reportID),
		}
		if _, err := probe.WriteReport(fx, store, report, recipient); err != nil {
			return err
		}
		woken[session] = true
		return nil
	}

	if who.Session != nil {
		err := queue(*who.Session, probe.RecipientMain, nil, nil, nil, nil, func(rid string) string {
			return probe.OwnKillText(rid, kill)
		})
		if err != nil {
			return nil, err
		}
	}

	probes, err := probe.LoadProbes(store)
	if err != nil {
		return nil, err
	}
	for _, state := range probes {
		ok, err := watches(state, who.Session, killedAtMS)
		if err != nil {
			return nil, err
		}
		if !ok {
			continue
		}
		kill.Probes = append(kill.Probes, state.ID)
		probeID := state.ID
		for _, sub := range state.Subscriptions {
			if !sub.Active {
				continue
			}
			if sub.Bound && sub.Agent == nil && who.Session != nil && sub.Session == *who.Session {
				continue // the session's own report already tells its main conversation
			}
			agents, err := probe.ReadSessionAgents(store, sub.Session)
			if err != nil {
				return nil, err
			}
			var recipient string
			var agent, task *string
			switch {
			case !sub.Bound:
				recipient = probe.RecipientUnbound(sub.ID)
			case sub.Agent == nil:
				recipient = probe.RecipientMain
			default:
				info := agents[*sub.Agent]
				agent = sub.Agent
				if info != nil {
					task = info.Task
				}
				if info != nil && info.Finished {
					recipient = probe.RecipientMain
				} else {
					recipient = probe.RecipientOf(sub.Agent)
				}
			}
			subscription := sub.ID
			err = queue(sub.Session, recipient, agent, task, &probeID, &subscription, func(rid string) string {
				return probe.ProbeKillText(rid, probeID, kill)
			})
			if err != nil {
				return nil, err
			}
		}
	}

	if err := probe.AppendKill(fx, store, kill); err != nil {
		return nil, err
	}
	sessions := make([]string, 0, len(woken))
	for session := range woken {
		sessions = append(sessions, session)
	}
	sort.Strings(sessions)
	for _, session := range sessions {
		if err := probe.WakeWaiter(fx, store, session); err != nil {
			return nil, err
		}
	}
	return &kill, nil
}
