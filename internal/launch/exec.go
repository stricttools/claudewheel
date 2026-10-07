package launch

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode"

	"golang.org/x/sys/unix"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/hookscripts"
)

// sizeRE is a size as systemd reads it for MemoryMax and MemorySwapMax,
// spelled as heavy's --mem is: a whole number with a K, M, G, or T suffix.
var sizeRE = regexp.MustCompile(`^[1-9][0-9]*[KMGT]$`)

// ToolCap is the memory cap a launched session's Bash commands share:
// every Bash command the session runs, and everything it starts, runs in the
// session's tools slice, which caps them together, so a runaway command is
// killed while the session goes on. Claude Code itself is not capped.
type ToolCap struct {
	MemoryMax     string
	MemorySwapMax string
}

// ToolCapFrom reads the cap from config.json's tool_memory_max and
// tool_memory_swap_max; a malformed value is an error.
func ToolCapFrom(cfg appconfig.Config) (ToolCap, error) {
	if !sizeRE.MatchString(cfg.ToolMemoryMax) {
		return ToolCap{}, fmt.Errorf("config.json tool_memory_max takes a whole number with a K, M, G, or T suffix (such as 6G), not %q", cfg.ToolMemoryMax)
	}
	if cfg.ToolMemorySwapMax != "0" && !sizeRE.MatchString(cfg.ToolMemorySwapMax) {
		return ToolCap{}, fmt.Errorf("config.json tool_memory_swap_max takes a whole number with a K, M, G, or T suffix (such as 6G), or 0 for none, not %q", cfg.ToolMemorySwapMax)
	}
	return ToolCap{MemoryMax: cfg.ToolMemoryMax, MemorySwapMax: cfg.ToolMemorySwapMax}, nil
}

// sessionSliceRE matches a session slice a launch starts sessions in; the
// groups are the process id and the launch second.
var sessionSliceRE = regexp.MustCompile(`^claudewheel-(\d+)_(\d+)\.slice$`)

// SessionUnits are the systemd user units one launched session runs in,
// named from the Claude Code process id (the launcher's own, which the exec
// keeps) and the launch second:
//
//   - claudewheel-<pid>_<time>.slice: the session, uncapped;
//   - claudewheel-session-<pid>-<time>.scope, inside it: Claude Code and its
//     hooks;
//   - claudewheel-<pid>_<time>-tools.slice, inside it: the ToolCap, shared by
//     one claudewheel-tool-<pid>-<time>-<n>.scope per Bash command.
//
// A slice's dashes name its parents, so the tools slice sits inside the
// session slice by its name alone. The launcher starts the session scope;
// the claudewheel-tool-scope shell prefix creates the tools slice with its
// cap at the session's first Bash command and starts each tool scope.
type SessionUnits struct {
	PID      int
	Launched int64
}

// SessionSlice is the session's slice.
func (u SessionUnits) SessionSlice() string {
	return fmt.Sprintf("claudewheel-%d_%d.slice", u.PID, u.Launched)
}

// SessionScope is the scope Claude Code runs in.
func (u SessionUnits) SessionScope() string {
	return fmt.Sprintf("claudewheel-session-%d-%d.scope", u.PID, u.Launched)
}

// ToolSlice is the slice the session's Bash commands share.
func (u SessionUnits) ToolSlice() string {
	return fmt.Sprintf("claudewheel-%d_%d-tools.slice", u.PID, u.Launched)
}

// Argv starts clientArgv inside the session scope, in the session slice.
// --scope makes systemd-run exec the client in place, so the session keeps
// this process's PID, terminal, and environment. --collect unloads the scope
// when it ends, failed or not. --expand-environment=no passes the client's
// arguments through as they are (systemd-run would otherwise expand $VAR
// and $$ in them, rewriting a prompt that mentions a price). OOMPolicy=continue
// keeps the session alive when the kernel kills one of its processes for
// lack of memory; without it systemd stops the whole scope on the first kill.
func (u SessionUnits) Argv(systemdRun string, clientArgv []string) []string {
	argv := []string{
		systemdRun,
		"--user",
		"--scope",
		"--quiet",
		"--collect",
		"--expand-environment=no",
		"--slice=" + u.SessionSlice(),
		"--unit=" + u.SessionScope(),
		"-p",
		"OOMPolicy=continue",
		"--",
	}
	return append(argv, clientArgv...)
}

// Env is what the session's environment carries for the shell prefix.
func (u SessionUnits) Env(toolCap ToolCap, prefix string) map[string]string {
	return map[string]string{
		"CLAUDE_CODE_SHELL_PREFIX":         prefix,
		"CLAUDEWHEEL_TOOL_SLICE":           u.ToolSlice(),
		"CLAUDEWHEEL_SESSION_SCOPE":        u.SessionScope(),
		"CLAUDEWHEEL_TOOL_MEMORY_MAX":      toolCap.MemoryMax,
		"CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX": toolCap.MemorySwapMax,
	}
}

// sweepAfter is how old a session slice must be before it is swept: a
// younger one may not have its session scope's process in it yet.
const sweepAfter = 60 * time.Second

// cgroupRoot is where the cgroup v2 hierarchy is mounted.
const cgroupRoot = "/sys/fs/cgroup"

// systemctlTimeout bounds each systemctl query; systemctlStopTimeout each
// stop.
const (
	systemctlTimeout     = 10 * time.Second
	systemctlStopTimeout = 30 * time.Second
)

// SweepEndedSessions stops the slices of sessions that ended and returns
// their names. systemd keeps an empty slice active until it is stopped, so
// each ended session would leave its session slice and tools slice behind.
// A session slice holds its Claude Code process for as long as the session
// lives and each Bash command for as long as it runs, so an empty one
// belongs to a session that ended with nothing left running; stopping it
// stops its tools slice too and ends no process. A slice launched less than
// sweepAfter before now is left alone. A systemctl query or stop that fails
// is an error.
func SweepEndedSessions(fx *effects.FX, now time.Time) ([]string, error) {
	listing, err := fx.Run(effects.Cmd{
		Argv:    []string{"systemctl", "--user", "list-units", "--type=slice", "--state=active", "--plain", "--no-legend", "claudewheel-*.slice"},
		Capture: true,
		Check:   true,
		Read:    true,
		Timeout: systemctlTimeout,
	})
	if err != nil {
		return nil, err
	}
	var candidates []string
	for _, line := range strings.Split(listing.Stdout(), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		m := sessionSliceRE.FindStringSubmatch(fields[0])
		if m == nil {
			continue
		}
		launched, err := strconv.ParseInt(m[2], 10, 64)
		if err != nil {
			continue
		}
		if launched <= now.Add(-sweepAfter).Unix() {
			candidates = append(candidates, fields[0])
		}
	}
	var stopped []string
	for _, name := range candidates {
		shown, err := fx.Run(effects.Cmd{
			Argv:    []string{"systemctl", "--user", "show", "-P", "ControlGroup", name},
			Capture: true,
			Check:   true,
			Read:    true,
			Timeout: systemctlTimeout,
		})
		if err != nil {
			return stopped, err
		}
		cgroup := strings.TrimSpace(shown.Stdout())
		if !strings.HasPrefix(cgroup, "/") {
			// A slice stopped between the listing and here has no group.
			continue
		}
		events, err := os.ReadFile(filepath.Join(cgroupRoot, strings.TrimLeft(cgroup, "/"), "cgroup.events"))
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				continue
			}
			return stopped, err
		}
		if !slices.Contains(strings.Split(string(events), "\n"), "populated 0") {
			continue
		}
		if _, err := fx.Run(effects.Cmd{
			Argv:    []string{"systemctl", "--user", "stop", name},
			Capture: true,
			Check:   true,
			Timeout: systemctlStopTimeout,
		}); err != nil {
			return stopped, err
		}
		stopped = append(stopped, name)
	}
	return stopped, nil
}

// setEnv returns env with key set to value, replacing every entry of key.
func setEnv(env []string, key, value string) []string {
	return append(unsetEnv(env, key), key+"="+value)
}

// unsetEnv returns env without key.
func unsetEnv(env []string, key string) []string {
	out := make([]string, 0, len(env))
	for _, kv := range env {
		if k, _, _ := strings.Cut(kv, "="); k != key {
			out = append(out, kv)
		}
	}
	return out
}

// Exec replaces this process with argv, started in the session's units with
// cwd as its working directory and env (os.Environ form) as its environment.
// On success it does not return; under --dry-run the exec is recorded and it
// returns nil.
//
// The client runs every shell command through the claudewheel-tool-scope
// script in scriptsDir (CLAUDE_CODE_SHELL_PREFIX); Exec adds it and the
// names it reads to env, and deploys it when it is missing. Every hook and
// Bash call of the session would fail without it, so one that cannot be run
// fails the launch, as does a systemd-run missing from env's PATH, the one
// the exec searches. The empty slices of ended sessions are stopped first.
// The layout and the cap are printed to stderr, which print mode keeps apart
// from its answer on stdout. secrets are redacted from the dry-run record
// and from every error.
func Exec(fx *effects.FX, cwd string, argv, env []string, toolCap ToolCap, scriptsDir string, stderr io.Writer, secrets []string) error {
	search, ok := effects.EnvValue(env, "PATH")
	if !ok {
		search = effects.DefaultExecPath
	}
	systemdRun, found, err := lookPath("systemd-run", search)
	if err != nil {
		return err
	}
	if !found {
		return fmt.Errorf("systemd-run is not on PATH; claudewheel starts every session in systemd user units, its Bash commands capped together at tool_memory_max from config.json")
	}
	prefix := filepath.Join(scriptsDir, hookscripts.ToolScopeScript)
	if strings.IndexFunc(prefix, unicode.IsSpace) >= 0 {
		return fmt.Errorf("the shell prefix %s has whitespace in its path, which Claude Code would split; claudewheel's scripts directory needs a path without it", prefix)
	}
	missing, err := hookscripts.MissingScripts([]string{hookscripts.ToolScopeScript}, scriptsDir)
	if err != nil {
		return err
	}
	if len(missing) > 0 {
		if _, err := hookscripts.DeployScripts(fx, missing, scriptsDir, false); err != nil {
			return err
		}
	}
	if !fx.Previewing() && unix.Access(prefix, unix.X_OK) != nil {
		return fmt.Errorf("%s is not executable; it is the shell prefix every command of the session runs through: redeploy it with `claudewheel deploy-hooks %s --force-overwrite`", prefix, hookscripts.ToolScopeScript)
	}
	now := time.Now()
	if _, err := SweepEndedSessions(fx, now); err != nil {
		return err
	}
	units := SessionUnits{PID: os.Getpid(), Launched: now.Unix()}
	fmt.Fprintf(stderr, "claudewheel: this session runs in %s, not memory-capped; its Bash commands run in %s, capped together at %s of memory and %s of swap (tool_memory_max and tool_memory_swap_max in config.json)\n",
		units.SessionScope(), units.ToolSlice(), toolCap.MemoryMax, toolCap.MemorySwapMax)
	unitEnv := units.Env(toolCap, prefix)
	for _, key := range slices.Sorted(maps.Keys(unitEnv)) {
		env = setEnv(env, key, unitEnv[key])
	}
	return fx.Exec(effects.Cmd{
		Argv:   units.Argv(systemdRun, argv),
		Dir:    cwd,
		Env:    env,
		Grant:  ExecClientGrant,
		Redact: secrets,
	})
}

// ExecClientGrant is the grant the launch command declares for replacing
// itself with the client.
const ExecClientGrant = "exec-client"
