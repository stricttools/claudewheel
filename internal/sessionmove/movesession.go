package sessionmove

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/stricttools/strictspec/go/strictspec"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/schema/sessionmovejournal"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// A session lives in the store dir named after the directory it belongs to:
// <projects>/<encoded directory>/<session>.jsonl, with an optional
// <session>/ folder beside it (tool results, subagent transcripts). Claude
// Code's own /cd moves a session by renaming both into the new directory's
// store dir and appending one relocated record to the transcript; it reads a
// session's directory as that record's relocatedCwd, else as the first cwd
// the transcript recorded. MoveSession makes the same move for a session
// that is not running, and also rewrites the paths in the top-level
// transcript that point into the session's own store folder (no other path,
// and no cwd field), keeps the transcript's access and modification times,
// records a moved event in the session's lifecycle file, and removes the
// source store dir when the move leaves it empty.
//
// Every check runs, and every file is read, before the first change, so a
// --dry-run preview records the changes a run would make. Before the first
// change the move writes a journal, shared/session-moves/<session>.json
// (its shape is owned by .strictspec/session-move-journal.schema.toml),
// records each step in it as the step completes, and removes it at the end.
// A rerun with the same session and directory reads the journal and does
// the steps not yet done; each step also recognizes on disk that it already
// happened, so a run stopped between a step and its journal update still
// completes. While a journal exists, a move of the session to any other
// directory is refused.

// JournalFormatVersion is the format_version every journal carries.
const JournalFormatVersion = 1

// The steps of a move, in the order they are done.
const (
	StepTranscriptMoved     = "transcript-moved"
	StepFolderMoved         = "folder-moved"
	StepTranscriptRewritten = "transcript-rewritten"
	StepTimesRestored       = "times-restored"
	StepLifecycleRecorded   = "lifecycle-recorded"
	StepSourceRemoved       = "source-removed"
)

// Steps returns the steps of a move in the order they are done.
func Steps() []string {
	return []string{
		StepTranscriptMoved,
		StepFolderMoved,
		StepTranscriptRewritten,
		StepTimesRestored,
		StepLifecycleRecorded,
		StepSourceRemoved,
	}
}

// MoveSessionError is a refusal: the run that returned it changed nothing.
type MoveSessionError struct {
	Msg string
}

func (e *MoveSessionError) Error() string { return e.Msg }

func refuse(format string, args ...any) error {
	return &MoveSessionError{Msg: fmt.Sprintf(format, args...)}
}

// Journal is one unfinished move, as recorded in its journal file. Every
// field is written; OldCwd is nil when the transcript recorded no directory.
type Journal struct {
	FormatVersion     int      `json:"format_version"`
	Session           string   `json:"session"`
	SourceStoreDir    string   `json:"source_store_dir"`
	TargetStoreDir    string   `json:"target_store_dir"`
	TargetCwd         string   `json:"target_cwd"`
	OldCwd            *string  `json:"old_cwd"`
	TranscriptAtimeNS int64    `json:"transcript_atime_ns"`
	TranscriptMtimeNS int64    `json:"transcript_mtime_ns"`
	EventID           string   `json:"event_id"`
	EventAt           string   `json:"event_at"`
	StepsDone         []string `json:"steps_done"`
}

// MoveResult is what one run did, or under --dry-run would do.
type MoveResult struct {
	SourceStoreDir string
	TargetStoreDir string
	TargetCwd      string
	// Resumed: the run finished a move a journal recorded.
	Resumed bool
	// Steps are the steps this run did, in order.
	Steps []string
}

// JournalPath is the journal file of a move of session.
func JournalPath(ws workspace.Workspace, session string) string {
	return filepath.Join(ws.Shared().SessionMovesDir(), session+".json")
}

func joinDiagnostics(diags []strictspec.Diagnostic) string {
	messages := make([]string, len(diags))
	for i, d := range diags {
		messages[i] = d.Message
	}
	return strings.Join(messages, "; ")
}

// validateJournal runs the schema validation, the format_version check first.
func validateJournal(data []byte, where string) error {
	_, diags := sessionmovejournal.ValidateBytes(data, "json")
	if len(diags) > 0 {
		return refuse("%s: %s", where, joinDiagnostics(diags))
	}
	return nil
}

// ReadJournal reads the journal at path; it returns nil when there is none,
// and a damaged one is an error.
func ReadJournal(path string) (*Journal, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, refuse("cannot read the move journal %s: %v", path, err)
	}
	if err := validateJournal(data, path); err != nil {
		return nil, err
	}
	var j Journal
	if err := jsonfile.DecodeStrict(data, &j); err != nil {
		return nil, refuse("%s: %v", path, err)
	}
	return &j, nil
}

func writeJournal(fx *effects.FX, path string, j Journal) error {
	data, err := jsonfile.MarshalIndented(j)
	if err != nil {
		return err
	}
	if err := validateJournal(data, path); err != nil {
		return err
	}
	return fx.WriteFileAtomic(path, data)
}

// holder is a store dir holding something of the session.
type holder struct {
	storeDir   string
	transcript bool
	folder     bool
}

func (h holder) describe(session string) string {
	var held []string
	if h.transcript {
		held = append(held, session+".jsonl")
	}
	if h.folder {
		held = append(held, session+"/")
	}
	return h.storeDir + ": " + strings.Join(held, ", ")
}

// holders returns every store dir, in every store, holding the session's
// transcript or folder.
func holders(stores []string, session string) ([]holder, error) {
	var found []holder
	for _, projects := range stores {
		dirs, err := subdirs(projects)
		if err != nil {
			return nil, err
		}
		for _, storeDir := range dirs {
			transcript, err := lexists(filepath.Join(storeDir, session+".jsonl"))
			if err != nil {
				return nil, err
			}
			folder, err := lexists(filepath.Join(storeDir, session))
			if err != nil {
				return nil, err
			}
			if transcript || folder {
				found = append(found, holder{storeDir: storeDir, transcript: transcript, folder: folder})
			}
		}
	}
	return found, nil
}

// sessionDirectory is the session's directory as Claude Code reads it from
// its transcript: the relocatedCwd of the last relocated record, else the
// first cwd any record carries; nil when the transcript has neither.
func sessionDirectory(data []byte) *string {
	var firstCwd, relocated *string
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.Contains(line, `"cwd"`) && !strings.Contains(line, `"relocatedCwd"`) {
			continue
		}
		record, err := jsonfile.Decode([]byte(line))
		if err != nil {
			continue
		}
		o, ok := record.(*jsonfile.Object)
		if !ok {
			continue
		}
		if t, _ := o.Get("type"); t == sessions.RelocatedRecordType {
			if movedTo, _ := o.Get("relocatedCwd"); movedTo != nil {
				if s, ok := movedTo.(string); ok && s != "" {
					relocated = &s
				}
			}
		} else if firstCwd == nil {
			if cwd, _ := o.Get("cwd"); cwd != nil {
				if s, ok := cwd.(string); ok && s != "" {
					firstCwd = &s
				}
			}
		}
	}
	if relocated != nil {
		return relocated
	}
	return firstCwd
}

// relocatedLine is Claude Code's relocated record, as its /cd writes it.
func relocatedLine(session, targetCwd string) ([]byte, error) {
	o := jsonfile.NewObject()
	o.Set("type", sessions.RelocatedRecordType)
	o.Set("sessionId", session)
	o.Set("relocatedCwd", targetCwd)
	return jsonfile.MarshalTranscriptLine(o)
}

// movedTranscript is data with the paths into the session's store folder in
// oldName (any spelling of .../projects/<oldName>/<session>) moved to
// newName, every cwd field left alone, and the relocated record appended.
func movedTranscript(data []byte, session, oldName, newName, targetCwd string) ([]byte, error) {
	old := "/projects/" + oldName + "/" + session
	repl := "/projects/" + newName + "/" + session
	str := func(s, key string) string {
		// A cwd field records where the session ran; it is never rewritten.
		if key == "cwd" {
			return s
		}
		out, _ := replaceBounded(s, old, repl)
		return out
	}
	objectKey := func(k, _ string) string {
		out, _ := replaceBounded(k, old, repl)
		return out
	}
	moved, _, err := rewriteLines(data, "/"+oldName+"/"+session, func(v jsonfile.Value) jsonfile.Value {
		return mapTree(v, "", str, objectKey)
	})
	if err != nil {
		return nil, err
	}
	line, err := relocatedLine(session, targetCwd)
	if err != nil {
		return nil, err
	}
	out := append([]byte(nil), moved...)
	if len(out) > 0 && out[len(out)-1] != '\n' {
		out = append(out, '\n')
	}
	out = append(out, line...)
	return append(out, '\n'), nil
}

// endsWithRelocated reports whether the last non-empty line of data is the
// relocated record of this move.
func endsWithRelocated(data []byte, session, targetCwd string) (bool, error) {
	line, err := relocatedLine(session, targetCwd)
	if err != nil {
		return false, err
	}
	text := strings.TrimRight(string(data), "\n")
	last := text[strings.LastIndexByte(text, '\n')+1:]
	return last == string(line), nil
}

// checkNotRunning refuses a session whose process runs, or whose lifecycle
// says it is starting.
func checkNotRunning(lifecycleDir, session string, profiles []sessions.ProfileConfigDir, nowMS int64) error {
	found := sessions.ReadProfileRecords(profiles)
	present := false
	for _, f := range found {
		if f.Record.SessionID != session {
			continue
		}
		present = true
		if f.Record.Live {
			return refuse("session %s is running (pid %d, profile %s): a running session cannot be moved", session, f.Record.PID, f.Profile)
		}
	}
	path, err := lifecycle.SessionFile(lifecycleDir, session)
	if err != nil {
		return err
	}
	events, err := lifecycle.ReadSession(path)
	if err != nil {
		return err
	}
	life := lifecycle.Summarize(events, session)
	state, err := lifecycle.DeriveState(&life, lifecycle.Observed{RegistryPresent: present}, nowMS)
	if err != nil {
		return err
	}
	if state == lifecycle.StateStarting {
		return refuse("session %s is starting: its lifecycle records a start and its process has not registered yet, so it may be running", session)
	}
	return nil
}

// refersTo reports whether a background-job record names the session.
func refersTo(data *jsonfile.Object, session string) bool {
	for _, key := range []string{"sessionId", "resumeSessionId"} {
		if v, _ := data.Get(key); v == session {
			return true
		}
	}
	link, _ := data.Get("linkScanPath")
	s, ok := link.(string)
	if !ok {
		return false
	}
	for _, part := range strings.Split(s, "/") {
		if part == session || part == session+".jsonl" {
			return true
		}
	}
	return false
}

// checkNoJobs refuses when a Claude Code background-job record refers to
// the session.
func checkNoJobs(session string, profiles []sessions.ProfileConfigDir) error {
	seen := map[string]bool{}
	var naming []string
	for _, p := range profiles {
		jobs := filepath.Join(p.ConfigDir, "jobs")
		names, err := namesIfDir(jobs)
		if err != nil {
			return err
		}
		for _, name := range names {
			state := filepath.Join(jobs, name, "state.json")
			if there, err := lexists(state); err != nil {
				return err
			} else if !there {
				continue
			}
			real, err := realPath(state)
			if err != nil {
				return err
			}
			if seen[real] {
				continue
			}
			seen[real] = true
			raw, err := os.ReadFile(state)
			var record jsonfile.Value
			if err == nil {
				record, err = jsonfile.Decode(raw)
			}
			if err != nil {
				return refuse("cannot read the background job record %s, so whether it refers to session %s is unknown: %v", state, session, err)
			}
			o, ok := record.(*jsonfile.Object)
			if !ok || !refersTo(o, session) {
				continue
			}
			label := ""
			if name, _ := o.Get("name"); name != nil {
				if s, ok := name.(string); ok && s != "" {
					label = " (" + s + ")"
				}
			}
			naming = append(naming, fmt.Sprintf("  background job %s%s refers to session %s", state, label, session))
		}
	}
	if len(naming) > 0 {
		return refuse("session %s has Claude Code background jobs, which would lose it if it moved:\n%s", session, strings.Join(naming, "\n"))
	}
	return nil
}

// linksInto appends to links every symbolic link under dir whose resolved
// target is inside, or is, the directory inside: a directory's entries
// first, then its subdirectories, each in name order.
func linksInto(dir, inside string, links []string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var deeper []string
	for _, e := range entries {
		p := filepath.Join(dir, e.Name())
		if e.Type()&fs.ModeSymlink != 0 {
			target, err := os.Readlink(p)
			if err != nil {
				return nil, err
			}
			real, err := realPath(pyJoin(dir, target))
			if err != nil {
				return nil, err
			}
			if real == inside || strings.HasPrefix(real, inside+"/") {
				links = append(links, fmt.Sprintf("  %s -> %s", p, target))
			}
			continue
		}
		if e.IsDir() {
			deeper = append(deeper, p)
		}
	}
	for _, d := range deeper {
		if links, err = linksInto(d, inside, links); err != nil {
			return nil, err
		}
	}
	return links, nil
}

// checkNoInboundLinks refuses when a symbolic link in another session's
// folder points into folder.
func checkNoInboundLinks(stores []string, session, folder string) error {
	ok, err := isDir(folder)
	if err != nil || !ok {
		return err
	}
	inside, err := realPath(folder)
	if err != nil {
		return err
	}
	var links []string
	for _, projects := range stores {
		dirs, err := subdirs(projects)
		if err != nil {
			return err
		}
		for _, storeDir := range dirs {
			names, err := dirNames(storeDir)
			if err != nil {
				return err
			}
			for _, name := range names {
				if name == session || !lifecycle.SessionUUIDRE.MatchString(name) {
					continue
				}
				other := filepath.Join(storeDir, name)
				info, err := statPath(other, false)
				if err != nil {
					return err
				}
				if info == nil || !info.IsDir() {
					continue
				}
				if links, err = linksInto(other, inside, links); err != nil {
					return err
				}
			}
		}
	}
	if len(links) > 0 {
		return refuse("symlinks in other sessions' folders point into %s, and moving it would break them:\n%s", folder, strings.Join(links, "\n"))
	}
	return nil
}

func resolveTarget(directory string) (string, error) {
	target, err := ResolveUserPath(directory)
	if err != nil {
		return "", err
	}
	ok, err := isDir(target)
	if err != nil {
		return "", err
	}
	if !ok {
		return "", refuse("the target is not an existing directory: %s", target)
	}
	return target, nil
}

// locateFresh returns the source and target store dirs of a move that has
// no journal.
func locateFresh(found []holder, session, target string, stores []string) (string, string, error) {
	if len(found) == 0 {
		return "", "", refuse("no session store holds %s; looked in %s", session, strings.Join(stores, ", "))
	}
	targetName := workspace.EncodePath(target)
	if len(found) > 1 {
		lines := make([]string, 0, len(found))
		for _, h := range found {
			lines = append(lines, "  "+h.describe(session))
		}
		for _, h := range found {
			if filepath.Base(h.storeDir) == targetName {
				_, held, _ := strings.Cut(h.describe(session), ": ")
				lines = append(lines, fmt.Sprintf("the target's store dir %s already holds %s", h.storeDir, held))
			}
		}
		return "", "", refuse("more than one session store dir holds %s, and `claude --resume` finds no session that is held twice:\n%s", session, strings.Join(lines, "\n"))
	}
	h := found[0]
	if !h.transcript {
		return "", "", refuse("%s holds %s/ but not %s.jsonl, so there is no session transcript to move", h.storeDir, session, session)
	}
	source := h.storeDir
	targetStore := filepath.Join(filepath.Dir(source), targetName)
	if targetName == filepath.Base(source) {
		return "", "", refuse("session %s is already in %s, the store dir of %s; there is nothing to move", session, source, target)
	}
	return source, targetStore, nil
}

// checkJournaled refuses a rerun the journal does not describe.
func checkJournaled(found []holder, session string, j *Journal, path, target string) error {
	if j.TargetCwd != target {
		return refuse("a move of session %s to %s was interrupted and its journal %s is still there; finish it first with `claudewheel move-session %s %s`", session, j.TargetCwd, path, session, j.TargetCwd)
	}
	stray, anyTranscript := false, false
	for _, h := range found {
		if h.storeDir != j.SourceStoreDir && h.storeDir != j.TargetStoreDir {
			stray = true
		}
		if h.transcript {
			anyTranscript = true
		}
	}
	if stray || !anyTranscript {
		listing := "  nothing"
		if len(found) > 0 {
			lines := make([]string, len(found))
			for i, h := range found {
				lines[i] = "  " + h.describe(session)
			}
			listing = strings.Join(lines, "\n")
		}
		return refuse("the journal %s records a move of session %s from %s to %s, but the session stores hold:\n%s", path, session, j.SourceStoreDir, j.TargetStoreDir, listing)
	}
	return nil
}

// fileTimes returns path's access and modification times in nanoseconds.
func fileTimes(path string) (atime, mtime int64, err error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, 0, err
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, fmt.Errorf("cannot read the times of %s: no stat data", path)
	}
	return st.Atim.Nano(), st.Mtim.Nano(), nil
}

// MoveSession moves session to directory's session store, or finishes the
// move its journal records. profiles are every profile's name and config
// directory, as the profile store enumerates them.
//
// Every refusal is a *MoveSessionError returned before anything changes. An
// effect that fails part way returns its own error and leaves the journal,
// so a rerun finishes the move.
func MoveSession(fx *effects.FX, ws workspace.Workspace, profiles []sessions.ProfileConfigDir, session, directory string) (MoveResult, error) {
	if !lifecycle.SessionUUIDRE.MatchString(session) {
		return MoveResult{}, refuse("'%s' is not a session id: a session id is a full lowercase UUID (8-4-4-4-12 hex digits), as Claude Code records it", session)
	}
	target, err := resolveTarget(directory)
	if err != nil {
		return MoveResult{}, err
	}
	jpath := JournalPath(ws, session)
	journal, err := ReadJournal(jpath)
	if err != nil {
		return MoveResult{}, err
	}

	configDirs := make([]string, len(profiles))
	for i, p := range profiles {
		configDirs[i] = p.ConfigDir
	}
	stores, err := sessions.DistinctStoreDirs(sessions.DiscoverProfileDirs(configDirs, ws.SharedDir()))
	if err != nil {
		return MoveResult{}, err
	}
	found, err := holders(stores, session)
	if err != nil {
		return MoveResult{}, err
	}

	var source, targetStore string
	if journal == nil {
		if source, targetStore, err = locateFresh(found, session, target, stores); err != nil {
			return MoveResult{}, err
		}
	} else {
		if err := checkJournaled(found, session, journal, jpath, target); err != nil {
			return MoveResult{}, err
		}
		source, targetStore = journal.SourceStoreDir, journal.TargetStoreDir
	}

	lifecycleDir := ws.Shared().LifecycleDir()
	nowMS := lifecycle.NowMS()
	if err := checkNotRunning(lifecycleDir, session, profiles, nowMS); err != nil {
		return MoveResult{}, err
	}
	if err := checkNoJobs(session, profiles); err != nil {
		return MoveResult{}, err
	}
	folder := filepath.Join(targetStore, session)
	if ok, err := exists(filepath.Join(source, session)); err != nil {
		return MoveResult{}, err
	} else if ok {
		folder = filepath.Join(source, session)
	}
	if err := checkNoInboundLinks(stores, session, folder); err != nil {
		return MoveResult{}, err
	}

	// Read everything the steps need, before the first change.
	oldTranscript := filepath.Join(source, session+".jsonl")
	newTranscript := filepath.Join(targetStore, session+".jsonl")
	atSource, err := exists(oldTranscript)
	if err != nil {
		return MoveResult{}, err
	}
	current := newTranscript
	if atSource {
		current = oldTranscript
	}
	// Stat before reading: the read itself may update the access time.
	atime, mtime, err := fileTimes(current)
	if err != nil {
		return MoveResult{}, err
	}
	text, err := readTranscript(current)
	if err != nil {
		return MoveResult{}, err
	}
	resumed := journal != nil
	if journal == nil {
		journal = &Journal{
			FormatVersion:     JournalFormatVersion,
			Session:           session,
			SourceStoreDir:    source,
			TargetStoreDir:    targetStore,
			TargetCwd:         target,
			OldCwd:            sessionDirectory(text),
			TranscriptAtimeNS: atime,
			TranscriptMtimeNS: mtime,
			EventID:           lifecycle.NewEventID(),
			EventAt:           lifecycle.TimestampAt(nowMS),
			StepsDone:         []string{},
		}
	}
	rewrittenAlready := false
	if !atSource {
		if rewrittenAlready, err = endsWithRelocated(text, session, target); err != nil {
			return MoveResult{}, err
		}
	}
	newText, err := movedTranscript(text, session, filepath.Base(source), filepath.Base(targetStore), target)
	if err != nil {
		return MoveResult{}, err
	}
	lifecycleFile, err := lifecycle.SessionFile(lifecycleDir, session)
	if err != nil {
		return MoveResult{}, err
	}
	events, err := lifecycle.ReadSession(lifecycleFile)
	if err != nil {
		return MoveResult{}, err
	}
	recorded := false
	for _, ev := range events {
		if ev.Head().ID == journal.EventID {
			recorded = true
		}
	}
	sourceExists, err := isDir(source)
	if err != nil {
		return MoveResult{}, err
	}
	var sourceLeft []string
	if sourceExists {
		names, err := dirNames(source)
		if err != nil {
			return MoveResult{}, err
		}
		for _, name := range names {
			if name != session+".jsonl" && name != session {
				sourceLeft = append(sourceLeft, name)
			}
		}
	}

	var performed []string
	done := func(step string) error {
		journal.StepsDone = append(slices.Clone(journal.StepsDone), step)
		if err := writeJournal(fx, jpath, *journal); err != nil {
			return err
		}
		performed = append(performed, step)
		return nil
	}
	isDone := func(step string) bool {
		return slices.Contains(journal.StepsDone, step)
	}

	if !resumed {
		if err := fx.MkdirAll(filepath.Dir(jpath)); err != nil {
			return MoveResult{}, err
		}
		if err := writeJournal(fx, jpath, *journal); err != nil {
			return MoveResult{}, err
		}
	} else {
		fx.Info("Finishing the interrupted move recorded in " + jpath)
	}

	steps := []struct {
		name string
		run  func() error
	}{
		{StepTranscriptMoved, func() error {
			if !atSource {
				return nil
			}
			if err := fx.MkdirAll(targetStore); err != nil {
				return err
			}
			return fx.Rename(oldTranscript, newTranscript)
		}},
		{StepFolderMoved, func() error {
			there, err := lexists(filepath.Join(source, session))
			if err != nil || !there {
				return err
			}
			if err := fx.MkdirAll(targetStore); err != nil {
				return err
			}
			return fx.Rename(filepath.Join(source, session), filepath.Join(targetStore, session))
		}},
		{StepTranscriptRewritten, func() error {
			if rewrittenAlready {
				return nil
			}
			return fx.WriteFileAtomic(newTranscript, newText)
		}},
		{StepTimesRestored, func() error {
			return fx.SetTimes(newTranscript, time.Unix(0, journal.TranscriptAtimeNS), time.Unix(0, journal.TranscriptMtimeNS))
		}},
		{StepLifecycleRecorded, func() error {
			if recorded {
				return nil
			}
			_, err := lifecycle.AppendEvent(fx, lifecycleDir, lifecycle.MovedEvent{
				Header: lifecycle.Header{
					ID:      journal.EventID,
					At:      journal.EventAt,
					Session: session,
					Source:  "user",
				},
				OldCwd:        journal.OldCwd,
				NewCwd:        journal.TargetCwd,
				OldTranscript: oldTranscript,
				NewTranscript: newTranscript,
			})
			return err
		}},
		{StepSourceRemoved, func() error {
			if !sourceExists || len(sourceLeft) > 0 {
				return nil
			}
			return fx.RemoveEmptyDir(source)
		}},
	}
	for _, step := range steps {
		if isDone(step.name) {
			continue
		}
		if err := step.run(); err != nil {
			return MoveResult{}, err
		}
		if err := done(step.name); err != nil {
			return MoveResult{}, err
		}
	}

	if err := fx.Remove(jpath); err != nil {
		return MoveResult{}, err
	}

	verb, stays := "Moved", "stays"
	if fx.Previewing() {
		verb, stays = "Would move", "would stay"
	}
	fx.Info(fmt.Sprintf("%s session %s to %s: %s -> %s", verb, session, target, oldTranscript, newTranscript))
	if len(sourceLeft) > 0 {
		fx.Info(fmt.Sprintf("The store dir %s %s, holding: %s", source, stays, strings.Join(sourceLeft, ", ")))
	}
	return MoveResult{
		SourceStoreDir: source,
		TargetStoreDir: targetStore,
		TargetCwd:      target,
		Resumed:        resumed,
		Steps:          performed,
	}, nil
}
