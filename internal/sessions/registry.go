package sessions

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// Claude Code registers every process it starts as <config dir>/sessions/<pid>.json,
// written at startup and unlinked on exit. A crash, a kill -9, or a reboot
// leaves the file behind and PIDs are recycled, so the directory is a set of
// claims. A claim is live only when its PID exists and, where both sides have
// one, the kernel start-time token recorded as procStart (field 22 of
// /proc/<pid>/stat) still matches: that tells the process that wrote the file
// from whatever the kernel handed its number to afterwards. Without /proc the
// filter cannot run and liveness is plain PID existence, as in Claude Code's
// own comparison.

// SessionsDirName is the registry directory inside a Claude Code config
// directory.
const SessionsDirName = "sessions"

// The session categories Claude Code writes in its registry records.
const (
	// CategoryInteractive is a human's terminal session, and the category
	// of a record that names none.
	CategoryInteractive = "interactive"
	// CategoryBackground is a background session.
	CategoryBackground = "bg"
	// CategoryDaemon is Claude Code's daemon supervisor, which its own
	// daemon-stop command stops.
	CategoryDaemon = "daemon"
	// CategoryDaemonWorker is a worker the daemon supervises, a process of
	// its own.
	CategoryDaemonWorker = "daemon-worker"
)

// BackgroundCategories returns the categories Claude Code writes for work
// that is not a human's terminal. Every other category, including one this
// package has never seen, is read as interactive, so nothing becomes
// deletable by carrying an unknown name.
func BackgroundCategories() []string {
	return []string{CategoryBackground, CategoryDaemon, CategoryDaemonWorker}
}

// SessionRecord is one parsed registry file with its liveness resolved.
// Absent or wrongly typed text fields are empty.
type SessionRecord struct {
	Path string
	PID  int
	// Category is the record's session category (CategoryInteractive when it
	// names none).
	Category string
	// Live is the filtered answer at read time: the PID exists and, where a
	// start token is available on both sides, still names the writer.
	Live      bool
	SessionID string
	Cwd       string
	Status    string
	Name      string
	Version   string
	// StartedAt is wall-clock milliseconds, nil when absent.
	StartedAt *int64
	// ProcStart is the recorded kernel start token, normalized by
	// RecordedToken: empty means none was recorded.
	ProcStart string
}

// Interactive reports whether the record is a human's session rather than
// background work: its category is not one of BackgroundCategories.
func (r SessionRecord) Interactive() bool {
	for _, c := range BackgroundCategories() {
		if r.Category == c {
			return false
		}
	}
	return true
}

// ProcessStartToken returns the kernel start-time token of the live process
// pid, or false when none can be read (a dead process, an unreadable /proc,
// or no /proc at all). The stat line is split after its last ')' so a
// process name holding spaces or parentheses cannot shift the fields.
func ProcessStartToken(pid int) (string, bool) {
	data, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return "", false
	}
	stat := string(data)
	tail := strings.Fields(stat[strings.LastIndex(stat, ")")+1:])
	// tail[0] is field 3 (state), so field 22 (starttime) is tail[19].
	if len(tail) < 20 {
		return "", false
	}
	return tail[19], true
}

// PIDExists reports whether pid names a process on this machine. It sends
// signal 0, which probes and delivers nothing; a process of another user
// exists.
func PIDExists(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

// RecordedToken normalizes a recorded start token: a blank value is read as
// absent (empty), never as a token that fails to match, which would make a
// running process look provably dead.
func RecordedToken(value string) string {
	return strings.TrimSpace(value)
}

// IsLive applies the liveness filter to one claim: the PID exists, and when
// procStart (normalized by RecordedToken) and the process's current token
// are both available, they are equal. It is asked again at the moment of
// acting, because a snapshot says nothing about the PID later.
func IsLive(pid int, procStart string) bool {
	if !PIDExists(pid) {
		return false
	}
	procStart = RecordedToken(procStart)
	if procStart == "" {
		return true
	}
	actual, ok := ProcessStartToken(pid)
	if !ok {
		return true
	}
	return actual == procStart
}

// parseRecord parses one registry file, or returns false when it is not
// one. Everything unreadable is skipped: the directory belongs to Claude
// Code, and a torn write from a starting session must not break a guard.
func parseRecord(path string) (SessionRecord, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return SessionRecord{}, false
	}
	o, err := jsonfile.DecodeObject(data)
	if err != nil {
		return SessionRecord{}, false
	}
	pid, ok := integerField(o, "pid")
	if !ok {
		// The file name is the PID; a record missing the field is still a claim.
		stem := strings.TrimSuffix(filepath.Base(path), ".json")
		if !AllDigits(stem) {
			return SessionRecord{}, false
		}
		n, err := strconv.ParseInt(stem, 10, 0)
		if err != nil {
			return SessionRecord{}, false
		}
		pid = n
	}
	text := func(key string) string {
		s, _ := stringField(o, key)
		return s
	}
	category := text("kind")
	if category == "" {
		category = CategoryInteractive
	}
	procStart := RecordedToken(text("procStart"))
	record := SessionRecord{
		Path:      path,
		PID:       int(pid),
		Category:  category,
		Live:      IsLive(int(pid), procStart),
		SessionID: text("sessionId"),
		Cwd:       text("cwd"),
		Status:    text("status"),
		Name:      text("name"),
		Version:   text("version"),
		ProcStart: procStart,
	}
	if startedAt, ok := integerField(o, "startedAt"); ok {
		record.StartedAt = &startedAt
	}
	return record, true
}

// integerField returns o[key] when it is a JSON integer literal that fits an
// int64.
func integerField(o *jsonfile.Object, key string) (int64, bool) {
	v, ok := o.Get(key)
	if !ok {
		return 0, false
	}
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := strconv.ParseInt(string(n), 10, 64)
	if err != nil {
		return 0, false
	}
	return i, true
}

// AllDigits reports whether s is non-empty and made only of ASCII digits.
func AllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// stillTheSameFile reports whether record's path still holds the record it
// was read from: the same PID and start token, the pair that names a process
// for good. A rewritten, half-written, or vanished file is not.
func stillTheSameFile(record SessionRecord) bool {
	current, ok := parseRecord(record.Path)
	if !ok {
		return false
	}
	return current.PID == record.PID && current.ProcStart == record.ProcStart
}

// ReadRecords returns every parseable registry record under configDir, live
// or not, sorted by PID. A missing or unreadable sessions directory is an
// empty registry.
func ReadRecords(configDir string) []SessionRecord {
	dir := filepath.Join(configDir, SessionsDirName)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var records []SessionRecord
	for _, e := range entries {
		if filepath.Ext(e.Name()) != ".json" {
			continue
		}
		path := filepath.Join(dir, e.Name())
		if info, err := os.Stat(path); err != nil || !info.Mode().IsRegular() {
			continue
		}
		if record, ok := parseRecord(path); ok {
			records = append(records, record)
		}
	}
	sort.SliceStable(records, func(i, j int) bool { return records[i].PID < records[j].PID })
	return records
}

// LiveRecords returns the records under configDir whose processes are
// running.
func LiveRecords(configDir string) []SessionRecord {
	var live []SessionRecord
	for _, r := range ReadRecords(configDir) {
		if r.Live {
			live = append(live, r)
		}
	}
	return live
}

// Prune deletes the registry files of the records that are provably dead and
// returns those records.
//
// Liveness is asked again here (IsLive), not read off the snapshot; a record
// IsLive cannot rule out stays. The file is re-read just before deletion and
// removed only while it still holds the record's own PID and start token: a
// new session given the recycled PID writes its own record over the same
// file, which must not be taken. An unparseable file is never pruned, so a
// file being written by a starting session is safe. Pruning must never walk
// the directory itself.
//
// A file that is gone or cannot be removed is skipped, and the rest go on.
// Under --dry-run the removals are recorded and the records reported. A
// read-only FX is an error wrapping effects.ErrReadOnly, never a skip.
func Prune(fx *effects.FX, records []SessionRecord) ([]SessionRecord, error) {
	var pruned []SessionRecord
	for _, record := range records {
		if IsLive(record.PID, record.ProcStart) {
			continue
		}
		if !stillTheSameFile(record) {
			continue
		}
		if err := fx.RemoveIfExists(record.Path); err != nil {
			if errors.Is(err, effects.ErrReadOnly) {
				return pruned, err
			}
			continue
		}
		pruned = append(pruned, record)
	}
	return pruned, nil
}

// HasLiveInteractive reports whether a human's session is live in
// configDir: the predicate of the profile delete and rename guards.
func HasLiveInteractive(configDir string) bool {
	for _, r := range ReadRecords(configDir) {
		if r.Interactive() && r.Live {
			return true
		}
	}
	return false
}

// ProfileConfigDir names one profile and its Claude Code config directory.
type ProfileConfigDir struct {
	Name      string
	ConfigDir string
}

// ProfileRecord is a registry record with the profile it was read from.
type ProfileRecord struct {
	Profile   string
	ConfigDir string
	Record    SessionRecord
}

// ReadProfileRecords returns every registry record of every profile, in
// profiles order and ReadRecords order within one.
func ReadProfileRecords(profiles []ProfileConfigDir) []ProfileRecord {
	var out []ProfileRecord
	for _, p := range profiles {
		for _, r := range ReadRecords(p.ConfigDir) {
			out = append(out, ProfileRecord{Profile: p.Name, ConfigDir: p.ConfigDir, Record: r})
		}
	}
	return out
}

// LiveSessionIDs returns the session ids of the records whose processes are
// running, as a set. Records with no session id are left out.
func LiveSessionIDs(records []SessionRecord) map[string]bool {
	ids := map[string]bool{}
	for _, r := range records {
		if r.Live && r.SessionID != "" {
			ids[r.SessionID] = true
		}
	}
	return ids
}
