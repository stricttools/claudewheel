package hookscripts

import (
	"bytes"
	"encoding/json"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/testkit"
)

// The lines the deployed session hooks write are read back by the Go
// lifecycle store, launch facts and all.
func TestSessionHookLinesReadBackInGo(t *testing.T) {
	testkit.Isolate(t)
	root := t.TempDir()
	scripts := filepath.Join(root, "scripts")
	if _, err := DeployScripts(testkit.FX(), []string{"hook-session-start", "hook-session-end"}, scripts, false); err != nil {
		t.Fatal(err)
	}
	lifecycleDir := filepath.Join(root, "lifecycle")
	env := []string{"PATH=/usr/bin:/bin", "HOME=" + root, "CLAUDE_CONFIG_DIR=" + filepath.Join(root, "config"),
		"CLAUDEWHEEL_LAUNCH_PROFILE=work", "CLAUDEWHEEL_LAUNCH_VERSION=2.1.281", "CLAUDEWHEEL_LAUNCH_MODEL=claude-opus-5",
		"CLAUDEWHEEL_LAUNCH_PERMISSIONS=bypass", "CLAUDEWHEEL_LIFECYCLE_DIR=" + lifecycleDir, "CLAUDE_PID=4242"}
	run := func(script string, payload map[string]any) {
		data, _ := json.Marshal(payload)
		cmd := exec.Command("bash", filepath.Join(scripts, script))
		cmd.Stdin = bytes.NewReader(data)
		cmd.Env = env
		if out, err := cmd.CombinedOutput(); err != nil || len(out) != 0 {
			t.Fatalf("%s: %v %q", script, err, out)
		}
	}
	run("hook-session-start", map[string]any{"session_id": hookSession, "transcript_path": "/t/s.jsonl",
		"cwd": "/home/m/Projects/x", "hook_event_name": "SessionStart", "source": "startup"})
	run("hook-session-end", map[string]any{"session_id": hookSession, "transcript_path": "/t/s.jsonl",
		"cwd": "/home/m/Projects/x", "hook_event_name": "SessionEnd", "reason": "prompt_input_exit"})
	path, _ := lifecycle.SessionFile(lifecycleDir, hookSession)
	events, err := lifecycle.ReadSession(path)
	if err != nil {
		t.Fatal(err)
	}
	summary := lifecycle.Summarize(events, hookSession)
	s := summary.Started
	if s == nil || s.Cwd != "/home/m/Projects/x" || s.Profile == nil || *s.Profile != "work" || s.Model == nil || *s.Model != "claude-opus-5" ||
		s.PID == nil || *s.PID != 4242 || s.Entry != "startup" {
		t.Fatalf("started %+v", s)
	}
	if summary.Ended == nil || summary.Ended.Outcome != "exited" || summary.Ended.Reason == nil || *summary.Ended.Reason != "prompt_input_exit" {
		t.Fatalf("ended %+v", summary.Ended)
	}
}
