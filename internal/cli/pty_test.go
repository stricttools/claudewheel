package cli

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stricttools/claudewheel/internal/testkit"
)

func childCommand(dir string, args ...string) *exec.Cmd {
	cmd := exec.Command(os.Args[0], args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), childEnvKey+"=1", "TERM=xterm-256color")
	return cmd
}

func TestLaunchBarDrawsAndQuitsWithoutLaunching(t *testing.T) {
	e := newEnv(t)
	home := filepath.Dir(e.ws.Root())
	testkit.Stub(t, e.stubs, "npm", `echo '["2.1.281"]'`)
	testkit.Stub(t, e.stubs, "gh", "exit 1")
	testkit.Stub(t, e.stubs, "systemd-run", `touch "$HOME/ran"`)
	e.setUp("work")
	p := testkit.StartPTY(t, childCommand(home, "launch"), 30, 120)
	p.Expect("claude (not installed)", 10*time.Second)
	p.Send("\r")
	p.Expect("Profile", 10*time.Second)
	p.Send("q")
	if code := p.Wait(10 * time.Second); code != 0 {
		t.Fatalf("exit %d; output:\n%q", code, p.Output())
	}
	if _, err := os.Stat(filepath.Join(home, "ran")); err == nil {
		t.Fatal("quitting the bar launched the client")
	}
}

func TestCtrlCOnTheLaunchBarExits130(t *testing.T) {
	e := newEnv(t)
	home := filepath.Dir(e.ws.Root())
	testkit.Stub(t, e.stubs, "npm", `echo '["2.1.281"]'`)
	testkit.Stub(t, e.stubs, "gh", "exit 1")
	e.setUp("work")
	p := testkit.StartPTY(t, childCommand(home, "launch", "--client", "claude"), 30, 120)
	p.Expect("Profile", 10*time.Second)
	p.Send("\x03")
	if code := p.Wait(10 * time.Second); code != 130 {
		t.Fatalf("exit %d; output:\n%q", code, p.Output())
	}
}
