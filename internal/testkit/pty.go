package testkit

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// PTY is a child process running on its own pseudo-terminal, which is its
// controlling terminal, stdin, stdout, and stderr.
type PTY struct {
	t      *testing.T
	master *os.File
	cmd    *exec.Cmd
	mu     sync.Mutex
	out    bytes.Buffer
	done   chan struct{}
	exit   error
}

// StartPTY starts cmd on a new pseudo-terminal of rows by cols.
func StartPTY(t *testing.T, cmd *exec.Cmd, rows, cols uint16) *PTY {
	t.Helper()
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	fd := int(master.Fd())
	if err := unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
		t.Fatal(err)
	}
	n, err := unix.IoctlGetInt(fd, unix.TIOCGPTN)
	if err != nil {
		t.Fatal(err)
	}
	slave, err := os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.IoctlSetWinsize(int(slave.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: rows, Col: cols}); err != nil {
		t.Fatal(err)
	}
	cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	slave.Close()
	p := &PTY{t: t, master: master, cmd: cmd, done: make(chan struct{})}
	go func() {
		buf := make([]byte, 4096)
		for {
			n, err := master.Read(buf)
			p.mu.Lock()
			p.out.Write(buf[:n])
			p.mu.Unlock()
			if err != nil {
				return
			}
		}
	}()
	go func() {
		p.exit = cmd.Wait()
		close(p.done)
	}()
	t.Cleanup(func() {
		select {
		case <-p.done:
		default:
			_ = cmd.Process.Kill()
			<-p.done
		}
		master.Close()
	})
	return p
}

// Output returns everything the child has written so far.
func (p *PTY) Output() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.out.String()
}

// Expect waits up to timeout for text to appear in the output.
func (p *PTY) Expect(text string, timeout time.Duration) {
	p.t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if strings.Contains(p.Output(), text) {
			return
		}
		select {
		case <-p.done:
			if strings.Contains(p.Output(), text) {
				return
			}
			p.t.Fatalf("the child exited (%v) without writing %q; output:\n%q", p.exit, text, p.Output())
		case <-time.After(20 * time.Millisecond):
		}
	}
	p.t.Fatalf("no %q within %v; output:\n%q", text, timeout, p.Output())
}

// Send types text into the terminal.
func (p *PTY) Send(text string) {
	p.t.Helper()
	if _, err := p.master.WriteString(text); err != nil {
		p.t.Fatal(err)
	}
}

// Wait waits up to timeout for the child to exit and returns its exit status.
func (p *PTY) Wait(timeout time.Duration) int {
	p.t.Helper()
	select {
	case <-p.done:
	case <-time.After(timeout):
		p.t.Fatalf("the child did not exit within %v; output:\n%q", timeout, p.Output())
	}
	if exitErr, ok := p.exit.(*exec.ExitError); ok {
		return exitErr.ExitCode()
	}
	if p.exit != nil {
		p.t.Fatal(p.exit)
	}
	return 0
}
