// Package pathstat answers whether something is at a path. Every helper
// counts a path as absent when it does not exist (fs.ErrNotExist) or when a
// parent is not a directory (ENOTDIR), since nothing can be there; any
// other stat error, a link loop (ELOOP) or a refused search (EACCES)
// included, is returned. It only reads the filesystem.
package pathstat

import (
	"errors"
	"io/fs"
	"os"

	"golang.org/x/sys/unix"
)

// NotFoundOrParentNotDirectory reports whether a path lookup error means
// nothing is at the path: it does not exist (fs.ErrNotExist), or a parent
// is not a directory (ENOTDIR). It is false for a nil error. Callers that
// open or read a path themselves use it to agree with the helpers here.
func NotFoundOrParentNotDirectory(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, unix.ENOTDIR)
}

// stat is os.Stat, or os.Lstat when follow is false, with a nil
// information and no error when nothing is at the path.
func stat(path string, follow bool) (fs.FileInfo, error) {
	var info fs.FileInfo
	var err error
	if follow {
		info, err = os.Stat(path)
	} else {
		info, err = os.Lstat(path)
	}
	if NotFoundOrParentNotDirectory(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return info, nil
}

// Exists reports whether path exists, following symbolic links: a dangling
// link does not exist.
func Exists(path string) (bool, error) {
	info, err := stat(path, true)
	return info != nil, err
}

// Lexists reports whether anything is at path without following a final
// symbolic link: a dangling link exists.
func Lexists(path string) (bool, error) {
	info, err := stat(path, false)
	return info != nil, err
}

// IsDir reports whether path is a directory, following symbolic links. A
// missing path or a dangling link is not one.
func IsDir(path string) (bool, error) {
	info, err := stat(path, true)
	return info != nil && info.IsDir(), err
}

// IsFile reports whether path is a regular file, following symbolic links.
// A missing path or a dangling link is not one.
func IsFile(path string) (bool, error) {
	info, err := stat(path, true)
	return info != nil && info.Mode().IsRegular(), err
}

// IsSymlink reports whether path itself is a symbolic link, dangling or not.
func IsSymlink(path string) (bool, error) {
	info, err := stat(path, false)
	return info != nil && info.Mode()&fs.ModeSymlink != 0, err
}

// IsExecutableFile reports whether path is a regular file, following
// symbolic links, that this process may execute. The test is access(2) with
// X_OK, which applies the process's user and groups, ACLs, and a noexec
// mount, where the mode's execute bits alone would accept a file only
// another user may run. A refused access (EACCES) is not executable, and a
// path that went away between the two checks is absent; any other access
// error is returned.
func IsExecutableFile(path string) (bool, error) {
	ok, err := IsFile(path)
	if err != nil || !ok {
		return false, err
	}
	err = unix.Access(path, unix.X_OK)
	switch {
	case err == nil:
		return true, nil
	case errors.Is(err, unix.EACCES), NotFoundOrParentNotDirectory(err):
		return false, nil
	}
	return false, &fs.PathError{Op: "access", Path: path, Err: err}
}
