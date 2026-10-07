// Package terminal does raw terminal I/O on /dev/tty: cbreak mode and its
// restore, key decoding with escape sequences, the alternate screen, the
// terminal size and its changes, the OSC 11 background and mode 2031
// color-scheme queries, and the ANSI sequences the TUI draws with.
//
// The terminal is opened through /dev/tty rather than stdin, so a piped stdin
// does not disable the TUI. A Terminal is used from one goroutine.
//
// Every key read takes a context and returns its cancellation cause as soon
// as it is done. The intended shape of a caller:
//
//	ctx, stop := terminal.WithSignals(ctx)
//	defer stop()
//	t, err := terminal.Open()
//	if err != nil { return err }
//	defer t.Close() // restores the terminal, also after Ctrl-C or a panic
//	if err := t.EnterRaw(true); err != nil { return err }
//	key, err := t.ReadKey(ctx) // errors.Is(err, terminal.ErrInterrupted) after Ctrl-C
package terminal

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sys/unix"
)

const ttyPath = "/dev/tty"

// Terminal is an open /dev/tty.
type Terminal struct {
	fd int

	// Rows and Cols hold the size measured by Open, EnterRaw, and the last
	// KeyResize ReadKey returned.
	Rows int
	Cols int

	savedAttrs        *unix.Termios
	inRaw             bool
	altScreen         bool
	mode2031Requested bool

	// pushback holds bytes read ahead that belong to the next key.
	pushback []byte

	// The wake pipe interrupts a poll on the tty when the context is
	// cancelled or SIGWINCH arrives.
	wakeR, wakeW int
	resized      atomic.Bool
	winch        chan os.Signal
	done         chan struct{}
	watcher      sync.WaitGroup
	closed       bool
}

// Open opens /dev/tty, measures its size, and starts watching SIGWINCH. The
// caller defers Close.
func Open() (*Terminal, error) {
	fd, err := unix.Open(ttyPath, unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", ttyPath, err)
	}
	pipe := make([]int, 2)
	if err := unix.Pipe2(pipe, unix.O_CLOEXEC|unix.O_NONBLOCK); err != nil {
		unix.Close(fd)
		return nil, fmt.Errorf("create the terminal wake pipe: %w", err)
	}
	t := &Terminal{
		fd:    fd,
		wakeR: pipe[0],
		wakeW: pipe[1],
		winch: make(chan os.Signal, 1),
		done:  make(chan struct{}),
	}
	rows, cols, err := t.Size()
	if err != nil {
		unix.Close(pipe[0])
		unix.Close(pipe[1])
		unix.Close(fd)
		return nil, err
	}
	t.Rows, t.Cols = rows, cols
	signal.Notify(t.winch, unix.SIGWINCH)
	t.watcher.Add(1)
	go func() {
		defer t.watcher.Done()
		for {
			select {
			case <-t.winch:
				t.resized.Store(true)
				t.wake()
			case <-t.done:
				return
			}
		}
	}()
	return t, nil
}

// HasControllingTerminal reports whether this process has a terminal it can
// prompt at: whether /dev/tty opens. Every interactive surface reaches the
// user through /dev/tty, so an isatty check on stdin or stdout would answer a
// different question. Nothing is read, written, or left open.
func HasControllingTerminal() bool {
	fd, err := unix.Open(ttyPath, unix.O_RDWR|unix.O_CLOEXEC, 0)
	if err != nil {
		return false
	}
	unix.Close(fd)
	return true
}

// Size measures the terminal as (rows, cols).
func (t *Terminal) Size() (rows, cols int, err error) {
	ws, err := unix.IoctlGetWinsize(t.fd, unix.TIOCGWINSZ)
	if err != nil {
		return 0, 0, fmt.Errorf("measure the terminal size: %w", err)
	}
	return int(ws.Row), int(ws.Col), nil
}

// Raw reports whether the terminal is in cbreak mode (entered by EnterRaw and
// not yet restored).
func (t *Terminal) Raw() bool {
	return t.inRaw
}

// EnterRaw saves the terminal attributes, enters cbreak mode (no echo, no
// line buffering; Ctrl-C still raises SIGINT), measures the size, and hides
// the cursor, switching to the alternate screen and clearing it when
// altScreen is set. Entering twice without ExitRaw is an error.
func (t *Terminal) EnterRaw(altScreen bool) error {
	if t.inRaw {
		return errors.New("terminal is already in cbreak mode")
	}
	saved, err := setCbreak(t.fd)
	if err != nil {
		return err
	}
	t.savedAttrs = saved
	t.inRaw = true
	t.altScreen = altScreen
	rows, cols, err := t.Size()
	if err != nil {
		return err
	}
	t.Rows, t.Cols = rows, cols
	if altScreen {
		return t.Write(AltScreenOn + HideCursor + ClearScreen)
	}
	return t.Write(HideCursor)
}

// SubscribeMode2031 asks the terminal for mode 2031 color-scheme change
// notifications, which ReadKey reports as KeyThemeDark and KeyThemeLight.
// ExitRaw unsubscribes.
func (t *Terminal) SubscribeMode2031() error {
	t.mode2031Requested = true
	return t.Write(mode2031On)
}

// ExitRaw restores the terminal to the attributes EnterRaw saved, shows the
// cursor, and leaves the alternate screen if EnterRaw entered it. It does
// nothing when the terminal is not raw, so it is safe to call more than once.
func (t *Terminal) ExitRaw() error {
	if !t.inRaw {
		return nil
	}
	t.inRaw = false
	var errs []error
	if t.mode2031Requested {
		t.mode2031Requested = false
		errs = append(errs, t.Write(mode2031Off))
	}
	if err := unix.IoctlSetTermios(t.fd, unix.TCSETSF, t.savedAttrs); err != nil {
		errs = append(errs, fmt.Errorf("restore the terminal attributes: %w", err))
	}
	if t.altScreen {
		errs = append(errs, t.Write(ShowCursor+AltScreenOff))
	} else {
		errs = append(errs, t.Write(ShowCursor))
	}
	return errors.Join(errs...)
}

// Cooked runs fn with the terminal out of cbreak mode, re-entering it
// afterwards with the same alternate-screen choice and the mode 2031
// subscription it had, also when fn fails. On a terminal that is not raw it
// just runs fn, so nesting is safe.
func (t *Terminal) Cooked(fn func() error) error {
	if !t.inRaw {
		return fn()
	}
	altScreen := t.altScreen
	subscribed := t.mode2031Requested
	if err := t.ExitRaw(); err != nil {
		return err
	}
	fnErr := fn()
	if err := t.EnterRaw(altScreen); err != nil {
		return errors.Join(fnErr, err)
	}
	if subscribed {
		return errors.Join(fnErr, t.SubscribeMode2031())
	}
	return fnErr
}

// Write writes text to the terminal.
func (t *Terminal) Write(text string) error {
	if err := writeAll(t.fd, []byte(text)); err != nil {
		return fmt.Errorf("write to the terminal: %w", err)
	}
	return nil
}

// Close restores the terminal (ExitRaw), stops watching SIGWINCH, and closes
// /dev/tty. It is safe to call more than once.
func (t *Terminal) Close() error {
	if t.closed {
		return nil
	}
	t.closed = true
	restoreErr := t.ExitRaw()
	signal.Stop(t.winch)
	close(t.done)
	t.watcher.Wait()
	unix.Close(t.wakeR)
	unix.Close(t.wakeW)
	closeErr := unix.Close(t.fd)
	if closeErr != nil {
		closeErr = fmt.Errorf("close %s: %w", ttyPath, closeErr)
	}
	return errors.Join(restoreErr, closeErr)
}

// setCbreak puts fd in cbreak mode as Python's tty.setcbreak does (ECHO and
// ICANON off, VMIN 1, VTIME 0, flushing pending input) and returns the
// attributes it replaced.
func setCbreak(fd int) (*unix.Termios, error) {
	saved, err := unix.IoctlGetTermios(fd, unix.TCGETS)
	if err != nil {
		return nil, fmt.Errorf("read the terminal attributes: %w", err)
	}
	attrs := *saved
	attrs.Lflag &^= unix.ECHO | unix.ICANON
	attrs.Cc[unix.VMIN] = 1
	attrs.Cc[unix.VTIME] = 0
	if err := unix.IoctlSetTermios(fd, unix.TCSETSF, &attrs); err != nil {
		return nil, fmt.Errorf("enter cbreak mode: %w", err)
	}
	return saved, nil
}

func writeAll(fd int, data []byte) error {
	for len(data) > 0 {
		n, err := unix.Write(fd, data)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			return err
		}
		data = data[n:]
	}
	return nil
}

// wake makes a pending waitInput poll return. A full pipe already holds a
// pending wake, so a failed write needs no handling.
func (t *Terminal) wake() {
	unix.Write(t.wakeW, []byte{0})
}

func (t *Terminal) drainWake() {
	buf := make([]byte, 64)
	for {
		n, err := unix.Read(t.wakeR, buf)
		if n <= 0 || err != nil {
			return
		}
	}
}

type waitResult int

const (
	inputReady waitResult = iota
	inputTimedOut
	inputResized
)

// waitInput waits until a byte can be read from the terminal, the timeout
// passes (a negative timeout waits forever), the context is done (its cause
// is returned), or, when reportResize is set, a SIGWINCH has arrived.
func (t *Terminal) waitInput(ctx context.Context, timeout time.Duration, reportResize bool) (waitResult, error) {
	stopWake := context.AfterFunc(ctx, t.wake)
	defer stopWake()
	var deadline time.Time
	if timeout >= 0 {
		deadline = time.Now().Add(timeout)
	}
	for {
		if ctx.Err() != nil {
			return 0, cancelled(ctx)
		}
		if reportResize && t.resized.Swap(false) {
			return inputResized, nil
		}
		if len(t.pushback) > 0 {
			return inputReady, nil
		}
		pollMs := -1
		if timeout >= 0 {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				return inputTimedOut, nil
			}
			pollMs = int((remaining + time.Millisecond - 1) / time.Millisecond)
		}
		fds := []unix.PollFd{
			{Fd: int32(t.fd), Events: unix.POLLIN},
			{Fd: int32(t.wakeR), Events: unix.POLLIN},
		}
		_, err := unix.Poll(fds, pollMs)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			return 0, fmt.Errorf("wait for terminal input: %w", err)
		}
		if fds[1].Revents != 0 {
			t.drainWake()
		}
		if fds[0].Revents&unix.POLLIN != 0 {
			return inputReady, nil
		}
		if fds[0].Revents&(unix.POLLHUP|unix.POLLERR|unix.POLLNVAL) != 0 {
			return 0, fmt.Errorf("wait for terminal input: %w", io.ErrUnexpectedEOF)
		}
	}
}

// readByte waits without a timeout for the next byte, ignoring SIGWINCH
// (a resize is reported at the next key boundary instead).
func (t *Terminal) readByte(ctx context.Context) (byte, error) {
	if _, err := t.waitInput(ctx, -1, false); err != nil {
		return 0, err
	}
	return t.takeByte()
}

// takeByte reads one byte that waitInput reported ready.
func (t *Terminal) takeByte() (byte, error) {
	if len(t.pushback) > 0 {
		b := t.pushback[0]
		t.pushback = t.pushback[1:]
		return b, nil
	}
	buf := make([]byte, 1)
	for {
		n, err := unix.Read(t.fd, buf)
		if err == unix.EINTR {
			continue
		}
		if err != nil {
			return 0, fmt.Errorf("read from the terminal: %w", err)
		}
		if n == 0 {
			return 0, fmt.Errorf("read from the terminal: %w", io.EOF)
		}
		return buf[0], nil
	}
}
