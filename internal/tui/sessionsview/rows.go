package sessionsview

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/sessions"
)

// SessionIDEnv is the environment variable carrying Claude Code's own
// session uuid, exported into every process a session starts.
const SessionIDEnv = "CLAUDE_CODE_SESSION_ID"

// PIDEnv is the environment variable carrying the pid of the Claude Code
// process that owns the session.
const PIDEnv = "CLAUDE_PID"

// CurrentMark marks the row of the session the user is sitting in.
const CurrentMark = "*"

// Unnamed is what a session with no name anywhere reads as.
const Unnamed = "(unnamed)"

// clauseSep separates the clauses inside one line.
const clauseSep = " · "

// Identity is who the reader is: the session uuid and the pid that owns it.
type Identity struct {
	SessionID string
	PID       int
}

// CurrentIdentity returns the identity the environment describes, read
// through lookup (os.LookupEnv, or a fixture), or nil when it describes
// none. Both values must be present and usable: a missing or empty
// variable, a pid that is not all ASCII digits, and a pid no process can
// have all mean no identity, so no row is marked rather than the wrong one.
// A row is current only when both values match its record: a session id
// alone would also mark a sibling process of the same session, and a pid
// alone whatever the kernel handed that recycled number to.
func CurrentIdentity(lookup func(string) (string, bool)) *Identity {
	sessionID, _ := lookup(SessionIDEnv)
	rawPID, _ := lookup(PIDEnv)
	sessionID = strings.TrimSpace(sessionID)
	rawPID = strings.TrimSpace(rawPID)
	if sessionID == "" || rawPID == "" {
		return nil
	}
	for _, r := range rawPID {
		if r < '0' || r > '9' {
			return nil
		}
	}
	pid, err := strconv.Atoi(rawPID)
	if err != nil || pid <= 0 {
		return nil
	}
	return &Identity{SessionID: sessionID, PID: pid}
}

// IsCurrent reports whether record is the session identity describes: both
// values compared exactly. A record with no session id never matches.
func IsCurrent(record sessions.SessionRecord, identity *Identity) bool {
	if identity == nil || record.SessionID == "" {
		return false
	}
	return record.SessionID == identity.SessionID && record.PID == identity.PID
}

// FormatUptime says how long a session started at startedAt has been up at
// nowMS, both wall-clock milliseconds. No start time reads "unknown"; a
// start time in the future (a clock stepped between the readings) reads as
// no uptime rather than negative.
func FormatUptime(startedAt *int64, nowMS int64) string {
	if startedAt == nil {
		return "unknown"
	}
	seconds := max(0, (nowMS-*startedAt)/1000)
	if seconds < 60 {
		return fmt.Sprintf("%ds", seconds)
	}
	minutes := seconds / 60
	if minutes < 60 {
		return fmt.Sprintf("%dm", minutes)
	}
	hours := minutes / 60
	if hours < 24 {
		return fmt.Sprintf("%dh %dm", hours, minutes%60)
	}
	return fmt.Sprintf("%dd %dh", hours/24, hours%24)
}

// FormatMemory renders resident memory given in KiB, as ps -o rss= reports
// it. A negative value is an error.
func FormatMemory(rssKiB int64) (string, error) {
	if rssKiB < 0 {
		return "", fmt.Errorf("resident memory must not be negative: %d", rssKiB)
	}
	if rssKiB < 1024 {
		return fmt.Sprintf("%d KiB", rssKiB), nil
	}
	if rssKiB < 1024*1024 {
		return fmt.Sprintf("%.1f MiB", float64(rssKiB)/1024), nil
	}
	return fmt.Sprintf("%.1f GiB", float64(rssKiB)/(1024*1024)), nil
}

func blockHeader(record sessions.SessionRecord, prefix string, current bool) string {
	mark := strings.Repeat(" ", len(CurrentMark))
	if current {
		mark = CurrentMark
	}
	name := record.Name
	if name == "" {
		name = Unnamed
	}
	status := record.Status
	if status == "" {
		status = "unknown"
	}
	parts := []string{name, record.Category, status}
	if !record.Live {
		parts = append(parts, "stale")
	}
	return prefix + mark + " " + strings.Join(parts, "  ")
}

// BlockOptions is what a caller adds to one record's row block.
type BlockOptions struct {
	// Highlighted expands the block.
	Highlighted bool
	NowMS       int64
	// Identity decides the current-session mark, compared per IsCurrent.
	Identity *Identity
	// RSSKiB is the resident memory measured for the pid; nil writes no
	// memory clause.
	RSSKiB *int64
	// State adds (collapsed) or fills (highlighted) the per-row state line;
	// nil has none.
	State *string
	// Selector is the "[x]" or "[ ]" toggle drawn at the left edge, with the
	// rest of the block indented under it; empty draws none.
	Selector string
}

// FormatRow returns the lines of one record's row block. Heights are fixed
// per state, because they are what ComputeViewport scrolls over: collapsed,
// two lines (a header and a summary), three with a state line; highlighted,
// five (header, directory, identity, resources, and the state line, blank
// when the row carries no state). Nothing a record may be missing changes
// those counts.
func FormatRow(record sessions.SessionRecord, opts BlockOptions) ([]string, error) {
	prefix := ""
	if opts.Selector != "" {
		prefix = opts.Selector + " "
	}
	indent := strings.Repeat(" ", runeLen(prefix)+len(CurrentMark)+1)
	memory := ""
	if opts.RSSKiB != nil {
		m, err := FormatMemory(*opts.RSSKiB)
		if err != nil {
			return nil, err
		}
		memory = m
	}
	uptime := FormatUptime(record.StartedAt, opts.NowMS)
	header := blockHeader(record, prefix, IsCurrent(record, opts.Identity))
	cwd := record.Cwd
	if cwd == "" {
		cwd = "(no directory)"
	}

	if !opts.Highlighted {
		summary := []string{cwd, "up " + uptime}
		if opts.RSSKiB != nil {
			summary = append(summary, memory)
		}
		lines := []string{header, indent + strings.Join(summary, clauseSep)}
		if opts.State != nil {
			lines = append(lines, indent+*opts.State)
		}
		return lines, nil
	}

	version := "version unknown"
	if record.Version != "" {
		version = "v" + record.Version
	}
	session := record.SessionID
	if session == "" {
		session = "unknown"
	}
	identityLine := strings.Join([]string{
		fmt.Sprintf("pid %d", record.PID),
		"session " + session,
		version,
	}, clauseSep)
	resources := []string{"up " + uptime}
	if opts.RSSKiB != nil {
		resources = append(resources, memory)
	}
	stateLine := ""
	if opts.State != nil {
		stateLine = indent + *opts.State
	}
	return []string{
		header,
		indent + "cwd " + cwd,
		indent + identityLine,
		indent + strings.Join(resources, clauseSep),
		stateLine,
	}, nil
}

// runeLen counts code points, the unit every width here is measured in.
func runeLen(s string) int {
	return len([]rune(s))
}

// runePrefix is the first n code points of s; n <= 0 is empty.
func runePrefix(s string, n int) string {
	if n <= 0 {
		return ""
	}
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}
