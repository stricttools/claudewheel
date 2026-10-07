// Package pathstat answers whether something is at a path. Exists, Lexists,
// and IsDir count only a path that does not exist (fs.ErrNotExist) as
// absent: any other stat error, a parent that is not a directory or a link
// loop included, is returned. NotFoundOrNotDirectory is the wider reading
// some callers use instead. It only reads the filesystem.
package pathstat

import (
	"errors"
	"io/fs"
	"os"
	"syscall"
)

// Exists reports whether path exists, following symbolic links: a dangling
// link does not exist.
func Exists(path string) (bool, error) {
	_, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// Lexists reports whether anything is at path without following a final
// symbolic link: a dangling link exists.
func Lexists(path string) (bool, error) {
	_, err := os.Lstat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// IsDir reports whether path is a directory, following symbolic links. A
// missing path or a dangling link is not one.
func IsDir(path string) (bool, error) {
	info, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return info.IsDir(), nil
}

// NotFoundOrNotDirectory reports whether a path lookup error means only
// that nothing is there: the path does not exist, or one of its parents is
// not a directory (ENOTDIR). It is false for a nil error.
func NotFoundOrNotDirectory(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOTDIR)
}
