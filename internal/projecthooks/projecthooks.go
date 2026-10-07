// Package projecthooks reads and fingerprints the Claude Code hooks a target
// project contributes through .claude/settings.json and
// .claude/settings.local.json (each file's top-level hooks section).
//
// Those hooks run arbitrary commands, so a launch shows them for approval
// and asks again whenever they change: the fingerprint detects a first
// sighting and a change, and the listing is what the approval page shows.
// Malformed JSON is a hard error naming the file. Claude Code owns these
// files, so they are read as ordered trees and their hooks schema is
// tolerated loosely.
package projecthooks

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/user"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pyrepr"
)

// settingsFiles are the per-project settings files that may carry a hooks
// section, in the order they are read and combined.
func settingsFiles() []string {
	return []string{"settings.json", "settings.local.json"}
}

// MalformedError reports a project settings file that is not valid JSON.
// Filename is the bare name (settings.local.json), for an abort message.
type MalformedError struct {
	Filename string
	Err      error
}

func (e *MalformedError) Error() string {
	return "malformed project hooks config: " + e.Filename
}

func (e *MalformedError) Unwrap() error { return e.Err }

// ProjectHooks are the combined hooks of a project, keyed by source file
// name. A file that is absent, has no hooks key, or has an empty or false
// hooks value is not in Sources.
type ProjectHooks struct {
	// Sources maps each contributing file's base name to its hooks value,
	// in settingsFiles order.
	Sources *jsonfile.Object
}

// HasHooks reports whether any settings file contributes a hooks section.
func (h ProjectHooks) HasHooks() bool {
	return h.Sources != nil && h.Sources.Len() > 0
}

// Fingerprint is the sha256 hex of the combined hooks written as compact
// JSON with sorted keys, so key order and whitespace do not change it while
// any change in content, including moving a hook between the two files, does.
// It equals the fingerprint the Python implementation stored for the same
// hooks, unless a number in them is a float Python wrote in another form.
func (h ProjectHooks) Fingerprint() (string, error) {
	canonical, err := jsonfile.MarshalSortedCompactASCII(h.Sources)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

// ListingLines flattens the hooks into one line per command for the approval
// page: the event, the matcher when present, and the command. Missing or
// extra fields never fail; an entry with a matcher and no commands still
// gives a line. Lines are ordered by file name, then event name.
func (h ProjectHooks) ListingLines() []string {
	var lines []string
	if h.Sources == nil {
		return nil
	}
	filenames := h.Sources.Keys()
	sort.Strings(filenames)
	for _, filename := range filenames {
		v, _ := h.Sources.Get(filename)
		hooks, ok := v.(*jsonfile.Object)
		if !ok {
			continue
		}
		events := hooks.Keys()
		sort.Strings(events)
		for _, event := range events {
			ev, _ := hooks.Get(event)
			entries, ok := ev.([]jsonfile.Value)
			if !ok {
				continue
			}
			for _, entry := range entries {
				lines = append(lines, entryLines(event, entry)...)
			}
		}
	}
	return lines
}

// entryLines renders one hooks entry ({matcher?, hooks: [...]}).
func entryLines(event string, entry jsonfile.Value) []string {
	var matcher jsonfile.Value
	var inner jsonfile.Value = []jsonfile.Value{}
	if o, ok := entry.(*jsonfile.Object); ok {
		matcher, _ = o.Get("matcher")
		if v, ok := o.Get("hooks"); ok {
			inner = v
		}
	}
	prefix := event
	if s, isString := matcher.(string); matcher != nil && !(isString && s == "") {
		prefix += "  [matcher: " + pyrepr.Str(matcher) + "]"
	}
	var commands []string
	if list, ok := inner.([]jsonfile.Value); ok {
		for _, h := range list {
			if o, ok := h.(*jsonfile.Object); ok {
				if cmd, ok := o.Get("command"); ok {
					commands = append(commands, pyrepr.Str(cmd))
				}
			}
		}
	}
	if len(commands) == 0 {
		return []string{prefix}
	}
	lines := make([]string, len(commands))
	for i, cmd := range commands {
		lines[i] = prefix + "  ->  " + cmd
	}
	return lines
}

// truthy reports Python truthiness of a tree value.
func truthy(v jsonfile.Value) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case json.Number:
		f, err := strconv.ParseFloat(string(t), 64)
		return err != nil || f != 0
	case string:
		return t != ""
	case []jsonfile.Value:
		return len(t) > 0
	case *jsonfile.Object:
		return t.Len() > 0
	}
	return true
}

// loadHooksSection returns the hooks value of the settings file at path, or
// false when the file cannot be read, is not an object, or has no truthy
// hooks value. Invalid JSON is a *MalformedError.
func loadHooksSection(path string) (jsonfile.Value, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, false, nil
	}
	v, err := jsonfile.Decode(data)
	if err != nil {
		return nil, false, &MalformedError{Filename: filepath.Base(path), Err: err}
	}
	o, ok := v.(*jsonfile.Object)
	if !ok {
		return nil, false, nil
	}
	hooks, ok := o.Get("hooks")
	if !ok || !truthy(hooks) {
		return nil, false, nil
	}
	return hooks, true, nil
}

// Read reads the combined hooks a project under directory declares, from
// <directory>/.claude/settings.json and settings.local.json; a leading ~ in
// directory is expanded. Absent files and absent or empty hooks are skipped;
// invalid JSON in either file is a *MalformedError.
func Read(directory string) (ProjectHooks, error) {
	expanded, err := normalizedPath(directory)
	if err != nil {
		return ProjectHooks{}, err
	}
	claudeDir := filepath.Join(expanded, ".claude")
	sources := jsonfile.NewObject()
	for _, name := range settingsFiles() {
		hooks, ok, err := loadHooksSection(filepath.Join(claudeDir, name))
		if err != nil {
			return ProjectHooks{}, err
		}
		if ok {
			sources.Set(name, hooks)
		}
	}
	return ProjectHooks{Sources: sources}, nil
}

// TargetDirectory resolves the directory a launch targets: the directory
// selection with ~ expanded when it is set (non-empty), the current working
// directory otherwise, the rule the launch config resolver applies.
func TargetDirectory(directorySelection string) (string, error) {
	if directorySelection != "" {
		return normalizedPath(directorySelection)
	}
	return os.Getwd()
}

// normalizedPath expands a leading ~ or ~name in p as Python's
// Path.expanduser does and normalizes the result as pathlib prints it, so a
// directory reads as the Python implementation stored it (approvals are
// keyed by it). ~ needs HOME; a ~name whose user does not exist is left as
// it is.
func normalizedPath(p string) (string, error) {
	if strings.HasPrefix(p, "~") {
		head, rest, hasSlash := strings.Cut(p, "/")
		var home string
		if head == "~" {
			home = os.Getenv("HOME")
			if home == "" {
				return "", errors.New("cannot expand ~: HOME is not set")
			}
		} else if u, err := user.Lookup(head[1:]); err == nil {
			home = u.HomeDir
		} else {
			var unknown user.UnknownUserError
			if !errors.As(err, &unknown) {
				return "", err
			}
			home = head
		}
		p = strings.TrimRight(home, "/")
		if p == "" {
			p = "/"
		}
		if hasSlash {
			p += "/" + rest
		}
	}
	return pathlibString(p), nil
}

// pathlibString writes p as str(PurePosixPath(p)): empty and "." parts
// dropped, no trailing slash, a leading pair of slashes kept, "." for an
// empty path. ".." parts stay.
func pathlibString(p string) string {
	prefix := ""
	switch {
	case strings.HasPrefix(p, "//") && !strings.HasPrefix(p, "///"):
		prefix = "//"
	case strings.HasPrefix(p, "/"):
		prefix = "/"
	}
	var parts []string
	for _, part := range strings.Split(p, "/") {
		if part != "" && part != "." {
			parts = append(parts, part)
		}
	}
	out := prefix + strings.Join(parts, "/")
	if out == "" {
		return "."
	}
	return out
}
