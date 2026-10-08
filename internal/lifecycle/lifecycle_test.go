package lifecycle

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/testkit"
)

const session = "0b5d6c1e-7a3f-4c2d-9e8b-1f2a3b4c5d6e"

func strp(s string) *string { return &s }

func at(ms int64) string { return TimestampAt(1_777_000_000_000 + ms) }

func started(ms int64) StartedEvent {
	pid := int64(4242)
	return StartedEvent{
		Header:    Header{ID: "e-started", At: at(ms), Session: session, Source: "hook"},
		Cwd:       "/home/m/Projects/claudewheel",
		ConfigDir: "/home/m/.claudewheel/profiles/work",
		Profile:   strp("work"),
		Model:     strp("claude-opus-5"),
		Entry:     "startup",
		PID:       &pid,
	}
}

func ended(ms int64, outcome string) EndedEvent {
	return EndedEvent{Header: Header{ID: "e-ended", At: at(ms), Session: session, Source: "hook"}, Outcome: outcome}
}

func mark(ms int64, state *string) MarkEvent {
	return MarkEvent{Header: Header{ID: "e-mark", At: at(ms), Session: session, Source: "user"}, State: state}
}

func moved(ms int64, cwd string) MovedEvent {
	return MovedEvent{Header: Header{ID: "e-moved", At: at(ms), Session: session, Source: "user"},
		NewCwd: cwd, OldTranscript: "/s/a.jsonl", NewTranscript: "/s/" + cwd + ".jsonl"}
}

// The Python wrote these lines (recorded by a generator deleted with the Python);
// Go reads each and writes it back byte for byte.
func TestPythonLinesRoundTrip(t *testing.T) {
	data, err := os.ReadFile("testdata/python-lines.json")
	if err != nil {
		t.Fatal(err)
	}
	var lines []string
	if err := json.Unmarshal(data, &lines); err != nil {
		t.Fatal(err)
	}
	for i, line := range lines {
		ev, err := ParseEvent([]byte(line), "py.jsonl", i+1)
		if err != nil {
			t.Fatalf("line %d: %v", i+1, err)
		}
		out, err := EventToJSON(ev)
		if err != nil {
			t.Fatal(err)
		}
		if string(out) != line {
			t.Errorf("line %d:\n got: %s\nwant: %s", i+1, out, line)
		}
	}
}

func TestTimestamps(t *testing.T) {
	stamp := TimestampAt(1_700_000_000_123)
	if stamp != "2023-11-14T22:13:20.123Z" {
		t.Fatalf("TimestampAt = %s", stamp)
	}
	ms, err := ParseTimestampMS(stamp)
	if err != nil || ms != 1_700_000_000_123 {
		t.Fatalf("round trip %d %v", ms, err)
	}
	if ms, err := ParseTimestampMS("2023-11-14T22:13:20.123+00:00"); err != nil || ms != 1_700_000_000_123 {
		t.Fatalf("offset form %d %v", ms, err)
	}
	for _, bad := range []string{"junk", "2023-11-14T22:13:20.123"} {
		if _, err := ParseTimestampMS(bad); err == nil {
			t.Errorf("ParseTimestampMS(%q) succeeded", bad)
		}
	}
	if !(TimestampAt(999) < TimestampAt(1000)) {
		t.Error("lexical order is not time order")
	}
}

func TestEventIDsAreUniqueAndSortByCreation(t *testing.T) {
	seen := map[string]bool{}
	prev := ""
	for i := 0; i < 1000; i++ {
		id := NewEventID()
		if seen[id] {
			t.Fatal("duplicate id")
		}
		seen[id] = true
		if id[:16] < prev {
			t.Fatal("ids do not sort by creation")
		}
		prev = id[:16]
	}
}

func TestSessionFileRefusesJunk(t *testing.T) {
	if p, err := SessionFile("/s", session); err != nil || p != "/s/"+session+".jsonl" {
		t.Fatalf("%s %v", p, err)
	}
	for _, junk := range []string{"", "../x", strings.ToUpper(session), session + "x"} {
		if _, err := SessionFile("/s", junk); err == nil {
			t.Errorf("SessionFile(%q) succeeded", junk)
		}
	}
}

func TestKeyOrderAndNulls(t *testing.T) {
	line, err := EventToJSON(mark(0, nil))
	if err != nil {
		t.Fatal(err)
	}
	want := `{"format_version":1,"id":"e-mark","at":"` + at(0) + `","session":"` + session + `","source":"user","kind":"mark","state":null,"note":null}`
	if string(line) != want {
		t.Fatalf("got %s", line)
	}
}

func TestParseRejections(t *testing.T) {
	good, _ := EventToJSON(started(0))
	with := func(key string, value any) string {
		var m map[string]any
		_ = json.Unmarshal(good, &m)
		m[key] = value
		out, _ := json.Marshal(m)
		return string(out)
	}
	without := func(key string) string {
		var m map[string]any
		_ = json.Unmarshal(good, &m)
		delete(m, key)
		out, _ := json.Marshal(m)
		return string(out)
	}
	for name, line := range map[string]string{
		"not json":           "{not json",
		"not an object":      "[1, 2, 3]",
		"no format_version":  without("format_version"),
		"format_version 2":   with("format_version", 2),
		"unknown event":      with("kind", "resurrected"),
		"unknown key":        with("surprise", "yes"),
		"missing field":      without("cwd"),
		"missing null field": without("profile"),
		"bad enum":           with("entry", "bogus"),
		"bad session":        with("session", "not-a-uuid"),
		"wrong type":         with("pid", "4242"),
	} {
		_, err := ParseEvent([]byte(line), "/s/lifecycle/x.jsonl", 7)
		var lerr *Error
		if !errors.As(err, &lerr) || !strings.Contains(err.Error(), "/s/lifecycle/x.jsonl:7") {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func writeLines(t *testing.T, path string, text string) {
	t.Helper()
	testkit.WriteFile(t, path, text)
}

func TestReadSession(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, session+".jsonl")
	if evs, err := ReadSession(path); err != nil || evs != nil {
		t.Fatalf("missing file: %v %v", evs, err)
	}
	a, _ := EventToJSON(started(0))
	b, _ := EventToJSON(ended(100, "exited"))
	cases := []struct {
		name  string
		text  string
		count int
		fails bool
	}{
		{"empty", "", 0, false},
		{"two lines", string(a) + "\n" + string(b) + "\n", 2, false},
		{"blank lines", string(a) + "\n\n  \n" + string(b) + "\n", 2, false},
		{"interrupted final line", string(a) + "\n" + string(b)[:20], 1, false},
		{"interrupted only line", string(a)[:20], 0, false},
		{"final line without newline", string(a) + "\n" + string(b), 2, false},
		{"damaged middle line", string(a)[:20] + "\n" + string(b) + "\n", 0, true},
		{"damaged final line that is JSON", string(a) + "\n{}", 0, true},
	}
	for _, c := range cases {
		writeLines(t, path, c.text)
		evs, err := ReadSession(path)
		if c.fails {
			if err == nil {
				t.Errorf("%s: no error", c.name)
			}
			continue
		}
		if err != nil || len(evs) != c.count {
			t.Errorf("%s: %d events, %v", c.name, len(evs), err)
		}
	}
	writeLines(t, path, string(a)+"\n"+string(a)+"\n{\"x\":1}\n")
	if _, err := ReadSession(path); err == nil || !strings.Contains(err.Error(), ":3") {
		t.Errorf("the error does not name line 3: %v", err)
	}
}

func TestAppendEvent(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "lifecycle")
	ev := started(0)
	ev.ID, ev.At = "", ""
	got, err := AppendEvent(testkit.FX(), dir, ev)
	if err != nil {
		t.Fatal(err)
	}
	if got.ID == "" || got.At == "" || ev.ID != "" {
		t.Fatalf("stamping: got %+v, caller's %+v", got.Header, ev.Header)
	}
	kept, err := AppendEvent(testkit.FX(), dir, ended(5, "exited"))
	if err != nil || kept.ID != "e-ended" {
		t.Fatalf("%+v %v", kept, err)
	}
	path := filepath.Join(dir, session+".jsonl")
	evs, err := ReadSession(path)
	if err != nil || len(evs) != 2 {
		t.Fatalf("%d %v", len(evs), err)
	}
	before := testkit.ReadFile(t, path)
	bad := ended(6, "vanished")
	if _, err := AppendEvent(testkit.FX(), dir, bad); err == nil {
		t.Fatal("an invalid event was appended")
	}
	if testkit.ReadFile(t, path) != before {
		t.Fatal("an invalid event changed the file")
	}
	junk := ended(6, "exited")
	junk.Session = "junk"
	if _, err := AppendEvent(testkit.FX(), dir, junk); err == nil {
		t.Fatal("a junk session was appended")
	}
	// A file ending mid-line gets a separator first.
	testkit.WriteFile(t, path, before+`{"partial`)
	if _, err := AppendEvent(testkit.FX(), dir, ended(7, "exited")); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(testkit.ReadFile(t, path), "{\"partial\n"+mustLine(t, ended(7, "exited"))+"\n") {
		t.Fatalf("no separator:\n%s", testkit.ReadFile(t, path))
	}
	if _, err := ReadSession(path); err == nil {
		t.Fatal("the damaged line in the middle was read")
	}
}

func mustLine(t *testing.T, ev Event) string {
	t.Helper()
	line, err := EventToJSON(ev)
	if err != nil {
		t.Fatal(err)
	}
	return string(line)
}

func TestLoadAll(t *testing.T) {
	dir := t.TempDir()
	if got, err := LoadAll(filepath.Join(dir, "missing")); err != nil || len(got) != 0 {
		t.Fatalf("%v %v", got, err)
	}
	if _, err := AppendEvent(testkit.FX(), dir, started(0)); err != nil {
		t.Fatal(err)
	}
	testkit.WriteFile(t, filepath.Join(dir, "notes.txt"), "x")
	got, err := LoadAll(dir)
	if err != nil || len(got) != 1 || got[session].Started == nil {
		t.Fatalf("%v %v", got, err)
	}
	testkit.WriteFile(t, filepath.Join(dir, "not-a-uuid.jsonl"), "")
	if _, err := LoadAll(dir); err == nil {
		t.Fatal("a file named by no session uuid was read around")
	}
}

func TestSummarize(t *testing.T) {
	onHold := "on-hold"
	cases := []struct {
		name   string
		events []Event
		check  func(SessionLifecycle) bool
	}{
		{"empty", nil, func(s SessionLifecycle) bool { return s.Started == nil && s.LastAt == "" }},
		{"move after start", []Event{started(0), moved(10, "/b")}, func(s SessionLifecycle) bool { c, _ := s.Cwd(); return c == "/b" }},
		{"latest move wins", []Event{started(0), moved(20, "/c"), moved(10, "/b")}, func(s SessionLifecycle) bool { c, _ := s.Cwd(); return c == "/c" }},
		{"start after move", []Event{moved(10, "/b"), started(20)}, func(s SessionLifecycle) bool { c, _ := s.Cwd(); return c == started(0).Cwd && s.Moved == nil }},
		{"move without start", []Event{moved(10, "/b")}, func(s SessionLifecycle) bool { c, ok := s.Cwd(); return ok && c == "/b" }},
		{"ended after started", []Event{started(0), ended(10, "exited")}, func(s SessionLifecycle) bool { return s.Ended != nil }},
		{"ended without started", []Event{ended(10, "crashed")}, func(s SessionLifecycle) bool { return s.Ended != nil }},
		{"restart drops the old end", []Event{started(0), ended(10, "exited"), started(20)}, func(s SessionLifecycle) bool { return s.Ended == nil }},
		{"null mark clears", []Event{mark(0, &onHold), mark(10, nil)}, func(s SessionLifecycle) bool { return s.Mark == nil }},
		{"mark again", []Event{mark(0, &onHold), mark(10, nil), mark(20, &onHold)}, func(s SessionLifecycle) bool { return s.Mark != nil }},
		{"tie goes to the later line", []Event{mark(0, nil), mark(0, &onHold)}, func(s SessionLifecycle) bool { return s.Mark != nil }},
		{"last at is the greatest", []Event{started(50), ended(10, "exited")}, func(s SessionLifecycle) bool { return s.LastAt == at(50) }},
	}
	for _, c := range cases {
		if s := Summarize(c.events, session); !c.check(s) || s.Session != session {
			t.Errorf("%s: %+v", c.name, s)
		}
	}
}

func TestSweepCrashed(t *testing.T) {
	dir := t.TempDir()
	if _, err := AppendEvent(testkit.FX(), dir, started(0)); err != nil {
		t.Fatal(err)
	}
	all, err := LoadAll(dir)
	if err != nil {
		t.Fatal(err)
	}
	startMS, _ := ParseTimestampMS(at(0))
	if w, err := SweepCrashed(testkit.FX(), dir, all, nil, startMS+SweepGraceMS-1); err != nil || len(w) != 0 {
		t.Fatalf("inside the grace: %v %v", w, err)
	}
	if w, err := SweepCrashed(testkit.FX(), dir, all, map[string]bool{session: true}, startMS+SweepGraceMS+1); err != nil || len(w) != 0 {
		t.Fatalf("live: %v %v", w, err)
	}
	w, err := SweepCrashed(testkit.FX(), dir, all, nil, startMS+SweepGraceMS+1)
	if err != nil || len(w) != 1 || w[0].Outcome != "crashed" || w[0].Source != "sweep" {
		t.Fatalf("dead: %v %v", w, err)
	}
	all, _ = LoadAll(dir)
	if w, err := SweepCrashed(testkit.FX(), dir, all, nil, startMS+SweepGraceMS+1); err != nil || len(w) != 0 {
		t.Fatalf("second sweep: %v %v", w, err)
	}
}

func TestCaptureName(t *testing.T) {
	dir := t.TempDir()
	derived := "derived"
	ev, err := CaptureName(testkit.FX(), dir, nil, session, "first", &derived)
	if err != nil || ev == nil {
		t.Fatalf("%v %v", ev, err)
	}
	if ev, err := CaptureName(testkit.FX(), dir, nil, session, "", nil); err != nil || ev != nil {
		t.Fatalf("no name: %v %v", ev, err)
	}
	all, _ := LoadAll(dir)
	if ev, err := CaptureName(testkit.FX(), dir, all[session], session, "first", &derived); err != nil || ev != nil {
		t.Fatalf("repeat: %v %v", ev, err)
	}
	user := "user"
	if ev, err := CaptureName(testkit.FX(), dir, all[session], session, "first", &user); err != nil || ev == nil {
		t.Fatalf("new source: %v %v", ev, err)
	}
	if ev, err := CaptureName(testkit.FX(), dir, all[session], session, "second", &derived); err != nil || ev == nil {
		t.Fatalf("new name: %v %v", ev, err)
	}
}

func TestStateSetsPartitionTheStates(t *testing.T) {
	seen := map[string]int{}
	for _, set := range []map[string]bool{LiveStates(), LooseEndStates(), HiddenByDefaultStates()} {
		for s := range set {
			seen[s]++
		}
	}
	for _, s := range States() {
		if seen[s] != 1 {
			t.Errorf("state %s is in %d sets", s, seen[s])
		}
	}
	if len(seen) != len(States()) {
		t.Errorf("the sets hold states outside States()")
	}
}

func TestDeriveState(t *testing.T) {
	startMS, _ := ParseTimestampMS(at(0))
	onHold := "on-hold"
	lcOf := func(events ...Event) *SessionLifecycle { s := Summarize(events, session); return &s }
	cases := []struct {
		name string
		lc   *SessionLifecycle
		obs  Observed
		now  int64
		want string
	}{
		{"unverified", nil, Observed{Live: true}, startMS, StateUnverified},
		{"busy", nil, Observed{Live: true, Verified: true, Status: "busy"}, startMS, StateWorking},
		{"unknown status", nil, Observed{Live: true, Verified: true, Status: "new"}, startMS, StateRunning},
		{"live beats a mark", lcOf(mark(0, &onHold)), Observed{Live: true, Verified: true, Status: "idle"}, startMS, StateIdle},
		{"mark beats an end", lcOf(started(0), ended(5, "exited"), mark(10, &onHold)), Observed{}, startMS, StateOnHold},
		{"ended", lcOf(started(0), ended(5, "exited")), Observed{}, startMS, StateExited},
		{"registry without process", lcOf(started(0)), Observed{RegistryPresent: true}, startMS, StateCrashed},
		{"starting", lcOf(started(0)), Observed{}, startMS + SweepGraceMS, StateStarting},
		{"crashed after the grace", lcOf(started(0)), Observed{}, startMS + SweepGraceMS + 1, StateCrashed},
		{"nothing", nil, Observed{}, startMS, StateExited},
	}
	for _, c := range cases {
		got, err := DeriveState(c.lc, c.obs, c.now)
		if err != nil || got != c.want {
			t.Errorf("%s: %q %v, want %q", c.name, got, err, c.want)
		}
	}
}
