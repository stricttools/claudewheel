package launch

import (
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"golang.org/x/sys/unix"

	"github.com/stricttools/claudewheel/internal/effects"
)

// preLaunchStage is the name prefix of the user's pre-launch hook scripts.
const preLaunchStage = "pre-launch"

// hookTimeout is how long one user hook may run.
const hookTimeout = 10 * time.Second

// userHooks returns the executable files in hooksDir whose names start with
// stage, sorted by name; none when hooksDir does not exist.
func userHooks(hooksDir, stage string) ([]string, error) {
	entries, err := os.ReadDir(hooksDir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var hooks []string
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), stage) {
			continue
		}
		path := filepath.Join(hooksDir, e.Name())
		if unix.Access(path, unix.X_OK) == nil {
			hooks = append(hooks, path)
		}
	}
	slices.Sort(hooks)
	return hooks, nil
}

// hookEnv is this process's environment with one CL_<KEY> variable per
// selection (the segment key in capitals).
func hookEnv(selections map[string]string) []string {
	env := os.Environ()
	for _, key := range slices.Sorted(maps.Keys(selections)) {
		env = setEnv(env, "CL_"+strings.ToUpper(key), selections[key])
	}
	return env
}

// RunUserHooks runs the user's hook scripts for stage from hooksDir in name
// order, each given the selections as CL_<KEY> variables and hookTimeout to
// finish. The first one that fails, exits nonzero, or times out stops the
// run with an error carrying what it wrote to stderr.
func RunUserHooks(fx *effects.FX, hooksDir, stage string, selections map[string]string) error {
	hooks, err := userHooks(hooksDir, stage)
	if err != nil {
		return err
	}
	env := hookEnv(selections)
	for _, hook := range hooks {
		name := filepath.Base(hook)
		res, err := fx.Run(effects.Cmd{Argv: []string{hook}, Env: env, Capture: true, Timeout: hookTimeout})
		if errors.Is(err, effects.ErrTimedOut) {
			return fmt.Errorf("hook '%s' timed out after %s", name, hookTimeout)
		}
		if err != nil {
			return fmt.Errorf("hook '%s': %w", name, err)
		}
		if res.Recorded() {
			continue
		}
		if code := res.ExitCode(); code != 0 {
			msg := fmt.Sprintf("hook '%s' failed (exit %d)", name, code)
			if stderr := strings.TrimSpace(res.Stderr()); stderr != "" {
				msg += ":\n" + stderr
			}
			return errors.New(msg)
		}
	}
	return nil
}
