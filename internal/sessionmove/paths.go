// Package sessionmove moves Claude Code session data: one session to another
// project directory's session store (MoveSession, journaled so that an
// interrupted move finishes when it is run again), every session of a renamed
// project directory (Mv), session artifacts between two profiles (Migrate),
// and the sessions of an external Claude Code directory into the shared
// store (Import).
//
// Transcripts belong to Claude Code. A line an operation does not change is
// written back as its original bytes; a changed line is decoded as an
// ordered tree (key order, number text, and escapes kept) and written the way
// Claude Code writes it (jsonfile.MarshalTranscriptLine). .claude.json is
// rewritten as an ordered tree too. Every change goes through an explicit
// *effects.FX, which records it instead under --dry-run.
//
// The profile store lives in a package above this one, so every operation
// takes the profiles' config directories from its caller.
package sessionmove

import (
	"fmt"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/realpath"
)

// ResolveUserPath expands a leading ~ or ~name in p and returns it absolute,
// with every symbolic link resolved, as Python's
// Path(p).expanduser().resolve() does: a part that does not exist is kept as
// written. An unset HOME under a leading ~, and an unknown ~name, are errors.
func ResolveUserPath(p string) (string, error) {
	expanded, err := expandUser(p)
	if err != nil {
		return "", err
	}
	return realpath.Resolve(expanded)
}

// expandUser is Python's posixpath.expanduser, refusing what it would leave
// unexpanded.
func expandUser(p string) (string, error) {
	if !strings.HasPrefix(p, "~") {
		return p, nil
	}
	i := strings.IndexByte(p, '/')
	if i < 0 {
		i = len(p)
	}
	var home string
	if i == 1 {
		home = os.Getenv("HOME")
		if home == "" {
			return "", fmt.Errorf("cannot expand ~ in %s: HOME is not set", p)
		}
	} else {
		u, err := user.Lookup(p[1:i])
		if err != nil {
			return "", fmt.Errorf("cannot expand %s in %s: %w", p[:i], p, err)
		}
		home = u.HomeDir
	}
	out := strings.TrimRight(home, "/") + p[i:]
	if out == "" {
		return "/", nil
	}
	return out, nil
}

// dirNames returns the names in dir, sorted.
func dirNames(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	names := make([]string, len(entries))
	for i, e := range entries {
		names[i] = e.Name()
	}
	return names, nil
}

// subdirs returns the paths of the directories in dir (symbolic links to
// directories included), sorted by name.
func subdirs(dir string) ([]string, error) {
	names, err := dirNames(dir)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, name := range names {
		p := filepath.Join(dir, name)
		ok, err := pathstat.IsDir(p)
		if err != nil {
			return nil, err
		}
		if ok {
			out = append(out, p)
		}
	}
	return out, nil
}

// sortPaths sorts paths as Python sorts pathlib paths: by their components.
func sortPaths(paths []string) {
	slices.SortFunc(paths, func(a, b string) int {
		return slices.Compare(strings.Split(a, "/"), strings.Split(b, "/"))
	})
}

// walkMatching returns every path under root whose base name satisfies
// match, root itself excluded, sorted as sortPaths does. Symbolic links to
// directories are listed but not descended into, as pathlib's rglob does.
func walkMatching(root string, match func(name string) bool) ([]string, error) {
	var out []string
	// A trailing slash makes WalkDir follow a root that is a symbolic link,
	// as rglob does.
	if !strings.HasSuffix(root, "/") {
		root += "/"
	}
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path != root && match(d.Name()) {
			out = append(out, path)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	sortPaths(out)
	return out, nil
}

// readTranscript reads one session JSONL file, refusing one that is not
// valid UTF-8, with an error naming it.
func readTranscript(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	if !utf8.Valid(data) {
		return nil, fmt.Errorf("cannot read %s: not valid UTF-8", path)
	}
	return data, nil
}

// continuesWord reports whether the character at s[i] is one Python's
// regular expression class [\w-] matches: a letter, a number, '_', or '-'.
// The end of s, invalid UTF-8, and a lone surrogate match nothing.
func continuesWord(s string, i int) bool {
	if i >= len(s) {
		return false
	}
	r, size := utf8.DecodeRuneInString(s[i:])
	if r == utf8.RuneError && size == 1 {
		return false
	}
	return r == '_' || r == '-' || unicode.IsLetter(r) || unicode.IsNumber(r)
}

// replaceBounded replaces every occurrence of old in s that is not followed
// by a word character or '-' with repl, as Python's
// re.sub(re.escape(old) + r"(?![\w-])", lambda _: repl, s) does, and reports
// whether it replaced anything. old must not be empty.
func replaceBounded(s, old, repl string) (string, bool) {
	var b strings.Builder
	copied, from := 0, 0
	for {
		j := strings.Index(s[from:], old)
		if j < 0 {
			break
		}
		start := from + j
		end := start + len(old)
		if continuesWord(s, end) {
			from = start + 1
			continue
		}
		b.WriteString(s[copied:start])
		b.WriteString(repl)
		copied, from = end, end
	}
	if copied == 0 {
		return s, false
	}
	b.WriteString(s[copied:])
	return b.String(), true
}

// quoted renders a value in a log line as Python's repr renders a plain
// string: in single quotes.
func quoted(s string) string {
	return "'" + s + "'"
}
