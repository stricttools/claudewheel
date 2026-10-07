package probe

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/testkit"
)

const (
	sessionS = "4d97ca01-9d56-4f49-8047-77f5160febde"
	sessionT = "0b1e7c52-4f6f-4d7e-9a53-1f2a3b4c5d6e"
	launched = int64(1_790_000_000)
)

func cgroupOf(scope string) string {
	return "0::/user.slice/user-1000.slice/user@1000.service/app.slice/claudewheel-4242_1790000000.slice/" + scope + "\n"
}

// setUp records sessions S (pid 4242) and T (pid 7777) as launched in
// their session scopes, and returns the store and the lifecycle directory.
func setUp(t *testing.T) (Store, string) {
	t.Helper()
	root := t.TempDir()
	lifecycleDir := filepath.Join(root, "shared", "lifecycle")
	for session, pid := range map[string]int64{sessionS: 4242, sessionT: 7777} {
		p := pid
		_, err := lifecycle.AppendEvent(testkit.FX(), lifecycleDir, lifecycle.StartedEvent{
			Header: lifecycle.Header{At: lifecycle.TimestampAt(launched*1000 + 1000), Session: session, Source: "hook"},
			Cwd:    "/w", ConfigDir: "/c", Entry: "startup", PID: &p,
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	return NewStore(filepath.Join(root, "shared", "probes")), lifecycleDir
}

func resolve(t *testing.T, lifecycleDir, scope string) (string, error) {
	t.Helper()
	return ResolveSession(lifecycleDir, cgroupOf(scope), (launched+600)*1000)
}

func TestResolveSessionFromTheCgroup(t *testing.T) {
	_, dir := setUp(t)
	if s, err := resolve(t, dir, "claudewheel-session-4242-1790000000.scope"); err != nil || s != sessionS {
		t.Fatalf("session scope: %q %v", s, err)
	}
	if s, err := resolve(t, dir, "claudewheel-tool-4242-1790000000-77.scope"); err != nil || s != sessionS {
		t.Fatalf("a Bash command's tool scope: %q %v", s, err)
	}
	if _, err := resolve(t, dir, "heavy-1-2.scope"); err == nil || !strings.Contains(err.Error(), "heavy-1-2.scope") || !strings.Contains(err.Error(), "Bash tool call") {
		t.Fatalf("outside a session: %v", err)
	}
	if _, err := resolve(t, dir, "claudewheel-session-9999-1790000000.scope"); err == nil {
		t.Fatal("a scope the lifecycle store does not know was resolved")
	}
}

func TestProbeLifecycle(t *testing.T) {
	store, dir := setUp(t)
	fx := testkit.FX()
	now := (launched + 600) * 1000
	watched := sessionT
	count := int64(2)
	probeID, sub, err := CreateProbe(fx, store, dir, CreateRequest{
		Session: sessionS, ProbeType: "oom-kill", WatchSession: &watched, DeadlineSeconds: 7200,
		UntilCount: &count, UntilWatchedEnds: true, NowMS: now,
	})
	if err != nil {
		t.Fatal(err)
	}
	state, err := ReadProbe(store, probeID)
	if err != nil || state == nil || len(state.Subscriptions) != 1 || state.Subscriptions[0].ID != sub {
		t.Fatalf("%+v %v", state, err)
	}
	other, err := Subscribe(fx, store, sessionT, probeID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Unsubscribe(fx, store, sessionS, other); err == nil {
		t.Fatal("a session removed another session's subscription")
	}
	if _, err := Unsubscribe(fx, store, sessionT, other); err != nil {
		t.Fatal(err)
	}
	if err := StopProbe(fx, store, sessionT, probeID); err == nil {
		t.Fatal("a session that did not create the probe stopped it")
	}
	if err := StopProbe(fx, store, sessionS, probeID); err != nil {
		t.Fatal(err)
	}
	if _, err := Subscribe(fx, store, sessionT, probeID); err == nil {
		t.Fatal("an ended probe took a subscription")
	}
	if _, err := Subscribe(fx, store, sessionT, "0123456789abcdef"); err == nil {
		t.Fatal("an unknown probe took a subscription")
	}
	list, err := RenderList(store)
	if err != nil || !strings.Contains(list, probeID) {
		t.Fatalf("list:\n%s\n%v", list, err)
	}
}

func TestCreateRefusalsWriteNothing(t *testing.T) {
	store, dir := setUp(t)
	fx := testkit.FX()
	unknown := "11111111-1111-4111-8111-111111111111"
	zero := int64(0)
	relative := "rel/path"
	cases := map[string]CreateRequest{
		"unknown type":               {ProbeType: "cpu"},
		"unknown watched session":    {ProbeType: "oom-kill", WatchSession: &unknown},
		"until ends without a watch": {ProbeType: "oom-kill", UntilWatchedEnds: true},
		"zero count":                 {ProbeType: "oom-kill", UntilCount: &zero},
		"relative until-file":        {ProbeType: "oom-kill", UntilFile: &relative},
	}
	for name, req := range cases {
		req.Session, req.DeadlineSeconds, req.NowMS = sessionS, 60, launched*1000
		if _, _, err := CreateProbe(fx, store, dir, req); err == nil {
			t.Errorf("%s: created", name)
		}
	}
	if entries, _ := os.ReadDir(store.Root()); len(entries) != 0 {
		t.Fatalf("a refusal wrote %v", entries)
	}
}

func TestParseDuration(t *testing.T) {
	good := map[string]int64{"90s": 90, "30m": 1800, "2h": 7200, "7d": 604800}
	for text, want := range good {
		if got, err := ParseDuration(text); err != nil || got != want {
			t.Errorf("%s: %d %v", text, got, err)
		}
	}
	for _, bad := range []string{"", "soon", "1", "1.5h", "-2h", "0s", "2 h"} {
		if _, err := ParseDuration(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}
