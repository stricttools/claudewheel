package profiles

import (
	"errors"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/sessions"
)

// Measuring and stopping the processes holding a profile, for the deletion
// checklist and the sessions overview.
const (
	// RSSFormat is ps's output format: pid first, since ps answers in its own
	// order, and "=" on each field to suppress the header.
	RSSFormat = "pid=,rss="
	// ProcessProbeTimeout bounds a measurement or a stop command.
	ProcessProbeTimeout = 5 * time.Second
	// ExitTimeout is how long a signalled process is waited on.
	ExitTimeout = 5 * time.Second
	// ExitPoll is how often the exit wait asks.
	ExitPoll = 100 * time.Millisecond
)

// ResidentMemory measures the resident set size in KiB of each of pids in
// one `ps` call, a declared read. A pid ps does not report is absent from
// the result: the process is gone. ps exits nonzero when it matched no pid,
// which is an empty answer, not an error; ps failing to run is an error.
// Memory is never summed across a process tree, where shared pages would
// count once per member.
func ResidentMemory(fx *effects.FX, pids []int) (map[int]int, error) {
	measured := map[int]int{}
	if len(pids) == 0 {
		return measured, nil
	}
	list := make([]string, len(pids))
	for i, p := range pids {
		list[i] = strconv.Itoa(p)
	}
	result, err := fx.Run(effects.Cmd{
		Argv:    []string{"ps", "-o", RSSFormat, "-p", strings.Join(list, ",")},
		Capture: true,
		Timeout: ProcessProbeTimeout,
		Read:    true,
	})
	if err != nil {
		return nil, err
	}
	for _, line := range strings.Split(result.Stdout(), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 || !allDigits(fields[0]) || !allDigits(fields[1]) {
			continue
		}
		pid, err1 := strconv.Atoi(fields[0])
		rss, err2 := strconv.Atoi(fields[1])
		if err1 != nil || err2 != nil {
			continue
		}
		measured[pid] = rss
	}
	return measured, nil
}

func allDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return s != ""
}

// StopDaemon stops the Claude Code daemon of configDir with `<binary> daemon
// stop --any --keep-workers`, reporting whether it exited 0. The daemon is
// addressed by the config directory (its socket directory derives from it),
// so CLAUDE_CONFIG_DIR is forced to configDir in environ (os.Environ form);
// --keep-workers keeps detached sessions nobody chose to stop. Under
// --dry-run the stop is recorded and reports false: nothing was stopped.
func StopDaemon(fx *effects.FX, binary, configDir string, environ []string) (bool, error) {
	env := LaunchEnv{Set: map[string]string{ConfigDirVar: configDir}}.Apply(environ)
	result, err := fx.Run(effects.Cmd{
		Argv:    []string{binary, "daemon", "stop", "--any", "--keep-workers"},
		Env:     env,
		Capture: true,
		Timeout: ProcessProbeTimeout,
	})
	if err != nil {
		return false, err
	}
	if result.Recorded() {
		return false, nil
	}
	return result.ExitCode() == 0, nil
}

// Terminate sends SIGTERM to pid. A process already gone is not an error:
// it no longer holds the profile. A signal not permitted is.
func Terminate(fx *effects.FX, pid int) error {
	err := fx.Kill(pid, syscall.SIGTERM)
	if errors.Is(err, syscall.ESRCH) {
		return nil
	}
	return err
}

// WaitForExit polls until pid is gone, reporting false when it is still
// there after timeout.
func WaitForExit(pid int, timeout, poll time.Duration) bool {
	return waitForExit(pid, timeout, poll, sessions.PIDExists, time.Sleep, time.Now)
}

// waitForExit is WaitForExit with its liveness probe, sleep, and clock
// injected.
func waitForExit(pid int, timeout, poll time.Duration, alive func(int) bool, sleep func(time.Duration), now func() time.Time) bool {
	deadline := now().Add(timeout)
	for {
		if !alive(pid) {
			return true
		}
		if !now().Before(deadline) {
			return false
		}
		sleep(poll)
	}
}
