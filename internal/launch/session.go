package launch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/sessionmove"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// SessionMode is which session a launch starts in.
type SessionMode int

const (
	// SessionNew starts a new session.
	SessionNew SessionMode = iota + 1
	// SessionContinue continues the most recent conversation in the
	// directory (Claude Code's --continue).
	SessionContinue
	// SessionResume resumes the session whose id is Session.Value.
	SessionResume
	// SessionPicker opens Claude Code's own session picker (a bare
	// --resume).
	SessionPicker
	// SessionPrint runs the prompt in Session.Value in print mode and
	// exits.
	SessionPrint
)

// Session is the session choice of one launch.
type Session struct {
	Mode SessionMode
	// Value is the session id or title for SessionResume (a title is
	// resolved to an id before the launch builds its argv), and the prompt
	// for SessionPrint.
	Value string
}

// uuidShapeRE matches a session id in any letter case: a value shaped like
// one but not lowercase is not an id, and is not searched as a title either.
var uuidShapeRE = regexp.MustCompile(`(?i)` + lifecycle.SessionUUIDPattern)

// resolveResume returns the session id a --resume value names: a session id
// is returned as it is; any other value is a session title, looked up first
// in the store directory of directory, then in every store directory. No
// match, and more than one, are errors.
func resolveResume(ws workspace.Workspace, value, directory string) (string, error) {
	if lifecycle.SessionUUIDRE.MatchString(value) {
		return value, nil
	}
	if uuidShapeRE.MatchString(value) {
		return "", fmt.Errorf("--resume %s is not a session id: session ids are lowercase, and a value shaped like one is not searched as a session title either", value)
	}
	projectsDir := ws.Shared().ProjectsDir()
	abs, err := filepath.Abs(directory)
	if err != nil {
		return "", err
	}
	matches, err := sessions.FindSessionsByTitle(value, []string{filepath.Join(projectsDir, workspace.EncodePath(abs))})
	if err != nil {
		return "", err
	}
	if len(matches) == 0 {
		all, err := storeDirs(projectsDir)
		if err != nil {
			return "", err
		}
		if matches, err = sessions.FindSessionsByTitle(value, all); err != nil {
			return "", err
		}
	}
	switch len(matches) {
	case 1:
		return matches[0].SessionID, nil
	case 0:
		return "", fmt.Errorf("no session titled %q was found in any project directory; --picker browses the sessions", value)
	}
	slices.SortStableFunc(matches, func(a, b sessions.TitleMatch) int { return b.Mtime.Compare(a.Mtime) })
	lines := []string{fmt.Sprintf("more than one session is titled %q:", value)}
	for _, m := range matches {
		lines = append(lines, fmt.Sprintf("  %s  %s  %s", m.SessionID, filepath.Base(m.ProjectDir), m.Mtime.Format("2006-01-02 15:04")))
	}
	lines = append(lines, "Resume by session id (the first column above) to choose one.")
	return "", errors.New(strings.Join(lines, "\n"))
}

// storeDirs lists the store directories under projectsDir, sorted; none
// when it does not exist.
func storeDirs(projectsDir string) ([]string, error) {
	entries, err := os.ReadDir(projectsDir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var dirs []string
	for _, e := range entries {
		path := filepath.Join(projectsDir, e.Name())
		info, err := os.Stat(path)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				continue
			}
			return nil, err
		}
		if info.IsDir() {
			dirs = append(dirs, path)
		}
	}
	return dirs, nil
}

// transcripts lists the top-level transcripts of a store directory and
// their total size; none when the directory does not exist.
func transcripts(storeDir string) (count int, size int64, err error) {
	entries, err := os.ReadDir(storeDir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return 0, 0, nil
		}
		return 0, 0, err
	}
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		info, err := os.Stat(filepath.Join(storeDir, e.Name()))
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				continue
			}
			return 0, 0, err
		}
		if info.Mode().IsRegular() {
			count++
			size += info.Size()
		}
	}
	return count, size, nil
}

// megabytes renders a size as the session-move prompts state it.
func megabytes(size int64) string {
	return fmt.Sprintf("%.1f MB", float64(size)/(1024*1024))
}

// sessionMover moves the sessions of a renamed project directory to the
// directory a launch runs in, after asking.
type sessionMover struct {
	ctx      context.Context
	fx       *effects.FX
	ws       workspace.Workspace
	profiles []sessions.ProfileConfigDir
	screens  *screens
	stderr   io.Writer
	// interactive is false when there is no terminal to ask at.
	interactive bool
}

// move asks to move every session recorded under oldCwd to newCwd, states
// what the move would change and asks again, then moves them. It reports
// whether the sessions moved; declined says what a decline means here.
func (m *sessionMover) move(oldCwd, newCwd string, lines []string, declined string) (bool, error) {
	answer, err := m.screens.confirm(m.ctx, widgets.Confirmation{
		Title:   "Move sessions to " + newCwd + "?",
		Lines:   lines,
		Accept:  "move them",
		Decline: declined,
		Skip:    declined,
	})
	if err != nil || answer != widgets.Accept {
		return false, err
	}
	counted, err := sessionmove.Mv(m.fx, m.ws, m.profiles, oldCwd, newCwd, sessionmove.MvOptions{PostHoc: true, Quiet: true, CountOnly: true})
	if err != nil {
		return false, err
	}
	answer, err = m.screens.confirm(m.ctx, widgets.Confirmation{
		Title: "Proceed with the move?",
		Lines: []string{
			fmt.Sprintf("Will move %d session files, rewrite %d path references,", counted.FilesRewritten, counted.LinesReplaced),
			fmt.Sprintf("and update %d profile keys.", counted.ProjectKeysUpdated),
		},
		Accept:  "move them",
		Decline: declined,
		Skip:    declined,
	})
	if err != nil || answer != widgets.Accept {
		return false, err
	}
	if _, err := sessionmove.Mv(m.fx, m.ws, m.profiles, oldCwd, newCwd, sessionmove.MvOptions{PostHoc: true, Quiet: true}); err != nil {
		return false, err
	}
	fmt.Fprintln(m.stderr, "Moved the sessions. Resuming the session...")
	return true, nil
}

// checkResume makes sure Claude Code finds the session it resumes from
// directory. When the session is recorded under a project directory that no
// longer exists (the directory was renamed), it offers to move that
// directory's sessions here; a session belonging to a directory that still
// exists, one found nowhere, and a declined move are errors.
func (m *sessionMover) checkResume(sessionID, directory string) error {
	here, err := filepath.Abs(directory)
	if err != nil {
		return err
	}
	projectsDir := m.ws.Shared().ProjectsDir()
	expected := filepath.Join(projectsDir, workspace.EncodePath(here), sessionID+".jsonl")
	if _, err := os.Stat(expected); err == nil {
		return nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	info, err := sessions.FindSession(sessionID, projectsDir)
	if err != nil {
		return err
	}
	if info == nil {
		return fmt.Errorf("session %s was not found in any project directory; --picker browses the sessions", sessionID)
	}
	if st, err := os.Stat(info.Cwd); err == nil && st.IsDir() {
		return fmt.Errorf("session %s belongs to %s, which still exists: launch from that directory instead, or move the session here with `claudewheel move-session %s %s`", sessionID, info.Cwd, sessionID, shellQuote(here))
	}
	if !m.interactive {
		return fmt.Errorf("session %s was created in %s, which no longer exists; move its sessions here with `claudewheel mv %s %s --post-hoc`, then launch again", sessionID, info.Cwd, shellQuote(info.Cwd), shellQuote(here))
	}
	count, size, err := transcripts(filepath.Join(projectsDir, info.EncodedCwd))
	if err != nil {
		return err
	}
	moved, err := m.move(info.Cwd, here, []string{
		fmt.Sprintf("Session %s was created in", sessionID),
		info.Cwd + ",",
		"which no longer exists. You are now in",
		here + ".",
		"",
		fmt.Sprintf("Found %d sessions (%s) under the old path.", count, megabytes(size)),
	}, "abort the launch")
	if err != nil {
		return err
	}
	if !moved {
		return errors.New("launch aborted: the sessions remain under the old path")
	}
	return nil
}

// checkContinue offers, when directory has no sessions while the store
// holds sessions of project directories that no longer exist, to move one
// of those directories' sessions here. Declining, or having no terminal to
// ask at, continues the launch without moving anything.
func (m *sessionMover) checkContinue(directory string) error {
	here, err := filepath.Abs(directory)
	if err != nil {
		return err
	}
	projectsDir := m.ws.Shared().ProjectsDir()
	count, _, err := transcripts(filepath.Join(projectsDir, workspace.EncodePath(here)))
	if err != nil || count > 0 {
		return err
	}
	orphans, err := sessions.FindOrphanedProjectDirs(projectsDir)
	if err != nil || len(orphans) == 0 || !m.interactive {
		return err
	}
	chosen := orphans[0]
	if len(orphans) > 1 {
		options := make([]widgets.Option, len(orphans))
		for i, o := range orphans {
			options[i] = widgets.Option{Key: o.EncodedCwd, Label: fmt.Sprintf("%s (%d sessions, %s)", o.Cwd, o.SessionCount, megabytes(o.TotalSizeBytes))}
		}
		key, ok, err := m.screens.selection(m.ctx, "No sessions under "+here+": move sessions from which directory? (esc: none)", options)
		if err != nil || !ok {
			return err
		}
		for _, o := range orphans {
			if o.EncodedCwd == key {
				chosen = o
			}
		}
	}
	_, err = m.move(chosen.Cwd, here, []string{
		"No sessions were found under " + here + ".",
		fmt.Sprintf("Found %d sessions (%s) under", chosen.SessionCount, megabytes(chosen.TotalSizeBytes)),
		chosen.Cwd + ", which no longer exists.",
	}, "launch without moving them")
	return err
}

// shellQuote quotes s for a POSIX shell when it needs quoting, as Python's
// shlex.quote does.
func shellQuote(s string) string {
	if s == "" {
		return "''"
	}
	safe := true
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune("@%+=:,./-_", r)) {
			safe = false
			break
		}
	}
	if safe {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

// interactiveTerminal reports whether a launch may prompt: never in print
// mode, whose stdout is the answer, and otherwise when a terminal is there
// to prompt at.
func interactiveTerminal(session Session) bool {
	return session.Mode != SessionPrint && terminal.HasControllingTerminal()
}
