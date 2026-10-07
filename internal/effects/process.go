package effects

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"
)

// ErrTimedOut is wrapped by the error a Run, an HTTPRead, or a Download
// returns when its Timeout passed. A timed-out child has been killed.
var ErrTimedOut = errors.New("timed out")

// ErrNotFound is wrapped by the error returned when a program named without a
// slash is on no directory of the PATH it is looked up in.
var ErrNotFound = exec.ErrNotFound

// timeoutWaitDelay bounds how long a killed child's output pipes are drained
// when a grandchild holds them open.
const timeoutWaitDelay = 5 * time.Second

// Cmd describes one child process. Argv runs with no shell in between; a
// program named without a slash is looked up on the PATH of Env (of this
// process's environment when Env is nil).
type Cmd struct {
	Argv []string
	// Dir is the child's working directory; empty means this process's.
	Dir string
	// Env is the child's complete environment in os.Environ form. Nil
	// inherits this process's environment (Run only: Exec and RunPTY require
	// it).
	Env []string
	// Stdin is written to the child's stdin, which is then closed. Nil gives
	// the child this process's stdin. Run only.
	Stdin []byte
	// Timeout kills the child once it has passed and makes Run return an
	// error wrapping ErrTimedOut; zero means no timeout. Run only.
	Timeout time.Duration
	// Capture collects stdout and stderr into the Result instead of letting
	// the child write to this process's streams. Run only.
	Capture bool
	// Check makes a nonzero exit an *ExitError. Run only.
	Check bool
	// Read declares that the child changes nothing: it runs in every mode,
	// read-only and --dry-run included, and is never recorded. Run only.
	Read bool
	// Resource, SkipIfCurrent, and Grant annotate the dry-run record: the
	// resource the run produces, the token naming when the caller skips it,
	// and a grant declared on the running command.
	Resource      string
	SkipIfCurrent string
	Grant         string
	// Redact lists values (tokens) replaced with RedactedMarker in the
	// dry-run record and in every error the call returns.
	Redact []string
}

// validate refuses a Cmd no operation can run; method names the operation
// in the error.
func (c Cmd) validate(method string) error {
	if len(c.Argv) == 0 || c.Argv[0] == "" {
		return fmt.Errorf("effects.%s: empty argv", method)
	}
	if c.Timeout < 0 {
		return fmt.Errorf("effects.%s %s: negative timeout %s", method, c.Argv[0], c.Timeout)
	}
	return validateRedact(c.Redact, "effects."+method+" "+c.Argv[0])
}

// validateReplacing refuses the fields only Run honors, for Exec and RunPTY,
// which also need the child's complete environment.
func (c Cmd) validateReplacing(method string) error {
	if err := c.validate(method); err != nil {
		return err
	}
	if c.Env == nil {
		return fmt.Errorf("effects.%s %s: Env is required: the child gets the complete environment it is given", method, c.Argv[0])
	}
	if c.Stdin != nil || c.Timeout != 0 || c.Capture || c.Check || c.Read {
		return fmt.Errorf("effects.%s %s: Stdin, Timeout, Capture, Check, and Read are Run fields", method, c.Argv[0])
	}
	return nil
}

// shown is the argv as one line, redacted.
func (c Cmd) shown() string {
	return Redact(strings.Join(c.Argv, " "), c.Redact...)
}

// Result is the outcome of a child that ran to completion. A nonzero exit is
// a Result, not an error, unless the Cmd set Check.
//
// A Result from a recorded run (Recorded reports true) stands for a child
// that never ran: reading ExitCode, Stdout, or Stderr from it ends the
// dry-run preview there, through strictcli's truncation, because no value
// exists. Callers that do not need the output return before reading it.
type Result struct {
	recorded bool
	carrier  strictcli.Completed
	exitCode int
	stdout   string
	stderr   string
}

// Recorded reports whether the run was recorded in a dry run instead of
// performed.
func (r Result) Recorded() bool { return r.recorded }

// ExitCode is the child's exit status, or the negated signal number when a
// signal ended it.
func (r Result) ExitCode() int {
	if r.recorded {
		return r.carrier.ExitCode()
	}
	return r.exitCode
}

// Stdout is the child's captured stdout as raw bytes (empty unless Capture
// was set); for RunPTY, everything the child wrote to its terminal.
func (r Result) Stdout() string {
	if r.recorded {
		return r.carrier.Stdout()
	}
	return r.stdout
}

// Stderr is the child's captured stderr as raw bytes (empty unless Capture
// was set).
func (r Result) Stderr() string {
	if r.recorded {
		return r.carrier.Stderr()
	}
	return r.stderr
}

// ExitError is a checked run's nonzero exit.
type ExitError struct {
	// Command is the argv as one line, redacted.
	Command string
	Code    int
	// Stderr is the captured stderr (empty unless Capture was set).
	Stderr string
}

func (e *ExitError) Error() string {
	return fmt.Sprintf("%s exited with status %d", e.Command, e.Code)
}

// Run runs c to completion. A declared read (c.Read) runs in every mode; any
// other run is a mutation: refused on a read-only FX, recorded under
// --dry-run, and performed otherwise.
func (fx *FX) Run(c Cmd) (Result, error) {
	if err := c.validate("Run"); err != nil {
		return Result{}, err
	}
	if c.Read {
		return runDirect(c)
	}
	if err := fx.admit("run "+strings.Join(c.Argv, " "), c.Redact); err != nil {
		return Result{}, err
	}
	if fx.handle != nil {
		opts := append(recordOptions(c.Resource, c.SkipIfCurrent, c.Grant, c.Redact),
			strictcli.Check(false), strictcli.Stream(!c.Capture))
		if c.Dir != "" {
			opts = append(opts, strictcli.Cwd(c.Dir))
		}
		done, err := fx.handle.Run(operands(c.Argv), opts...)
		if err != nil {
			return Result{}, err
		}
		return Result{recorded: true, carrier: done}, nil
	}
	return runDirect(c)
}

// runDirect performs c with the full process semantics.
func runDirect(c Cmd) (Result, error) {
	path, err := lookPath(c.Argv[0], c.Env)
	if err != nil {
		return Result{}, RedactError(err, c.Redact...)
	}
	ctx := context.Background()
	if c.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, c.Timeout)
		defer cancel()
	}
	cmd := exec.CommandContext(ctx, path)
	cmd.Args = c.Argv
	cmd.Dir = c.Dir
	cmd.Env = c.Env
	var timedOut atomic.Bool
	if c.Timeout > 0 {
		cmd.Cancel = func() error {
			err := cmd.Process.Kill()
			if err == nil {
				timedOut.Store(true)
			}
			return err
		}
		cmd.WaitDelay = timeoutWaitDelay
	}
	if c.Stdin != nil {
		cmd.Stdin = bytes.NewReader(c.Stdin)
	} else {
		cmd.Stdin = os.Stdin
	}
	var outBuf, errBuf bytes.Buffer
	if c.Capture {
		cmd.Stdout = &outBuf
		cmd.Stderr = &errBuf
	} else {
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
	}
	runErr := cmd.Run()
	if timedOut.Load() {
		return Result{}, fmt.Errorf("%s: %w after %s", c.shown(), ErrTimedOut, c.Timeout)
	}
	var exitErr *exec.ExitError
	if runErr != nil && !errors.As(runErr, &exitErr) {
		return Result{}, RedactError(fmt.Errorf("%s: %w", c.shown(), runErr), c.Redact...)
	}
	code := exitCode(cmd.ProcessState)
	if c.Check && code != 0 {
		return Result{}, &ExitError{Command: c.shown(), Code: code, Stderr: Redact(errBuf.String(), c.Redact...)}
	}
	return Result{exitCode: code, stdout: outBuf.String(), stderr: errBuf.String()}, nil
}

// exitCode is a finished child's exit status, or its negated signal number
// when a signal ended it.
func exitCode(state *os.ProcessState) int {
	if ws, ok := state.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return -int(ws.Signal())
	}
	return state.ExitCode()
}

// defaultExecPath is the search path when the environment sets no PATH, the
// same one Python's os.defpath gives on POSIX.
const defaultExecPath = "/bin:/usr/bin"

// lookPath resolves a program the way execvpe does: a name holding a slash is
// used as is, and any other name is searched on the PATH of env (of this
// process's environment when env is nil).
func lookPath(name string, env []string) (string, error) {
	if strings.Contains(name, "/") {
		return name, nil
	}
	var search string
	var ok bool
	if env == nil {
		search, ok = os.LookupEnv("PATH")
	} else {
		search, ok = envValue(env, "PATH")
	}
	if !ok {
		search = defaultExecPath
	}
	for _, dir := range filepath.SplitList(search) {
		if dir == "" {
			dir = "."
		}
		candidate := dir + "/" + name
		info, err := os.Stat(candidate)
		if err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0o111 != 0 {
			return candidate, nil
		}
	}
	return "", &exec.Error{Name: name, Err: exec.ErrNotFound}
}

// envValue is the value of key in env, the last entry winning as it does for
// a child.
func envValue(env []string, key string) (string, bool) {
	value, found := "", false
	for _, entry := range env {
		if k, v, ok := strings.Cut(entry, "="); ok && k == key {
			value, found = v, true
		}
	}
	return value, found
}

// Exec changes to c.Dir and replaces this process with c.Argv under c.Env.
// On success it does not return. Under --dry-run it records the replacement
// as the run it effectively is and returns nil, so the dispatch can finish
// rendering its preview. Dir and Env are required.
func (fx *FX) Exec(c Cmd) error {
	if err := c.validateReplacing("Exec"); err != nil {
		return err
	}
	if c.Dir == "" {
		return fmt.Errorf("effects.Exec %s: Dir is required", c.Argv[0])
	}
	if err := fx.admit("exec "+strings.Join(c.Argv, " "), c.Redact); err != nil {
		return err
	}
	if fx.handle != nil {
		opts := append(recordOptions(c.Resource, c.SkipIfCurrent, c.Grant, c.Redact),
			strictcli.Cwd(c.Dir), strictcli.Check(false), strictcli.Stream(true))
		_, err := fx.handle.Run(operands(c.Argv), opts...)
		return err
	}
	if err := os.Chdir(c.Dir); err != nil {
		return RedactError(err, c.Redact...)
	}
	path, err := lookPath(c.Argv[0], c.Env)
	if err != nil {
		return RedactError(err, c.Redact...)
	}
	err = syscall.Exec(path, c.Argv, c.Env)
	return RedactError(fmt.Errorf("exec %s: %w", c.shown(), err), c.Redact...)
}

// Kill sends sig to the process pid. A process that is already gone is an
// error wrapping syscall.ESRCH, one that is not ours wraps syscall.EPERM.
// Under --dry-run it records the kill command that performs it.
func (fx *FX) Kill(pid int, sig syscall.Signal) error {
	argv := []string{"kill", "-" + strconv.Itoa(int(sig)), strconv.Itoa(pid)}
	if err := fx.admit(strings.Join(argv, " "), nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Run(operands(argv))
		return err
	}
	if err := syscall.Kill(pid, sig); err != nil {
		return fmt.Errorf("kill %d: %w", pid, err)
	}
	return nil
}

// Follower is a long-running declared read started by Follow, whose stdout
// is read line by line. The caller owns it and ends it with Stop.
type Follower struct {
	cmd    *exec.Cmd
	pipe   *os.File
	lines  *bufio.Reader
	exited chan struct{}
}

// Follow starts argv as a long-running declared read with its stdout piped
// back, such as `journalctl --follow`. It changes nothing, so it starts in
// every mode and is never recorded. The child shares this process's stdin and
// stderr and inherits its environment.
func (fx *FX) Follow(argv []string) (*Follower, error) {
	c := Cmd{Argv: argv}
	if err := c.validate("Follow"); err != nil {
		return nil, err
	}
	path, err := lookPath(argv[0], nil)
	if err != nil {
		return nil, err
	}
	pr, pw, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(path)
	cmd.Args = argv
	cmd.Stdin = os.Stdin
	cmd.Stdout = pw
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		pr.Close()
		pw.Close()
		return nil, fmt.Errorf("%s: %w", c.shown(), err)
	}
	// The child holds the write end now; closing ours makes the read end
	// report the end once the child is gone.
	pw.Close()
	f := &Follower{cmd: cmd, pipe: pr, lines: bufio.NewReader(pr), exited: make(chan struct{})}
	go func() {
		defer close(f.exited)
		cmd.Wait()
	}()
	return f, nil
}

// PID is the child's process id.
func (f *Follower) PID() int { return f.cmd.Process.Pid }

// ReadLine returns the next line the child wrote, without its newline. After
// the child closed its stdout it returns io.EOF (a last line without a
// newline is returned first); after Stop it returns the closed pipe's error.
func (f *Follower) ReadLine() (string, error) {
	line, err := f.lines.ReadString('\n')
	if err == io.EOF && line != "" {
		return line, nil
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSuffix(line, "\n"), nil
}

// Stop ends the child: SIGTERM, then SIGKILL when it has not exited within
// grace. It returns once the child is reaped, and closes the pipe.
func (f *Follower) Stop(grace time.Duration) error {
	defer f.pipe.Close()
	if err := f.cmd.Process.Signal(syscall.SIGTERM); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return err
	}
	select {
	case <-f.exited:
		return nil
	case <-time.After(grace):
	}
	if err := f.cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return err
	}
	<-f.exited
	return nil
}
