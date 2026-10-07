// Package scratchpad scans the per-user Claude Code scratchpad tree under
// /tmp for stale data and deletes what the user confirms.
//
// Claude Code sessions write scratchpad data under
// /tmp/claude-<uid>/<encoded-project>/<session-uuid>/..., one top-level
// subdirectory per project. A project directory is stale when nothing
// anywhere in its tree was modified within StaleDays. Sizes are allocated tmpfs
// block usage (st_blocks * 512) of regular files; symbolic links are never
// followed, so link targets outside /tmp are never charged to it. This
// package never writes under /tmp itself; deletion goes through effects.
package scratchpad

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/pathstat"
)

// StaleDays is how long a project directory's tree must have gone
// unmodified to be stale.
const StaleDays = 14

// TmpClaudeDir returns the per-user Claude Code scratchpad root,
// /tmp/claude-<uid>.
func TmpClaudeDir() string {
	return "/tmp/claude-" + strconv.Itoa(os.Getuid())
}

// Dir is one top-level project directory of the scratchpad tree with its
// measured facts.
type Dir struct {
	Path string
	Name string
	// SizeBytes is the allocated block usage of the regular files in the tree.
	SizeBytes int64
	// NewestMtime is the newest modification time (lstat, never followed)
	// of any entry in the tree, the directory itself included.
	NewestMtime time.Time
}

// AgeDays is the number of days, fractional, since the newest activity in
// the tree, relative to now; never negative.
func (d Dir) AgeDays(now time.Time) float64 {
	age := now.Sub(d.NewestMtime).Hours() / 24
	if age < 0 {
		return 0
	}
	return age
}

// IsStale reports whether the newest activity is more than staleDays before
// now.
func (d Dir) IsStale(now time.Time, staleDays int) bool {
	return d.AgeDays(now) > float64(staleDays)
}

// ScanTree returns the allocated block usage of the regular files under root and
// the newest lstat modification time of root and every entry under it. It
// never descends into a symbolic link. Entries that vanish or cannot be read
// during the walk are skipped (Claude Code changes the tree concurrently).
func ScanTree(root string) (int64, time.Time) {
	var total int64
	var newest time.Time
	if info, err := os.Lstat(root); err == nil {
		newest = info.ModTime()
	}
	filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			if entry != nil && entry.IsDir() && path != root {
				return fs.SkipDir
			}
			return nil
		}
		if path == root {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return nil
		}
		if info.ModTime().After(newest) {
			newest = info.ModTime()
		}
		if info.Mode().IsRegular() {
			if st, ok := info.Sys().(*syscall.Stat_t); ok {
				total += st.Blocks * 512
			}
		}
		return nil
	})
	return total, newest
}

// ScanDirs returns one Dir per immediate subdirectory of root, sorted by
// name, skipping other entries and top-level symbolic links. A missing root,
// or one that is not a directory, has none.
func ScanDirs(root string) ([]Dir, error) {
	isDir, err := pathstat.IsDir(root)
	if err != nil {
		return nil, err
	}
	if !isDir {
		return nil, nil
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var dirs []Dir
	for _, e := range entries {
		if e.Type()&fs.ModeSymlink != 0 || !e.IsDir() {
			continue
		}
		path := filepath.Join(root, e.Name())
		size, newest := ScanTree(path)
		dirs = append(dirs, Dir{Path: path, Name: e.Name(), SizeBytes: size, NewestMtime: newest})
	}
	return dirs, nil
}

// Remove deletes each directory tree in dirs through fx. Every directory is
// attempted; the errors of those that could not be deleted are returned
// joined, each naming its directory.
func Remove(fx *effects.FX, dirs []Dir) error {
	var errs []error
	for _, d := range dirs {
		if err := fx.RemoveTree(d.Path); err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", d.Name, err))
		}
	}
	return errors.Join(errs...)
}
