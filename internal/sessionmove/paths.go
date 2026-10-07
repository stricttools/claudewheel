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
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/user"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"unicode"
	"unicode/utf8"
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
	return realPath(expanded)
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

// realPath is Python's non-strict os.path.realpath: symbolic links are
// resolved component by component, ".." applies to the path resolved so
// far, a component that cannot be examined is taken as it is, and a link
// loop leaves the rest unresolved.
func realPath(p string) (string, error) {
	resolved, _, err := joinRealPath("", p, map[string]*string{})
	if err != nil {
		return "", err
	}
	return filepath.Abs(resolved)
}

func joinRealPath(path, rest string, seen map[string]*string) (string, bool, error) {
	if strings.HasPrefix(rest, "/") {
		rest = rest[1:]
		path = "/"
	}
	for rest != "" {
		var name string
		name, rest, _ = strings.Cut(rest, "/")
		if name == "" || name == "." {
			continue
		}
		if name == ".." {
			if path == "" {
				path = ".."
				continue
			}
			var base string
			path, base = pySplit(path)
			if base == ".." {
				path = pyJoin(path, "..", "..")
			}
			continue
		}
		next := pyJoin(path, name)
		info, err := os.Lstat(next)
		if err != nil || info.Mode()&fs.ModeSymlink == 0 {
			path = next
			continue
		}
		if prior, ok := seen[next]; ok {
			if prior != nil {
				path = *prior
				continue
			}
			return pyJoin(next, rest), false, nil
		}
		seen[next] = nil
		target, err := os.Readlink(next)
		if err != nil {
			return "", false, err
		}
		var ok bool
		path, ok, err = joinRealPath(path, target, seen)
		if err != nil {
			return "", false, err
		}
		if !ok {
			return pyJoin(path, rest), false, nil
		}
		done := path
		seen[next] = &done
	}
	return path, true, nil
}

// pySplit is posixpath.split.
func pySplit(p string) (head, tail string) {
	i := strings.LastIndexByte(p, '/') + 1
	head, tail = p[:i], p[i:]
	if head != "" && strings.Trim(head, "/") != "" {
		head = strings.TrimRight(head, "/")
	}
	return head, tail
}

// pyJoin is posixpath.join.
func pyJoin(base string, parts ...string) string {
	out := base
	for _, b := range parts {
		switch {
		case strings.HasPrefix(b, "/"):
			out = b
		case out == "" || strings.HasSuffix(out, "/"):
			out += b
		default:
			out += "/" + b
		}
	}
	return out
}

// absent reports whether a stat error means the path is not there: missing,
// under a non-directory, or behind a link loop. Any other error is real.
func absent(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOTDIR) || errors.Is(err, syscall.ELOOP)
}

// statPath returns path's information, following symbolic links when follow
// is set, and nil when the path is not there.
func statPath(path string, follow bool) (fs.FileInfo, error) {
	var info fs.FileInfo
	var err error
	if follow {
		info, err = os.Stat(path)
	} else {
		info, err = os.Lstat(path)
	}
	if err != nil {
		if absent(err) {
			return nil, nil
		}
		return nil, err
	}
	return info, nil
}

// exists follows symbolic links, so a dangling link does not exist.
func exists(path string) (bool, error) {
	info, err := statPath(path, true)
	return info != nil, err
}

// lexists does not follow a final symbolic link, so a dangling link exists.
func lexists(path string) (bool, error) {
	info, err := statPath(path, false)
	return info != nil, err
}

// isDir follows symbolic links.
func isDir(path string) (bool, error) {
	info, err := statPath(path, true)
	return info != nil && info.IsDir(), err
}

// isFile follows symbolic links.
func isFile(path string) (bool, error) {
	info, err := statPath(path, true)
	return info != nil && info.Mode().IsRegular(), err
}

func isSymlink(path string) (bool, error) {
	info, err := statPath(path, false)
	return info != nil && info.Mode()&fs.ModeSymlink != 0, err
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
		ok, err := isDir(p)
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
