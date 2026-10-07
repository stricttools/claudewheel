package proberunner

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// LostAfter is how long after its hand-off a handed report may sit while its
// session's transcript moves on without it before it counts as lost on the
// way and is queued again.
const LostAfter = 60 * time.Second

// Ended is one probe EndProbes ended, and why (one of probe's Ended
// constants).
type Ended struct {
	Probe  string
	Reason string
}

// EndProbes ends every live probe whose deadline, count, file, or watched
// session says so.
func EndProbes(fx *effects.FX, ws workspace.Workspace, nowMS int64) ([]Ended, error) {
	store := probe.NewStore(ws.Shared().ProbesDir())
	all, err := probe.LoadProbes(store)
	if err != nil {
		return nil, err
	}
	var live []*probe.ProbeState
	needLifecycles, needKills := false, false
	for _, state := range all {
		if state.Active() {
			live = append(live, state)
			needLifecycles = needLifecycles || state.UntilWatchedEnds
			needKills = needKills || state.UntilCount != nil
		}
	}
	var ended []Ended
	if len(live) == 0 {
		return ended, nil
	}
	// Read only what a live probe's stops need: this runs every tick.
	lifecycles := map[string]*lifecycle.SessionLifecycle{}
	if needLifecycles {
		lifecycles, err = lifecycle.LoadAll(ws.Shared().LifecycleDir())
		if err != nil {
			return nil, err
		}
	}
	var kills []probe.Kill
	if needKills {
		kills, err = probe.ReadKills(store)
		if err != nil {
			return nil, err
		}
	}
	for _, state := range live {
		reason, err := stopReason(state, nowMS, kills, lifecycles)
		if err != nil {
			return nil, err
		}
		if reason == "" {
			continue
		}
		if err := probe.AppendEnded(fx, store, state.ID, reason); err != nil {
			return nil, err
		}
		ended = append(ended, Ended{Probe: state.ID, Reason: reason})
	}
	return ended, nil
}

// stopReason is why state ends now, or "" while it keeps running.
func stopReason(state *probe.ProbeState, nowMS int64, kills []probe.Kill, lifecycles map[string]*lifecycle.SessionLifecycle) (string, error) {
	deadline, err := lifecycle.ParseTimestampMS(state.Deadline)
	if err != nil {
		return "", err
	}
	if deadline <= nowMS {
		return probe.EndedDeadline, nil
	}
	if state.UntilCount != nil {
		var seen int64
		for _, kill := range kills {
			for _, id := range kill.Probes {
				if id == state.ID {
					seen++
					break
				}
			}
		}
		if seen >= *state.UntilCount {
			return probe.EndedCount, nil
		}
	}
	if state.UntilFile != nil {
		found, err := pathstat.Exists(*state.UntilFile)
		if err != nil {
			return "", err
		}
		if found {
			return probe.EndedFile, nil
		}
	}
	if state.UntilWatchedEnds && state.WatchSession != nil {
		if life := lifecycles[*state.WatchSession]; life != nil && life.Ended != nil {
			return probe.EndedWatchedEnded, nil
		}
	}
	return "", nil
}

// sessionState returns whether session's client runs now, and its
// transcript path ("" when none is recorded).
func sessionState(lifecycles map[string]*lifecycle.SessionLifecycle, session string) (bool, string, error) {
	life := lifecycles[session]
	if life == nil || life.Started == nil {
		return false, "", nil
	}
	transcript, _ := life.Transcript()
	pid := life.Started.PID
	if life.Ended != nil || pid == nil {
		return false, transcript, nil
	}
	running, err := pathstat.Exists("/proc/" + strconv.FormatInt(*pid, 10))
	if err != nil {
		return false, "", err
	}
	return running, transcript, nil
}

// withoutSuffix drops the last suffix of path's final element, as pathlib's
// with_suffix("") does: a leading dot alone, or a trailing one, is no
// suffix.
func withoutSuffix(path string) string {
	base := filepath.Base(path)
	i := strings.LastIndex(base, ".")
	if i <= 0 || i == len(base)-1 {
		return path
	}
	return path[:len(path)-(len(base)-i)]
}

// transcripts returns the session's transcript and its subagents'
// transcripts.
func transcripts(transcript string) ([]string, error) {
	if transcript == "" {
		return nil, nil
	}
	out := []string{transcript}
	subagents := filepath.Join(withoutSuffix(transcript), "subagents")
	isDir, err := pathstat.IsDir(subagents)
	if err != nil {
		return nil, err
	}
	if !isDir {
		return out, nil
	}
	entries, err := os.ReadDir(subagents)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".jsonl") {
			out = append(out, filepath.Join(subagents, entry.Name()))
		}
	}
	return out, nil
}

// mentions reports whether any of paths holds needle; a missing file holds
// nothing.
func mentions(paths []string, needle []byte) (bool, error) {
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if pathstat.NotFoundOrParentNotDirectory(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		if bytes.Contains(data, needle) {
			return true, nil
		}
	}
	return false, nil
}

// latestMtime returns the newest modification time among paths; missing
// files are skipped, and none at all is the zero time.
func latestMtime(paths []string) (time.Time, error) {
	var latest time.Time
	for _, path := range paths {
		info, err := os.Stat(path)
		if pathstat.NotFoundOrParentNotDirectory(err) {
			continue
		}
		if err != nil {
			return time.Time{}, err
		}
		if info.ModTime().After(latest) {
			latest = info.ModTime()
		}
	}
	return latest, nil
}

// Settled counts what SettleReports did.
type Settled struct {
	Delivered int
	Requeued  int
	Expired   int
}

// SettleReports confirms and requeues the handed reports and expires the
// pending ones among the reports in states (probe's Report state constants).
//
// A handed report whose id is in its session's transcript is delivered. One
// whose session no longer runs, or whose transcript moved on LostAfter past
// the hand-off without it, is queued again and its waiter woken (a waiter
// path that is not a FIFO fails only that wake, written to log). A pending
// report a probe produced expires once that probe ended and its session no
// longer runs; a report no probe produced never expires.
func SettleReports(fx *effects.FX, ws workspace.Workspace, states []string, log io.Writer) (Settled, error) {
	var counts Settled
	store := probe.NewStore(ws.Shared().ProbesDir())
	reports, err := probe.ListReports(store, states)
	if err != nil {
		return counts, err
	}
	if len(reports) == 0 {
		return counts, nil
	}
	lifecycles, err := lifecycle.LoadAll(ws.Shared().LifecycleDir())
	if err != nil {
		return counts, err
	}
	probeList, err := probe.LoadProbes(store)
	if err != nil {
		return counts, err
	}
	probes := make(map[string]*probe.ProbeState, len(probeList))
	for _, state := range probeList {
		probes[state.ID] = state
	}
	for _, item := range reports {
		report := item.Report
		live, transcript, err := sessionState(lifecycles, report.Session)
		if err != nil {
			return counts, err
		}
		paths, err := transcripts(transcript)
		if err != nil {
			return counts, err
		}
		if item.State == probe.ReportHanded {
			found, err := mentions(paths, []byte("claudewheel probe report "+report.ID))
			if err != nil {
				return counts, err
			}
			if found {
				if _, err := probe.MoveReport(fx, store, item, probe.ReportDelivered); err != nil {
					return counts, err
				}
				counts.Delivered++
				continue
			}
			info, err := os.Stat(item.Path)
			if err != nil {
				return counts, err
			}
			latest, err := latestMtime(paths)
			if err != nil {
				return counts, err
			}
			movedOn := latest.After(info.ModTime().Add(LostAfter))
			if !live || movedOn {
				if _, err := probe.MoveReport(fx, store, item, probe.ReportPending); err != nil {
					return counts, err
				}
				if err := wake(fx, store, report.Session, log); err != nil {
					return counts, err
				}
				counts.Requeued++
			}
			continue
		}
		if item.State != probe.ReportPending || report.Probe == nil || live {
			continue
		}
		if state := probes[*report.Probe]; state != nil && !state.Active() {
			if _, err := probe.MoveReport(fx, store, item, probe.ReportExpired); err != nil {
				return counts, err
			}
			counts.Expired++
		}
	}
	return counts, nil
}

// Tick is one pass of keeping the store moving: it ends the probes that
// should end and settles the handed reports; expire also checks the pending
// reports for expiry. Wakes that fail for a waiter path that is not a FIFO
// are written to log.
func Tick(fx *effects.FX, ws workspace.Workspace, expire bool, log io.Writer) error {
	if _, err := EndProbes(fx, ws, lifecycle.NowMS()); err != nil {
		return err
	}
	states := []string{probe.ReportHanded}
	if expire {
		states = []string{probe.ReportPending, probe.ReportHanded}
	}
	_, err := SettleReports(fx, ws, states, log)
	return err
}
