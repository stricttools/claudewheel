// Package proberunner is the probe runner: the one long-running process that
// hosts every probe (claudewheel-probe-runner.service, started as
// `claudewheel probe run-service`).
//
// It follows the user journal for systemd's "The kernel OOM killer killed
// some processes in this unit" entries and, for each one:
//
//   - attributes the unit to a Claude Code session (a claudewheel session
//     scope through the lifecycle store, a heavy scope through the session
//     scope heavy named in the scope's description, which the journal keeps
//     after the scope is gone) or records why it cannot;
//   - queues a report to the session whose command it was, with no probe and
//     no deadline, and one to every subscription of every live probe that
//     watches that session or all sessions;
//   - records the kill in kills.jsonl with the reports it produced (none: an
//     unrouted kill, kept and listed rather than dropped), then saves the
//     journal cursor, so a restarted runner resumes after the last entry with
//     no repeat and no gap.
//
// Between entries it keeps the store moving: it ends probes whose deadline
// passed, whose kill count was reached, whose file appeared, or whose watched
// session ended; it confirms each report handed to a session by finding its
// id in the session's transcript, and puts one back in the queue when the
// session went away without it or moved on without it; and it expires the
// undelivered reports of an ended probe once their session has ended too.
package proberunner

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// TickInterval is how long the journal may stay quiet before the store is
// kept moving anyway.
const TickInterval = 2 * time.Second

// ExpiryEvery is how often pending reports are checked for expiry. A handed
// report is settled every tick, since a session is waiting on it; a pending
// one can only expire, and one kept for an ended session may wait for months.
const ExpiryEvery = 60 * time.Second

// stopGrace is how long journalctl gets to exit after SIGTERM before it is
// killed.
const stopGrace = 10 * time.Second

// ErrJournalEnded is returned by Run when journalctl stopped on its own.
var ErrJournalEnded = errors.New(probe.ServiceName + ": journalctl ended")

// JournalArgv is journalctl following the OOM-kill entries, after cursor or,
// for an empty cursor, from now on.
func JournalArgv(cursor string) []string {
	argv := []string{
		"journalctl", "--user", "--follow", "-o", "json", "--no-pager",
		"MESSAGE_ID=" + OOMKillMessageID,
	}
	if cursor != "" {
		return append(argv, "--after-cursor="+cursor)
	}
	return append(argv, "--lines", "0")
}

// readCursor returns the saved journal cursor, "" when none was saved.
func readCursor(store probe.Store) (string, error) {
	data, err := os.ReadFile(store.CursorFile())
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// followed is one read from journalctl: a line, or the error that ended the
// reading.
type followed struct {
	line string
	err  error
}

// Run follows the journal and keeps the probe store moving until ctx is
// cancelled, then stops journalctl and returns nil. Progress lines go to
// log. It returns ErrJournalEnded when journalctl stops on its own, and any
// error reading or writing the store.
func Run(ctx context.Context, fx *effects.FX, ws workspace.Workspace, log io.Writer) (err error) {
	store := probe.NewStore(ws.Shared().ProbesDir())
	if err := fx.MkdirAll(store.Root()); err != nil {
		return err
	}
	cursor, err := readCursor(store)
	if err != nil {
		return err
	}
	follower, err := fx.Follow(JournalArgv(cursor))
	if err != nil {
		return err
	}
	defer func() {
		if stopErr := follower.Stop(stopGrace); stopErr != nil {
			err = errors.Join(err, fmt.Errorf("stopping journalctl: %w", stopErr))
		}
	}()
	if cursor != "" {
		logf(log, "following OOM kills after cursor %s", cursor)
	} else {
		logf(log, "following OOM kills from now on")
	}

	lines := make(chan followed)
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			line, err := follower.ReadLine()
			select {
			case lines <- followed{line: line, err: err}:
			case <-done:
				return
			}
			if err != nil {
				return
			}
		}
	}()

	describe := HeavyDescriber(fx)
	var lastExpiry time.Time
	keepMoving := func() error {
		expire := lastExpiry.IsZero() || time.Since(lastExpiry) >= ExpiryEvery
		if err := Tick(fx, ws, expire); err != nil {
			return err
		}
		if expire {
			lastExpiry = time.Now()
		}
		return nil
	}

	if err := keepMoving(); err != nil {
		return err
	}
	for {
		var got followed
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(TickInterval):
			if err := keepMoving(); err != nil {
				return err
			}
			continue
		case got = <-lines:
		}
		if errors.Is(got.err, io.EOF) {
			return ErrJournalEnded
		}
		if got.err != nil {
			return fmt.Errorf("reading journalctl: %w", got.err)
		}
		entry, perr := ParseEntry([]byte(got.line))
		if perr != nil {
			shown := got.line
			if len(shown) > 200 {
				shown = shown[:200]
			}
			logf(log, "unreadable journal line: %s", strconv.Quote(shown))
			continue
		}
		kill, err := ProcessEntry(fx, ws, entry, describe)
		if err != nil {
			return err
		}
		if kill != nil {
			session := "None"
			if kill.Session != nil {
				session = *kill.Session
			}
			logf(log, "%s: session %s, %d report(s)", kill.Unit, session, len(kill.Reports))
		}
		if next, ok := entry.String("__CURSOR"); ok {
			if err := fx.WriteFileAtomic(store.CursorFile(), []byte(next+"\n")); err != nil {
				return err
			}
		}
		if err := keepMoving(); err != nil {
			return err
		}
	}
}

// logf writes one progress line naming the service, as systemd's journal
// shows it.
func logf(log io.Writer, format string, args ...any) {
	fmt.Fprintf(log, probe.ServiceName+": "+format+"\n", args...)
}
