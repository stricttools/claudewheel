package hookscripts

import (
	"bytes"
	"encoding/json"
	"errors"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/testkit"
)

const hookSession = "4d97ca01-9d56-4f49-8047-77f5160febde"

// probeHookRun runs the waiter hook over a workspace root whose probe store
// Go wrote, with a live process standing in for the client.
func probeHookRun(t *testing.T, root string, interactive bool) (int, string) {
	t.Helper()
	scripts := filepath.Join(root, "scripts")
	if _, err := DeployScripts(testkit.FX(), Names(), scripts, false); err != nil {
		t.Fatal(err)
	}
	client := exec.Command("sleep", "60")
	if err := client.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Process.Kill(); _ = client.Wait() })
	attended, entry := "1", "cli"
	if !interactive {
		attended, entry = "0", "sdk-cli"
	}
	payload, _ := json.Marshal(map[string]any{"session_id": hookSession, "hook_event_name": "Stop", "stop_hook_active": false})
	cmd := exec.Command("bash", filepath.Join(scripts, "hook-wait-for-probe-reports"))
	cmd.Stdin = bytes.NewReader(payload)
	cmd.Env = []string{"PATH=/usr/bin:/bin", "HOME=" + filepath.Dir(root), "CLAUDEWHEEL_CONFIG_DIR=" + root,
		"CLAUDE_PID=" + strconv.Itoa(client.Process.Pid), "CLAUDE_CODE_SESSION_ATTENDED=" + attended, "CLAUDE_CODE_ENTRYPOINT=" + entry}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	done := make(chan error, 1)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return exitErr.ExitCode(), stderr.String()
		}
		if err != nil {
			t.Fatal(err)
		}
		return 0, stderr.String()
	case <-time.After(20 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatalf("the waiter did not exit; stderr %q", stderr.String())
		return 0, ""
	}
}

func queueReports(t *testing.T, store probe.Store) {
	t.Helper()
	for id, text := range map[string]string{"aaaaaaaaaaaaaaaa": "first report", "bbbbbbbbbbbbbbbb": "second report"} {
		_, err := probe.WriteReport(testkit.FX(), store, probe.Report{
			ID: id, At: "2026-10-07T10:00:00.000Z", Session: hookSession, Kill: "cccccccccccccccc",
			Text: "[claudewheel probe report " + id + "] " + text,
		}, probe.RecipientOf(nil))
		if err != nil {
			t.Fatal(err)
		}
	}
}

// The waiter hook hands over the reports the Go store wrote by exiting 2,
// and moves them to handed, where the Go runner settles them.
func TestTheWaiterHandsOverReportsGoWrote(t *testing.T) {
	testkit.Isolate(t)
	root := filepath.Join(t.TempDir(), ".claudewheel")
	store := probe.NewStore(filepath.Join(root, "shared", "probes"))
	queueReports(t, store)
	code, stderr := probeHookRun(t, root, true)
	if code != 2 || !strings.Contains(stderr, "first report") || !strings.Contains(stderr, "second report") {
		t.Fatalf("exit %d, stderr %q", code, stderr)
	}
	pending, err := probe.ListReports(store, []string{"pending"})
	if err != nil || len(pending) != 0 {
		t.Fatalf("pending %v %v", pending, err)
	}
	handed, err := probe.ListReports(store, []string{"handed"})
	if err != nil || len(handed) != 2 {
		t.Fatalf("handed %v %v", handed, err)
	}
}

func TestANonInteractiveSessionGetsNoWaiter(t *testing.T) {
	testkit.Isolate(t)
	root := filepath.Join(t.TempDir(), ".claudewheel")
	store := probe.NewStore(filepath.Join(root, "shared", "probes"))
	queueReports(t, store)
	if code, stderr := probeHookRun(t, root, false); code != 0 || stderr != "" {
		t.Fatalf("exit %d, stderr %q", code, stderr)
	}
	if pending, _ := probe.ListReports(store, []string{"pending"}); len(pending) != 2 {
		t.Fatalf("pending %v", pending)
	}
}
