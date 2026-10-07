// Package realpath resolves symbolic links in a path the way Python's
// non-strict os.path.realpath (and so pathlib's non-strict Path.resolve)
// does, so every package that ports a Python resolve shares one resolver.
// It only reads the filesystem.
package realpath

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Resolve returns p absolute with its symbolic links resolved component by
// component: ".." applies to the path resolved so far, a component that does
// not exist or cannot be examined is taken as written (so a dangling link
// resolves to its missing target), and a link loop leaves the rest of the
// path unresolved. A link that cannot be read is an error.
func Resolve(p string) (string, error) {
	resolved, _, err := joinResolved("", p, map[string]*string{})
	if err != nil {
		return "", err
	}
	return filepath.Abs(resolved)
}

// joinResolved is posixpath._joinrealpath: it resolves rest onto the
// already resolved path, reporting false when a link loop stopped it.
func joinResolved(path, rest string, seen map[string]*string) (string, bool, error) {
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
			path, base = split(path)
			if base == ".." {
				path = Join(path, "..", "..")
			}
			continue
		}
		next := Join(path, name)
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
			return Join(next, rest), false, nil
		}
		seen[next] = nil
		target, err := os.Readlink(next)
		if err != nil {
			return "", false, err
		}
		var ok bool
		path, ok, err = joinResolved(path, target, seen)
		if err != nil {
			return "", false, err
		}
		if !ok {
			return Join(path, rest), false, nil
		}
		done := path
		seen[next] = &done
	}
	return path, true, nil
}

// split is posixpath.split.
func split(p string) (head, tail string) {
	i := strings.LastIndexByte(p, '/') + 1
	head, tail = p[:i], p[i:]
	if head != "" && strings.Trim(head, "/") != "" {
		head = strings.TrimRight(head, "/")
	}
	return head, tail
}

// Join is posixpath.join: an absolute part replaces everything before it,
// and nothing is cleaned (unlike filepath.Join, ".." is kept as written).
func Join(base string, parts ...string) string {
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
