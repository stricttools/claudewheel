package effects

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"

	"github.com/stricttools/strictcli/go/strictcli"
	"golang.org/x/sys/unix"
)

// PTY says how RunPTY drives its child.
type PTY struct {
	// Input is written to the child's terminal right after it starts, so an
	// interactive child can be driven without a human typing.
	Input []byte
	// ProxyTerminal connects the real terminal (/dev/tty, so it works with
	// stdin piped) to the child: the terminal goes to full raw mode so every
	// byte, Ctrl-C included, reaches the child; the child's output is shown
	// as well as captured; and window-size changes are passed on. Without
	// it no real terminal is touched and the output is only captured.
	ProxyTerminal bool
}

// RunPTY runs c.Argv under c.Env on a fresh pseudo-terminal and returns its
// exit code with everything it wrote to the terminal as the Result's Stdout.
// SIGTERM or SIGHUP received meanwhile is passed to the child and ends the
// run with an error once the terminal is restored and the child reaped.
// Env is required; Stdin, Timeout, Capture, Check, and Read are Run fields.
// Under --dry-run the run is recorded and its Result is unsettled: no login
// happened, so there is neither an exit code nor output.
func (fx *FX) RunPTY(c Cmd, p PTY) (Result, error) {
	if err := c.validateReplacing("RunPTY"); err != nil {
		return Result{}, err
	}
	if err := fx.admit("run "+strings.Join(c.Argv, " "), c.Redact); err != nil {
		return Result{}, err
	}
	if fx.handle != nil {
		opts := append(recordOptions(c.Resource, c.SkipIfCurrent, c.Grant, c.Redact),
			strictcli.Check(false), strictcli.Stream(true))
		if c.Dir != "" {
			opts = append(opts, strictcli.Cwd(c.Dir))
		}
		done, err := fx.handle.Run(operands(c.Argv), opts...)
		if err != nil {
			return Result{}, err
		}
		return Result{recorded: true, carrier: done}, nil
	}
	code, captured, err := runPTY(c, p)
	if err != nil {
		return Result{}, RedactError(err, c.Redact...)
	}
	return Result{exitCode: code, stdout: string(captured)}, nil
}

// firstError keeps the first error reported by any of the proxy loops.
type firstError struct {
	mu  sync.Mutex
	err error
}

func (f *firstError) set(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err == nil {
		f.err = err
	}
}

func (f *firstError) get() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.err
}

func runPTY(c Cmd, p PTY) (int, []byte, error) {
	var tty *os.File
	if p.ProxyTerminal {
		f, err := os.OpenFile("/dev/tty", os.O_RDWR, 0)
		if err != nil {
			return 0, nil, fmt.Errorf("cannot open /dev/tty: a real terminal is required to proxy the child process (run without ProxyTerminal to capture headless): %w", err)
		}
		tty = f
	}
	closeTTY := func() {
		if tty != nil {
			tty.Close()
		}
	}

	master, slave, err := openPTY()
	if err != nil {
		closeTTY()
		return 0, nil, err
	}
	path, err := lookPath(c.Argv[0], c.Env)
	if err != nil {
		master.Close()
		slave.Close()
		closeTTY()
		return 0, nil, err
	}
	cmd := exec.Command(path)
	cmd.Args = c.Argv
	cmd.Env = c.Env
	cmd.Dir = c.Dir
	cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
	// A new session whose controlling terminal is the slave, which is the
	// child's descriptor 0.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true, Ctty: 0}
	if err := cmd.Start(); err != nil {
		master.Close()
		slave.Close()
		closeTTY()
		return 0, nil, fmt.Errorf("%s: %w", c.shown(), err)
	}
	// Only the child holds the slave now, so the master reads the end of the
	// stream once the child has closed it.
	slave.Close()

	signals := []os.Signal{syscall.SIGTERM, syscall.SIGHUP}
	if tty != nil {
		signals = append(signals, syscall.SIGWINCH)
	}
	sigs := make(chan os.Signal, 4)
	signal.Notify(sigs, signals...)

	var proxyErr firstError
	var saved *unix.Termios
	if tty != nil {
		copyWinsize(tty, master)
		saved, err = makeRaw(tty)
		if err != nil {
			proxyErr.set(err)
		}
	}

	var captured []byte
	outDone := make(chan struct{})
	go func() {
		defer close(outDone)
		buf := make([]byte, 65536)
		for {
			n, err := master.Read(buf)
			if n > 0 {
				captured = append(captured, buf[:n]...)
				if tty != nil {
					if _, werr := tty.Write(buf[:n]); werr != nil {
						proxyErr.set(werr)
						return
					}
				}
			}
			if err != nil {
				// EIO once the child closed the slave: the normal end of
				// the stream on Linux.
				return
			}
		}
	}()

	if proxyErr.get() == nil && len(p.Input) > 0 {
		if _, err := master.Write(p.Input); err != nil {
			proxyErr.set(err)
		}
	}

	// The input loop is not waited for: it ends when the terminal is closed
	// below, or at the next key on a terminal the runtime cannot poll.
	if tty != nil && proxyErr.get() == nil {
		go func() {
			buf := make([]byte, 65536)
			for {
				n, err := tty.Read(buf)
				if n > 0 {
					if _, werr := master.Write(buf[:n]); werr != nil {
						return
					}
				}
				if err != nil {
					return
				}
			}
		}()
	}

	var ended os.Signal
	if proxyErr.get() == nil {
	wait:
		for {
			select {
			case <-outDone:
				break wait
			case s := <-sigs:
				if s == syscall.SIGWINCH {
					copyWinsize(tty, master)
					signalChild(cmd, syscall.SIGWINCH)
					continue
				}
				signalChild(cmd, s.(syscall.Signal))
				ended = s
				break wait
			}
		}
	}

	signal.Stop(sigs)
	if saved != nil {
		if err := restoreTerminal(tty, saved); err != nil {
			proxyErr.set(err)
		}
	}
	closeTTY()
	// Closing the master hangs up the child's terminal, so the wait below
	// ends on every path.
	master.Close()
	<-outDone
	waitErr := cmd.Wait()
	var exitErr *exec.ExitError
	if waitErr != nil && !errors.As(waitErr, &exitErr) {
		return 0, nil, waitErr
	}
	if ended != nil {
		return 0, nil, fmt.Errorf("%s: ended by %s, which was passed to the child", c.shown(), ended)
	}
	if err := proxyErr.get(); err != nil {
		return 0, nil, err
	}
	return exitCode(cmd.ProcessState), captured, nil
}

// signalChild sends sig to the child; one that has already exited is fine.
func signalChild(cmd *exec.Cmd, sig syscall.Signal) {
	_ = cmd.Process.Signal(sig)
}

// openPTY opens a new pseudo-terminal pair: unlock the master's slave, then
// open the slave by its number.
func openPTY() (master, slave *os.File, err error) {
	master, err = os.OpenFile("/dev/ptmx", os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return nil, nil, err
	}
	var number uint32
	ctlErr := control(master, func(fd int) error {
		if err := unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
			return fmt.Errorf("unlock the pseudo-terminal: %w", err)
		}
		n, err := unix.IoctlGetUint32(fd, unix.TIOCGPTN)
		if err != nil {
			return fmt.Errorf("read the pseudo-terminal number: %w", err)
		}
		number = n
		return nil
	})
	if ctlErr != nil {
		master.Close()
		return nil, nil, ctlErr
	}
	slave, err = os.OpenFile("/dev/pts/"+strconv.FormatUint(uint64(number), 10), os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	return master, slave, nil
}

// control runs fn on f's descriptor without taking the file out of the
// runtime poller (File.Fd would put it in blocking mode, and a blocked read
// could then not be ended by Close).
func control(f *os.File, fn func(fd int) error) error {
	raw, err := f.SyscallConn()
	if err != nil {
		return err
	}
	var fnErr error
	if err := raw.Control(func(fd uintptr) { fnErr = fn(int(fd)) }); err != nil {
		return err
	}
	return fnErr
}

// copyWinsize gives dst the window size of src; a failure is ignored, as a
// terminal without a size is no reason to stop the run.
func copyWinsize(src, dst *os.File) {
	var size *unix.Winsize
	if control(src, func(fd int) error {
		var err error
		size, err = unix.IoctlGetWinsize(fd, unix.TIOCGWINSZ)
		return err
	}) != nil {
		return
	}
	_ = control(dst, func(fd int) error {
		return unix.IoctlSetWinsize(fd, unix.TIOCSWINSZ, size)
	})
}

// makeRaw puts the terminal in full raw mode (cfmakeraw, applied after
// flushing pending input) and returns the settings to restore.
func makeRaw(tty *os.File) (*unix.Termios, error) {
	var saved *unix.Termios
	err := control(tty, func(fd int) error {
		current, err := unix.IoctlGetTermios(fd, unix.TCGETS)
		if err != nil {
			return err
		}
		saved = current
		raw := *current
		raw.Iflag &^= unix.IGNBRK | unix.BRKINT | unix.IGNPAR | unix.PARMRK | unix.INPCK | unix.ISTRIP |
			unix.INLCR | unix.IGNCR | unix.ICRNL | unix.IXON | unix.IXANY | unix.IXOFF
		raw.Oflag &^= unix.OPOST
		raw.Lflag &^= unix.ECHO | unix.ECHOE | unix.ECHOK | unix.ECHONL | unix.ICANON | unix.IEXTEN |
			unix.ISIG | unix.NOFLSH | unix.TOSTOP
		raw.Cflag &^= unix.CSIZE | unix.PARENB
		raw.Cflag |= unix.CS8
		raw.Cc[unix.VMIN] = 1
		raw.Cc[unix.VTIME] = 0
		return unix.IoctlSetTermios(fd, unix.TCSETSF, &raw)
	})
	if err != nil {
		return nil, fmt.Errorf("put /dev/tty in raw mode: %w", err)
	}
	return saved, nil
}

// restoreTerminal puts back saved once pending output is written.
func restoreTerminal(tty *os.File, saved *unix.Termios) error {
	err := control(tty, func(fd int) error {
		return unix.IoctlSetTermios(fd, unix.TCSETSW, saved)
	})
	if err != nil {
		return fmt.Errorf("restore /dev/tty: %w", err)
	}
	return nil
}
