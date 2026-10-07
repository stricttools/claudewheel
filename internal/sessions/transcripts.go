// Package sessions reads what Claude Code keeps about its sessions: the
// transcript JSONL files in a session store (locating a session, its project
// path, its user-assigned title, and orphaned project directories), the
// per-process registry under <config dir>/sessions/<pid>.json read into
// liveness-checked records, and the session stores of every profile.
//
// Claude Code owns every file read here, so they are decoded tolerantly as
// ordered trees: a line or field of an unexpected shape is skipped, never an
// error. The one mutation, pruning dead registry files, goes through an
// explicit *effects.FX.
package sessions

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// MaxCwdScanLines is how many lines GetSessionCwd reads before giving up.
const MaxCwdScanLines = 10

// CustomTitleMarker is the record type Claude Code writes for a user-assigned
// session title: {"type":"custom-title","sessionId":...,"customTitle":...}.
// Lines are checked for it as a substring before being parsed. Auto-generated
// "ai-title" and "agent-name" records never match it.
const CustomTitleMarker = "custom-title"

// RelocatedRecordType is the type of the record Claude Code appends to a
// transcript it moved to another directory's store:
// {"type":"relocated","sessionId":...,"relocatedCwd":<new directory>}.
const RelocatedRecordType = "relocated"

// SessionInfo is a session located in a session store.
type SessionInfo struct {
	SessionID string
	JSONLPath string
	// EncodedCwd is the name of the store directory holding the transcript.
	EncodedCwd string
	// Cwd is the one recorded cwd that encodes to the store directory's name.
	Cwd string
}

// TitleMatch is a transcript whose custom-title record matches a requested
// title.
type TitleMatch struct {
	SessionID  string
	ProjectDir string
	Mtime      time.Time
}

// OrphanedProject is a store directory whose original cwd no longer exists.
type OrphanedProject struct {
	EncodedCwd     string
	Cwd            string
	SessionCount   int
	TotalSizeBytes int64
	// ProjectsDir is the full path of the store directory.
	ProjectsDir string
}

// StoreDirPathError reports a store directory whose sessions do not record
// one and only one path matching its name.
type StoreDirPathError struct {
	StoreDir string
	// Found holds the matching paths, sorted; empty when none was recorded.
	Found []string
}

func (e *StoreDirPathError) Error() string {
	if len(e.Found) == 0 {
		return fmt.Sprintf("%s: no session recorded a cwd that encodes to this dir's name", e.StoreDir)
	}
	return fmt.Sprintf("%s: its sessions recorded several paths that encode to this dir's name: %s",
		e.StoreDir, strings.Join(e.Found, ", "))
}

// topLevelJSONL lists the *.jsonl files directly inside dir, sorted by name,
// skipping hidden names as a shell glob does. Nested transcripts
// (<session>/subagents/*.jsonl) are never listed.
func topLevelJSONL(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") || !strings.HasSuffix(name, ".jsonl") {
			continue
		}
		paths = append(paths, filepath.Join(dir, name))
	}
	return paths, nil
}

// splitLines splits a transcript into its lines.
func splitLines(data []byte) [][]byte {
	return bytes.Split(data, []byte("\n"))
}

// parseObject decodes one transcript line as a JSON object, or returns nil
// when it is not one (a live session's last line may be partial).
func parseObject(line []byte) *jsonfile.Object {
	v, err := jsonfile.Decode(bytes.TrimSpace(line))
	if err != nil {
		return nil
	}
	o, _ := v.(*jsonfile.Object)
	return o
}

// stringField returns o[key] when it is a string.
func stringField(o *jsonfile.Object, key string) (string, bool) {
	v, ok := o.Get(key)
	if !ok {
		return "", false
	}
	s, ok := v.(string)
	return s, ok
}

// RecordedStoreCwds returns every directory a store directory's own sessions
// recorded that encodes to the directory's name, sorted.
//
// A transcript records the path its session started in as the top-level
// "cwd" of its lines. A session Claude Code's /cd moved here keeps its old
// cwd values and ends with a relocated record naming its new directory, which
// Claude Code then reads as the session's directory, and so does this
// function: a transcript holding a relocated record contributes the
// relocatedCwd of its last one and none of its cwd values. Only values whose
// encoding equals the directory's name are kept; another value is a directory
// change during the session.
//
// More than one result means distinct paths share the name (the encoding is
// lossy); none means the sessions never recorded the path. A line that is not
// a JSON object is skipped. A transcript that cannot be read is an error
// naming it.
func RecordedStoreCwds(storeDir string) ([]string, error) {
	paths, err := topLevelJSONL(storeDir)
	if err != nil {
		return nil, err
	}
	name := filepath.Base(storeDir)
	found := map[string]bool{}
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("cannot read %s: %w", path, err)
		}
		cwds := map[string]bool{}
		relocated, hasRelocated := "", false
		for _, line := range splitLines(data) {
			if !bytes.Contains(line, []byte(`"cwd"`)) && !bytes.Contains(line, []byte(`"relocatedCwd"`)) {
				continue
			}
			o := parseObject(line)
			if o == nil {
				continue
			}
			if t, _ := stringField(o, "type"); t == RelocatedRecordType {
				if movedTo, ok := stringField(o, "relocatedCwd"); ok {
					relocated, hasRelocated = movedTo, true
				}
				continue
			}
			if cwd, ok := stringField(o, "cwd"); ok {
				cwds[cwd] = true
			}
		}
		if hasRelocated {
			cwds = map[string]bool{relocated: true}
		}
		for d := range cwds {
			if workspace.EncodePath(d) == name {
				found[d] = true
			}
		}
	}
	out := make([]string, 0, len(found))
	for d := range found {
		out = append(out, d)
	}
	sort.Strings(out)
	return out, nil
}

// StoreDirPath returns the one path a store directory's own sessions
// recorded for it (the single value of RecordedStoreCwds). None, or more
// than one, is a *StoreDirPathError: the directory's path is then unknown.
func StoreDirPath(storeDir string) (string, error) {
	cwds, err := RecordedStoreCwds(storeDir)
	if err != nil {
		return "", err
	}
	if len(cwds) == 1 {
		return cwds[0], nil
	}
	return "", &StoreDirPathError{StoreDir: storeDir, Found: cwds}
}

// GetSessionCwd returns the first cwd value within the first maxLines lines
// of a transcript. It reports false when the file is missing or unreadable,
// or when the first line carrying a cwd holds a non-string; lines that are
// not JSON objects are skipped.
func GetSessionCwd(jsonlPath string, maxLines int) (string, bool) {
	f, err := os.Open(jsonlPath)
	if err != nil {
		return "", false
	}
	defer f.Close()
	r := bufio.NewReader(f)
	for i := 0; i < maxLines; i++ {
		line, err := r.ReadBytes('\n')
		if len(line) > 0 {
			if o := parseObject(line); o != nil {
				if v, ok := o.Get("cwd"); ok {
					s, isString := v.(string)
					return s, isString
				}
			}
		}
		if err != nil {
			return "", false
		}
	}
	return "", false
}

// FindSession locates the transcript <sharedProjectsDir>/*/<sessionID>.jsonl
// and returns nil when there is none. Its project path is its store
// directory's (StoreDirPath), so a store directory whose sessions do not
// record one and only one matching path is a *StoreDirPathError.
func FindSession(sessionID, sharedProjectsDir string) (*SessionInfo, error) {
	entries, err := os.ReadDir(sharedProjectsDir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		storeDir := filepath.Join(sharedProjectsDir, e.Name())
		jsonlPath := filepath.Join(storeDir, sessionID+".jsonl")
		there, err := pathstat.Lexists(jsonlPath)
		if err != nil {
			return nil, err
		}
		if !there {
			continue
		}
		cwd, err := StoreDirPath(storeDir)
		if err != nil {
			return nil, err
		}
		return &SessionInfo{
			SessionID:  sessionID,
			JSONLPath:  jsonlPath,
			EncodedCwd: e.Name(),
			Cwd:        cwd,
		}, nil
	}
	return nil, nil
}

// findTitleInFile returns the session id of a transcript holding a
// custom-title record whose customTitle equals title: the record's sessionId
// when it is a string, the file's stem otherwise. Only lines containing
// CustomTitleMarker are parsed; an unreadable file matches nothing.
func findTitleInFile(jsonlPath, title string) (string, bool) {
	data, err := os.ReadFile(jsonlPath)
	if err != nil || !bytes.Contains(data, []byte(CustomTitleMarker)) {
		return "", false
	}
	for _, line := range splitLines(data) {
		if !bytes.Contains(line, []byte(CustomTitleMarker)) {
			continue
		}
		o := parseObject(line)
		if o == nil {
			continue
		}
		if t, _ := stringField(o, "type"); t != CustomTitleMarker {
			continue
		}
		if got, ok := stringField(o, "customTitle"); !ok || got != title {
			continue
		}
		if sid, ok := stringField(o, "sessionId"); ok {
			return sid, true
		}
		return strings.TrimSuffix(filepath.Base(jsonlPath), ".jsonl"), true
	}
	return "", false
}

// FindSessionsByTitle returns the sessions whose user-assigned title equals
// title, one per matching transcript, in projectDirs order and by file name
// within a directory. Only the top-level *.jsonl files of each directory are
// read: subagent transcripts must never match a top-level resume. A path in
// projectDirs that is not a directory is skipped.
func FindSessionsByTitle(title string, projectDirs []string) ([]TitleMatch, error) {
	var results []TitleMatch
	for _, dir := range projectDirs {
		isDir, err := pathstat.IsDir(dir)
		if err != nil {
			return nil, err
		}
		if !isDir {
			continue
		}
		paths, err := topLevelJSONL(dir)
		if err != nil {
			return nil, err
		}
		for _, path := range paths {
			sid, ok := findTitleInFile(path, title)
			if !ok {
				continue
			}
			info, err := os.Stat(path)
			if err != nil {
				return nil, err
			}
			results = append(results, TitleMatch{SessionID: sid, ProjectDir: dir, Mtime: info.ModTime()})
		}
	}
	return results, nil
}

// FindOrphanedProjectDirs returns the store directories under
// sharedProjectsDir whose cwd, read from the newest transcript, no longer
// exists as a directory. A store directory with no transcript, or whose
// newest transcript records no cwd, is skipped. A missing sharedProjectsDir
// has none.
func FindOrphanedProjectDirs(sharedProjectsDir string) ([]OrphanedProject, error) {
	isDir, err := pathstat.IsDir(sharedProjectsDir)
	if err != nil {
		return nil, err
	}
	if !isDir {
		return nil, nil
	}
	entries, err := os.ReadDir(sharedProjectsDir)
	if err != nil {
		return nil, err
	}
	var results []OrphanedProject
	for _, e := range entries {
		dir := filepath.Join(sharedProjectsDir, e.Name())
		isDir, err := pathstat.IsDir(dir)
		if err != nil {
			return nil, err
		}
		if !isDir {
			continue
		}
		paths, err := topLevelJSONL(dir)
		if err != nil {
			return nil, err
		}
		if len(paths) == 0 {
			continue
		}
		infos := make([]fs.FileInfo, len(paths))
		for i, p := range paths {
			if infos[i], err = os.Stat(p); err != nil {
				return nil, err
			}
		}
		newest := 0
		for i := range infos {
			if infos[i].ModTime().After(infos[newest].ModTime()) {
				newest = i
			}
		}
		cwd, ok := GetSessionCwd(paths[newest], MaxCwdScanLines)
		if !ok {
			continue
		}
		cwdIsDir, err := pathstat.IsDir(cwd)
		if err != nil {
			return nil, err
		}
		if cwdIsDir {
			continue
		}
		var total int64
		for _, info := range infos {
			total += info.Size()
		}
		results = append(results, OrphanedProject{
			EncodedCwd:     e.Name(),
			Cwd:            cwd,
			SessionCount:   len(paths),
			TotalSizeBytes: total,
			ProjectsDir:    dir,
		})
	}
	return results, nil
}
