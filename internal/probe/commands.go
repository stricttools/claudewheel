package probe

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
)

// SessionScopeOfUnit returns the claudewheel session scope unit belongs to: a
// session scope is its own, and a Bash command's tool scope names its
// session scope. ok is false for any other unit.
func SessionScopeOfUnit(unit string) (scope string, ok bool) {
	if SessionScopeRE.MatchString(unit) {
		return unit, true
	}
	if m := ToolScopeRE.FindStringSubmatch(unit); m != nil {
		return "claudewheel-session-" + m[1] + "-" + m[2] + ".scope", true
	}
	return "", false
}

// SessionScopeOfCgroup returns the claudewheel session scope a
// /proc/<pid>/cgroup text places a process in, read from the unified
// hierarchy's 0:: line, whose last element is the session scope or one of
// its tool scopes. ok is false for anything else.
func SessionScopeOfCgroup(cgroupText string) (scope string, ok bool) {
	for _, line := range splitLines(cgroupText) {
		if rest, found := strings.CutPrefix(line, "0::"); found {
			rest = strings.TrimRight(rest, "/")
			return SessionScopeOfUnit(rest[strings.LastIndex(rest, "/")+1:])
		}
	}
	return "", false
}

// splitLines splits text at line boundaries as Python's str.splitlines does
// for the newline forms a cgroup file can hold.
func splitLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	lines := strings.Split(text, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

// SessionForScope returns the Claude Code session that ran in scope at atMS,
// from the lifecycle store under lifecycleDir: the one whose newest started
// line records the scope's process id, no earlier than the scope's launch
// second and no later than atMS. When none does, session is empty and why
// says so.
func SessionForScope(lifecycleDir, scope string, atMS int64) (session, why string, err error) {
	m := SessionScopeRE.FindStringSubmatch(scope)
	if m == nil {
		return "", scope + " is not a claudewheel session scope", nil
	}
	pid, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return "", "", errorf("%s: process id out of range: %v", scope, err)
	}
	launched, err := strconv.ParseInt(m[2], 10, 64)
	if err != nil {
		return "", "", errorf("%s: launch time out of range: %v", scope, err)
	}
	paths, err := lifecycle.SessionFiles(lifecycleDir)
	if err != nil {
		return "", "", err
	}
	found := false
	var bestMS int64
	for _, path := range paths {
		events, err := lifecycle.ReadSession(path)
		if err != nil {
			return "", "", err
		}
		for _, ev := range events {
			started, ok := ev.(lifecycle.StartedEvent)
			if !ok || started.PID == nil || *started.PID != pid {
				continue
			}
			startedMS, err := lifecycle.ParseTimestampMS(started.At)
			if err != nil {
				return "", "", err
			}
			if startedMS < launched*1000 || startedMS > atMS {
				continue
			}
			if !found || startedMS >= bestMS {
				found, bestMS, session = true, startedMS, started.Session
			}
		}
	}
	if !found {
		return "", fmt.Sprintf("the lifecycle store records no session started by Claude Code process %d of %s", pid, scope), nil
	}
	return session, "", nil
}

// OwnCgroupText returns this process's /proc/self/cgroup.
func OwnCgroupText() (string, error) {
	data, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		return "", err
	}
	return string(data), nil
}

// ResolveSession returns the Claude Code session the calling process belongs
// to, from its own cgroup text. Outside a claudewheel session it is refused,
// naming the fix.
func ResolveSession(lifecycleDir, cgroupText string, atMS int64) (string, error) {
	scope, ok := SessionScopeOfCgroup(cgroupText)
	if !ok {
		where := "unknown"
		for _, line := range splitLines(cgroupText) {
			if rest, found := strings.CutPrefix(line, "0::"); found {
				where = rest
				break
			}
		}
		return "", errorf("a probe command learns its session from its own cgroup, and this "+
			"process runs outside any claudewheel session scope (its cgroup is "+
			"%s); run it from a Bash tool call of a Claude Code session "+
			"claudewheel launched, whose commands run in scopes named "+
			"claudewheel-tool-<pid>-<time>-<n>.scope after the session's "+
			"claudewheel-session-<pid>-<time>.scope, and not through heavy, "+
			"which runs its command in a scope of its own", where)
	}
	session, why, err := SessionForScope(lifecycleDir, scope, atMS)
	if err != nil {
		return "", err
	}
	if session == "" {
		return "", errorf("%s; the hook-session-start hook records each session as it "+
			"starts, so this session started before that hook was wired, or "+
			"its record failed: relaunch the session with claudewheel", why)
	}
	return session, nil
}

// LocalTime renders a journal timestamp in microseconds as local wall-clock
// time, as the hook-timestamp hook prints it.
func LocalTime(atUS int64) string {
	return time.UnixMicro(atUS).Local().Format("2006-01-02 15:04:05 MST")
}

// WhatWasKilled is one phrase naming what kill killed, where, and when.
func WhatWasKilled(kill Kill) string {
	when := LocalTime(kill.KilledAtUS)
	switch kill.Scope {
	case ScopeHeavy:
		command := "(command not recorded)"
		if kill.Command != nil && *kill.Command != "" {
			command = *kill.Command
		}
		return "the heavy job `" + command + "` in " + kill.Unit + " was killed at its memory cap at " + when
	case ScopeTool:
		return "a process of the Bash command in " + kill.Unit + " was killed at " + when
	case ScopeSession:
		return "a process in the session scope " + kill.Unit + " was killed at " + when
	}
	return "a process in " + kill.Unit + " was killed at " + when
}

// OwnKillText is the report a session gets of its own command's kill.
func OwnKillText(reportID string, kill Kill) string {
	return "[claudewheel probe report " + reportID + "] A command this session started " +
		"was OOM-killed: " + WhatWasKilled(kill) + ". Fix the memory at its source, " +
		"do not rerun it: " + OOMKillFix + "."
}

// ProbeKillText is the report a subscriber gets from probe.
func ProbeKillText(reportID, probe string, kill Kill) string {
	whose := "outside any claudewheel session (" + strOr(kill.Unattributed) + ")"
	if kill.Session != nil {
		whose = "in claudewheel session " + *kill.Session
	}
	return "[claudewheel probe report " + reportID + "] Probe " + probe + " saw an OOM kill " +
		whose + ": " + WhatWasKilled(kill) + "."
}

// KillLabel is what a tool call that ended OOM-killed during kill is told.
func KillLabel(kill Kill) string {
	return OOMKillLabel + " (" + WhatWasKilled(kill) + "; " + OOMKillFix + ")."
}

var durationRE = regexp.MustCompile(`^([1-9][0-9]*)([smhd])$`)

var unitSeconds = map[string]int64{"s": 1, "m": 60, "h": 3600, "d": 86400}

// ParseDuration reads a whole number of seconds, minutes, hours, or days
// (90s, 30m, 2h, 7d) as seconds.
func ParseDuration(text string) (int64, error) {
	refuse := errorf("a duration is a whole number with an s, m, h, or d suffix (90s, 30m, 2h, 7d), not '%s'", text)
	m := durationRE.FindStringSubmatch(text)
	if m == nil {
		return 0, refuse
	}
	n, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return 0, refuse
	}
	return n * unitSeconds[m[2]], nil
}

// CreateRequest is what probe create asks for.
type CreateRequest struct {
	// Session is the session creating the probe and subscribed to it.
	Session string
	// ProbeType is what the probe watches for, one of ProbeTypes.
	ProbeType string
	// WatchSession is the one session watched; nil watches all sessions.
	WatchSession     *string
	DeadlineSeconds  int64
	UntilCount       *int64
	UntilWatchedEnds bool
	// UntilFile must be absolute.
	UntilFile *string
	NowMS     int64
}

// CreateProbe creates a probe owned by req.Session and subscribes that
// session to it, returning the probe id and the subscription id. Every
// refusal happens before anything is written.
func CreateProbe(fx *effects.FX, store Store, lifecycleDir string, req CreateRequest) (probeID, subscription string, err error) {
	known := false
	for _, t := range ProbeTypes() {
		if req.ProbeType == t {
			known = true
		}
	}
	if !known {
		return "", "", errorf("no probe kind '%s'; the kinds that exist are: %s", req.ProbeType, strings.Join(ProbeTypes(), ", "))
	}
	if req.WatchSession != nil {
		if err := requireSessionArg(*req.WatchSession); err != nil {
			return "", "", err
		}
		lifecycles, err := lifecycle.LoadAll(lifecycleDir)
		if err != nil {
			return "", "", err
		}
		if _, ok := lifecycles[*req.WatchSession]; !ok {
			return "", "", errorf("the lifecycle store records no session %s; name a "+
				"session claudewheel has recorded (claudewheel's sessions overview "+
				"lists them)", *req.WatchSession)
		}
	} else if req.UntilWatchedEnds {
		return "", "", errorf("--until-watched-ends needs a watched session, and a probe of all sessions has none")
	}
	if req.UntilCount != nil && *req.UntilCount < 1 {
		return "", "", errorf("--count takes a whole number of at least 1, not %d", *req.UntilCount)
	}
	if req.UntilFile != nil && !filepath.IsAbs(*req.UntilFile) {
		return "", "", errorf("--until-file takes an absolute path, not '%s'", *req.UntilFile)
	}
	probeID, subscription = NewID(), NewID()
	created := createdLine{
		lineHead:         newHead(probeID, "created"),
		Session:          req.Session,
		ProbeType:        req.ProbeType,
		WatchSession:     req.WatchSession,
		Deadline:         lifecycle.TimestampAt(req.NowMS + req.DeadlineSeconds*1000),
		UntilCount:       req.UntilCount,
		UntilWatchedEnds: req.UntilWatchedEnds,
		UntilFile:        req.UntilFile,
	}
	if err := appendProbeLine(fx, store, probeID, created); err != nil {
		return "", "", err
	}
	subscribed := subscribedLine{lineHead: newHead(probeID, "subscribed"), Subscription: subscription, Session: req.Session}
	if err := appendProbeLine(fx, store, probeID, subscribed); err != nil {
		return "", "", err
	}
	return probeID, subscription, nil
}

func requireSessionArg(session string) error {
	if !lifecycle.SessionUUIDRE.MatchString(session) {
		return errorf("not a Claude Code session uuid: '%s' (lowercase 8-4-4-4-12 hex)", session)
	}
	return nil
}

func requireID(what, value string) error {
	if !IDRE.MatchString(value) {
		return errorf("not a %s id: '%s' (16 lowercase hex digits)", what, value)
	}
	return nil
}

// Subscribe subscribes session to a live probe and returns the subscription
// id.
func Subscribe(fx *effects.FX, store Store, session, probeID string) (string, error) {
	if err := requireID("probe", probeID); err != nil {
		return "", err
	}
	state, err := ReadProbe(store, probeID)
	if err != nil {
		return "", err
	}
	if !state.Active() {
		return "", errorf("probe %s ended (%s); it reports nothing more", probeID, *state.Ended)
	}
	subscription := NewID()
	line := subscribedLine{lineHead: newHead(probeID, "subscribed"), Subscription: subscription, Session: session}
	if err := appendProbeLine(fx, store, probeID, line); err != nil {
		return "", err
	}
	return subscription, nil
}

// Unsubscribe removes one of session's subscriptions and returns its probe's
// id.
func Unsubscribe(fx *effects.FX, store Store, session, subscription string) (string, error) {
	if err := requireID("subscription", subscription); err != nil {
		return "", err
	}
	probes, err := LoadProbes(store)
	if err != nil {
		return "", err
	}
	state, sub, err := FindSubscription(probes, subscription)
	if err != nil {
		return "", err
	}
	if sub.Session != session {
		return "", errorf("subscription %s belongs to session %s, not to this one (%s); a session removes only its own", subscription, sub.Session, session)
	}
	if !sub.Active {
		return "", errorf("subscription %s was already removed", subscription)
	}
	line := unsubscribedLine{lineHead: newHead(state.ID, "unsubscribed"), Subscription: subscription}
	if err := appendProbeLine(fx, store, state.ID, line); err != nil {
		return "", err
	}
	return state.ID, nil
}

// StopProbe ends a live probe session created.
func StopProbe(fx *effects.FX, store Store, session, probeID string) error {
	if err := requireID("probe", probeID); err != nil {
		return err
	}
	state, err := ReadProbe(store, probeID)
	if err != nil {
		return err
	}
	if state.Session != session {
		return errorf("probe %s was created by session %s, not by this one (%s); only the session that created a probe stops it", probeID, state.Session, session)
	}
	if !state.Active() {
		return errorf("probe %s already ended (%s)", probeID, *state.Ended)
	}
	return AppendEnded(fx, store, probeID, EndedStopped)
}

// when renders an at timestamp as LocalTime does.
func when(at string) (string, error) {
	ms, err := lifecycle.ParseTimestampMS(at)
	if err != nil {
		return "", err
	}
	return LocalTime(ms * 1000), nil
}

// DescribeProbe returns the lines probe list shows for one probe.
func DescribeProbe(state *ProbeState) ([]string, error) {
	watch := "all sessions"
	if state.WatchSession != nil && *state.WatchSession != "" {
		watch = "session " + *state.WatchSession
	}
	deadline, err := when(state.Deadline)
	if err != nil {
		return nil, err
	}
	stops := []string{"deadline " + deadline}
	if state.UntilCount != nil {
		stops = append(stops, fmt.Sprintf("after %d kill(s)", *state.UntilCount))
	}
	if state.UntilWatchedEnds {
		stops = append(stops, "when the watched session ends")
	}
	if state.UntilFile != nil {
		stops = append(stops, "when "+*state.UntilFile+" exists")
	}
	status := "live"
	if !state.Active() {
		endedAt := state.CreatedAt
		if state.EndedAt != nil && *state.EndedAt != "" {
			endedAt = *state.EndedAt
		}
		at, err := when(endedAt)
		if err != nil {
			return nil, err
		}
		status = "ended (" + *state.Ended + ", " + at + ")"
	}
	lines := []string{
		"probe " + state.ID + " [" + status + "]: " + state.ProbeType + " in " + watch +
			", created by session " + state.Session + "; stops at " + strings.Join(stops, ", "),
	}
	for _, sub := range state.Subscriptions {
		var to string
		switch {
		case !sub.Bound:
			to = "not bound to a conversation yet"
		case sub.Agent == nil:
			to = "main conversation"
		default:
			to = "subagent " + *sub.Agent
		}
		removed := ""
		if !sub.Active {
			removed = " (unsubscribed)"
		}
		lines = append(lines, "  subscription "+sub.ID+": session "+sub.Session+", "+to+removed)
	}
	return lines, nil
}

// UndeliveredCounts returns how many reports each session has not been
// confirmed to receive (pending or handed).
func UndeliveredCounts(store Store) (map[string]int, error) {
	items, err := ListReports(store, []string{ReportPending, ReportHanded})
	if err != nil {
		return nil, err
	}
	counts := map[string]int{}
	for _, item := range items {
		counts[item.Report.Session]++
	}
	return counts, nil
}

// RenderList returns, as text, everything the store holds that someone may
// need to act on: probes, undelivered and expired reports, and unrouted
// kills.
func RenderList(store Store) (string, error) {
	var out []string
	probes, err := LoadProbes(store)
	if err != nil {
		return "", err
	}
	out = append(out, "Probes:")
	if len(probes) == 0 {
		out = append(out, "  none")
	}
	for _, state := range probes {
		lines, err := DescribeProbe(state)
		if err != nil {
			return "", err
		}
		for _, line := range lines {
			out = append(out, "  "+line)
		}
	}

	out = append(out, "Undelivered reports:")
	undelivered, err := ListReports(store, []string{ReportPending, ReportHanded})
	if err != nil {
		return "", err
	}
	if len(undelivered) == 0 {
		out = append(out, "  none")
	}
	for _, item := range undelivered {
		why := "pending"
		if item.State == ReportHanded {
			why = "handed, not yet in the transcript"
		}
		if strings.HasPrefix(item.Recipient, "unbound-") {
			why = "waiting for its subscription to be bound"
		}
		origin := "own command"
		if item.Report.Probe != nil && *item.Report.Probe != "" {
			origin = "probe " + *item.Report.Probe
		}
		out = append(out, "  report "+item.Report.ID+" to session "+item.Report.Session+
			" ("+item.Recipient+"), "+origin+": "+why)
	}

	out = append(out, "Expired reports:")
	expired, err := ListReports(store, []string{ReportExpired})
	if err != nil {
		return "", err
	}
	if len(expired) == 0 {
		out = append(out, "  none")
	}
	for _, item := range expired {
		out = append(out, "  report "+item.Report.ID+" to session "+item.Report.Session+
			" ("+item.Recipient+"), probe "+strOr(item.Report.Probe))
	}

	out = append(out, "Kills no session took (unrouted):")
	kills, err := ReadKills(store)
	if err != nil {
		return "", err
	}
	unrouted := 0
	for _, kill := range kills {
		if len(kill.Reports) > 0 {
			continue
		}
		unrouted++
		reason := "attributed, but no conversation took it"
		if kill.Unattributed != nil && *kill.Unattributed != "" {
			reason = *kill.Unattributed
		}
		out = append(out, "  "+LocalTime(kill.KilledAtUS)+" "+kill.Unit+": "+reason)
	}
	if unrouted == 0 {
		out = append(out, "  none")
	}
	return strings.Join(out, "\n") + "\n", nil
}
