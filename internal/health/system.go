package health

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/hookscripts"
	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/scratchpad"
)

// tmpfsQuota checks the use of /tmp through df.
func (c *checker) tmpfsQuota() Result {
	const label = "tmpfs"
	res, err := c.in.FX.Run(effects.Cmd{
		Argv:    []string{"df", "--output=pcent", "/tmp"},
		Capture: true,
		Timeout: 3 * time.Second,
		Read:    true,
	})
	if err != nil {
		return failed(label, err)
	}
	if res.ExitCode() != 0 {
		return failed(label, fmt.Errorf("df exited with status %d: %s", res.ExitCode(), strings.TrimSpace(res.Stderr())))
	}
	lines := strings.Split(strings.TrimSpace(res.Stdout()), "\n")
	if len(lines) < 2 {
		return failed(label, fmt.Errorf("df printed no usage line: %q", res.Stdout()))
	}
	last := strings.TrimRight(strings.TrimSpace(lines[len(lines)-1]), "%")
	pct, err := strconv.Atoi(strings.TrimSpace(last))
	if err != nil {
		return failed(label, fmt.Errorf("df printed %q, not a percentage", lines[len(lines)-1]))
	}
	if pct > 80 {
		return warn(label, fmt.Sprintf("%d%% used (>80%% threshold)", pct))
	}
	return ok(label, fmt.Sprintf("%d%% used", pct))
}

// tmpClaudeSize checks the allocated size of the per-user Claude Code
// scratchpad tree; link targets are not counted.
func (c *checker) tmpClaudeSize() Result {
	const label = "/tmp/claude"
	root := scratchpad.TmpClaudeDir()
	exists, _, err := pathState(root)
	if err != nil {
		return failed(label, err)
	}
	if !exists {
		return ok(label, "not present")
	}
	size, _ := scratchpad.ScanTree(root)
	mb := float64(size) / (1024 * 1024)
	if mb > 1024 {
		return warn(label, fmt.Sprintf("%.0f MB (>1 GB threshold)", mb))
	}
	return ok(label, fmt.Sprintf("%.0f MB", mb))
}

// probeRunner checks that the probe runner's unit is the one deploy-hooks
// writes for this binary, and that systemd reports it enabled and active.
// Each failure names the fix: deploying the service again.
func (c *checker) probeRunner() Result {
	const label = "probe-runner"
	fix := fmt.Sprintf("run 'claudewheel deploy-hooks %s --force-overwrite'", probe.ServiceName)
	unit := filepath.Join(c.in.Workspace.SystemdUserDir(), probe.ServiceName)
	exists, _, err := pathState(unit)
	if err != nil {
		return failed(label, err)
	}
	if !exists {
		return warn(label, fmt.Sprintf("%s is not installed -- %s", unit, fix))
	}
	want, err := hookscripts.ServiceUnit(c.in.Executable)
	if err != nil {
		return failed(label, err)
	}
	have, err := os.ReadFile(unit)
	if err != nil {
		return failed(label, err)
	}
	if string(have) != want {
		return warn(label, fmt.Sprintf("%s differs from the unit claudewheel deploys for %s -- %s", unit, c.in.Executable, fix))
	}
	var states []string
	for _, verb := range []string{"is-enabled", "is-active"} {
		res, err := c.in.FX.Run(effects.Cmd{
			Argv:    []string{"systemctl", "--user", verb, probe.ServiceName},
			Capture: true,
			Timeout: 10 * time.Second,
			Read:    true,
		})
		if err != nil {
			return warn(label, fmt.Sprintf("systemctl %s failed: %v", verb, err))
		}
		answer := strings.TrimSpace(res.Stdout())
		if answer == "" {
			answer = "unknown"
		}
		states = append(states, answer)
	}
	enabled, active := states[0], states[1]
	if enabled != "enabled" {
		return warn(label, fmt.Sprintf("%s is %s, not enabled -- %s", probe.ServiceName, enabled, fix))
	}
	if active != "active" {
		return warn(label, fmt.Sprintf("%s is %s, not running -- %s", probe.ServiceName, active, fix))
	}
	return ok(label, probe.ServiceName+" installed, enabled, and running")
}

// inodeRenames finds directory renames in the project inode map
// (appconfig.AnalyzeInodes). Stale entries, for directories deleted rather
// than renamed, are counted and left in place: health changes nothing, and
// the launch preflight prunes them.
func (c *checker) inodeRenames() Result {
	const label = "inode-renames"
	data, found, err := readObject(c.in.Workspace.InodesFile())
	if err != nil {
		return warn(label, "unreadable inodes.json")
	}
	if !found {
		return ok(label, "no inode data yet")
	}
	analysis, err := appconfig.AnalyzeInodes(data)
	if err != nil {
		return failed(label, err)
	}
	var renames []string
	for _, r := range analysis.Renames {
		renames = append(renames, fmt.Sprintf("%s -> %s. Run: claudewheel mv --post-hoc %s %s", r.Old, r.New, r.Old, r.New))
	}
	stale := len(analysis.Stale)
	if len(renames) > 0 {
		return warn(label, strings.Join(renames, "; "))
	}
	if stale > 0 {
		return ok(label, fmt.Sprintf("no renames detected; %d entries name directories that no longer exist, which the next launch removes", stale))
	}
	return ok(label, "no renames detected")
}
