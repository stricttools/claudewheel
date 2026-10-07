package effects

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/pathstat"
)

// Fresh files and directories are created with these modes, narrowed by the
// umask, as Python's open and mkdir create them.
const (
	newFileMode = 0o666
	newDirMode  = 0o777
)

// freshAtomicMode is the mode an atomic write gives a fresh target.
const freshAtomicMode = 0o644

// SecretFileMode is the mode WriteSecretAtomic always leaves its target with:
// owner read and write only.
const SecretFileMode = 0o600

// WriteFile writes data to path, creating it or truncating what is there.
func (fx *FX) WriteFile(path string, data []byte) error {
	if err := fx.admit("write "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Write(path, data)
		return err
	}
	return os.WriteFile(path, data, newFileMode)
}

// WriteFileAtomic replaces path with data through a staging file in the same
// directory, so a reader sees the whole old file or the whole new one. The
// target keeps its mode across the replacement; a fresh target gets 0644.
// Because the commit is a rename, it also succeeds when the target itself is
// read-only. There is no fsync: this is concurrency safety, not durability.
func (fx *FX) WriteFileAtomic(path string, data []byte) error {
	if err := fx.admit("write "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Write(path, data)
		return err
	}
	return stageAndReplace(path, data, false)
}

// WriteSecretAtomic is WriteFileAtomic for a file holding a secret: the
// staging file is 0600 from its creation and the target is always left 0600,
// whatever mode it had before. Under --dry-run the record carries the mode.
func (fx *FX) WriteSecretAtomic(path string, data []byte) error {
	if err := fx.admit("write "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Write(path, data, strictcli.Mode(SecretFileMode))
		return err
	}
	return stageAndReplace(path, data, true)
}

// stageAndReplace writes data to a unique staging file beside target, named
// after target's full name, sets the final mode on it, and renames it over
// target. Unique staging names keep concurrent writers of one target from
// splicing their bytes together. The staging file is removed on any failure.
func stageAndReplace(target string, data []byte, secret bool) (err error) {
	// os.CreateTemp creates the file 0600, so a secret is never readable by
	// others, even for a moment.
	tmp, err := os.CreateTemp(filepath.Dir(target), filepath.Base(target)+".*.tmp")
	if err != nil {
		return err
	}
	staged := tmp.Name()
	defer func() {
		if err != nil {
			if rmErr := syscall.Unlink(staged); rmErr != nil && rmErr != syscall.ENOENT {
				err = errors.Join(err, &os.PathError{Op: "unlink", Path: staged, Err: rmErr})
			}
		}
	}()
	if _, err = tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err = tmp.Close(); err != nil {
		return err
	}
	mode := os.FileMode(SecretFileMode)
	if !secret {
		info, statErr := os.Stat(target)
		switch {
		case statErr == nil:
			mode = info.Mode().Perm()
		case errors.Is(statErr, fs.ErrNotExist):
			mode = freshAtomicMode
		default:
			return statErr
		}
	}
	if err = os.Chmod(staged, mode); err != nil {
		return err
	}
	return os.Rename(staged, target)
}

// AppendFile appends data to path, creating it when missing. Under --dry-run
// the record is a write of the appended bytes, the handle having no append.
func (fx *FX) AppendFile(path string, data []byte) error {
	if err := fx.admit("append "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Write(path, data)
		return err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|os.O_CREATE, newFileMode)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// MkdirAll creates the directory path and any missing parents; a directory
// already there is not an error.
func (fx *FX) MkdirAll(path string) error {
	if err := fx.admit("mkdir "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Mkdir(path)
		return err
	}
	return os.MkdirAll(path, newDirMode)
}

// MkdirNew creates the directory path, and any missing parents, and fails
// with an error wrapping fs.ErrExist when path itself is already there.
func (fx *FX) MkdirNew(path string) error {
	if err := fx.admit("mkdir "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Mkdir(path)
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), newDirMode); err != nil {
		return err
	}
	return os.Mkdir(path, newDirMode)
}

// Remove deletes the file or symbolic link at path; a missing path is an
// error wrapping fs.ErrNotExist, and a directory is refused.
func (fx *FX) Remove(path string) error {
	return fx.unlink(path, false)
}

// RemoveIfExists deletes the file or symbolic link at path; a missing path
// is not an error, and a directory is refused.
func (fx *FX) RemoveIfExists(path string) error {
	return fx.unlink(path, true)
}

func (fx *FX) unlink(path string, missingOK bool) error {
	if err := fx.admit("remove "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Remove(path)
		return err
	}
	if err := syscall.Unlink(path); err != nil {
		if missingOK && pathstat.NotFoundOrParentNotDirectory(err) {
			return nil
		}
		return &os.PathError{Op: "unlink", Path: path, Err: err}
	}
	return nil
}

// RemoveEmptyDir removes the directory path, which must be empty: a
// directory still holding something is an error, never a recursive delete.
func (fx *FX) RemoveEmptyDir(path string) error {
	if err := fx.admit("remove "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Remove(path)
		return err
	}
	if err := syscall.Rmdir(path); err != nil {
		return &os.PathError{Op: "rmdir", Path: path, Err: err}
	}
	return nil
}

// RemoveTree deletes the directory tree at path. A missing path is an error
// wrapping fs.ErrNotExist; a path that is a symbolic link or not a directory
// is refused.
func (fx *FX) RemoveTree(path string) error {
	if err := fx.admit("remove "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Remove(path)
		return err
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("cannot remove a directory tree through the symbolic link %s", path)
	}
	if !info.IsDir() {
		return &os.PathError{Op: "remove tree", Path: path, Err: syscall.ENOTDIR}
	}
	return os.RemoveAll(path)
}

// Rename renames src to dst within one filesystem, replacing a file at dst.
func (fx *FX) Rename(src, dst string) error {
	if err := fx.admit("rename "+src+" -> "+dst, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Rename(src, dst)
		return err
	}
	return os.Rename(src, dst)
}

// Move moves src to dst. When dst is an existing directory, src moves inside
// it under its own name, and a name already there is an error. A move across
// filesystems copies (symbolic links as links, files and directories with
// their modes and times) and then deletes src. Under --dry-run it records
// the rename of src to dst.
func (fx *FX) Move(src, dst string) error {
	if err := fx.admit("rename "+src+" -> "+dst, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Rename(src, dst)
		return err
	}
	target := dst
	dstIsDir, err := pathstat.IsDir(dst)
	if err != nil {
		return err
	}
	if dstIsDir {
		info, err := os.Stat(dst)
		if err != nil {
			return err
		}
		srcInfo, srcErr := os.Lstat(src)
		if srcErr == nil && srcInfo.Mode()&os.ModeSymlink == 0 && os.SameFile(srcInfo, info) {
			return os.Rename(src, dst)
		}
		target = filepath.Join(dst, filepath.Base(filepath.Clean(src)))
		taken, err := pathstat.Lexists(target)
		if err != nil {
			return err
		}
		if taken {
			return fmt.Errorf("cannot move %s: destination path %s already exists", src, target)
		}
	}
	err = os.Rename(src, target)
	var linkErr *os.LinkError
	if err == nil || !errors.As(err, &linkErr) || linkErr.Err != syscall.EXDEV {
		return err
	}
	srcInfo, err := os.Lstat(src)
	if err != nil {
		return err
	}
	switch {
	case srcInfo.Mode()&os.ModeSymlink != 0:
		dest, err := os.Readlink(src)
		if err != nil {
			return err
		}
		if err := os.Symlink(dest, target); err != nil {
			return err
		}
		return os.Remove(src)
	case srcInfo.IsDir():
		absSrc, err := filepath.Abs(src)
		if err != nil {
			return err
		}
		absTarget, err := filepath.Abs(target)
		if err != nil {
			return err
		}
		if rel, err := filepath.Rel(absSrc, absTarget); err == nil && (rel == "." || filepath.IsLocal(rel)) {
			return fmt.Errorf("cannot move the directory %s into itself, %s", src, target)
		}
		if err := copyTree(src, target, true, liveTree{}); err != nil {
			return err
		}
		return os.RemoveAll(src)
	default:
		if err := copyFile(src, target); err != nil {
			return err
		}
		return os.Remove(src)
	}
}

// Chmod sets the permission bits of path.
func (fx *FX) Chmod(path string, mode os.FileMode) error {
	if err := fx.admit("chmod "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Chmod(path, int(mode.Perm()))
		return err
	}
	return os.Chmod(path, mode)
}

// SetTimes sets the access and modification times of the existing file path,
// to the nanosecond, following a symbolic link. Under --dry-run it records
// the two touch commands that perform it.
func (fx *FX) SetTimes(path string, atime, mtime time.Time) error {
	if err := fx.admit("set times of "+path, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		if _, err := fx.handle.Run(operands([]string{"touch", "--no-create", "-a", "-d", touchTime(atime), path})); err != nil {
			return err
		}
		_, err := fx.handle.Run(operands([]string{"touch", "--no-create", "-m", "-d", touchTime(mtime), path}))
		return err
	}
	return os.Chtimes(path, atime, mtime)
}

// touchTime renders t as GNU touch -d reads it to the nanosecond:
// @<seconds>.<nine digits>.
func touchTime(t time.Time) string {
	ns := t.UnixNano()
	seconds, fraction := ns/1e9, ns%1e9
	if fraction < 0 {
		seconds, fraction = seconds-1, fraction+1e9
	}
	return fmt.Sprintf("@%d.%09d", seconds, fraction)
}

// Symlink creates link as a symbolic link pointing at target (note the
// operand order: the link first). Under --dry-run it records the ln -s that
// performs it.
func (fx *FX) Symlink(link, target string) error {
	if err := fx.admit("ln -s "+target+" "+link, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		_, err := fx.handle.Run(operands([]string{"ln", "-s", target, link}))
		return err
	}
	return os.Symlink(target, link)
}

// CopyFile copies the file src (following a symbolic link) to dst with its
// permission bits and its access and modification times. When dst is an
// existing directory the copy goes inside it under src's name. Under
// --dry-run it reads src and records the write of dst.
func (fx *FX) CopyFile(src, dst string) error {
	dstIsDir, err := pathstat.IsDir(dst)
	if err != nil {
		return err
	}
	if dstIsDir {
		dst = filepath.Join(dst, filepath.Base(src))
	}
	if err := fx.admit("copy "+src+" -> "+dst, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		data, err := os.ReadFile(src)
		if err != nil {
			return err
		}
		_, err = fx.handle.Write(dst, data)
		return err
	}
	return copyFile(src, dst)
}

// CopyTree copies the directory tree src to dst, which must not exist yet;
// missing parents of dst are created. Symbolic links are followed: a link to
// a file is copied as the file, a link to a directory as the directory, and
// a dangling link is an error. Directories and files keep their permission
// bits and times. The first error stops the copy. Under --dry-run it reads
// the tree and records one mkdir per directory and one write per file.
func (fx *FX) CopyTree(src, dst string) error {
	if err := fx.admit("copy tree "+src+" -> "+dst, nil); err != nil {
		return err
	}
	if fx.handle != nil {
		return copyTree(src, dst, false, recordedTree{handle: fx.handle})
	}
	return copyTree(src, dst, false, liveTree{})
}

// treeSink is what copyTree does at each node: perform it, or record it.
type treeSink interface {
	mkdir(dst string, first bool) error
	file(src, dst string) error
	symlink(dest, dst string) error
	finishDir(src, dst string) error
}

// copyTree walks src depth first. keepSymlinks copies a symbolic link as a
// link instead of following it.
func copyTree(src, dst string, keepSymlinks bool, sink treeSink) error {
	return copyTreeLevel(src, dst, keepSymlinks, sink, true)
}

func copyTreeLevel(src, dst string, keepSymlinks bool, sink treeSink, first bool) error {
	entries, err := os.ReadDir(src)
	if err != nil {
		return err
	}
	if err := sink.mkdir(dst, first); err != nil {
		return err
	}
	for _, entry := range entries {
		from := filepath.Join(src, entry.Name())
		to := filepath.Join(dst, entry.Name())
		isLink := entry.Type()&os.ModeSymlink != 0
		if isLink && keepSymlinks {
			dest, err := os.Readlink(from)
			if err != nil {
				return err
			}
			if err := sink.symlink(dest, to); err != nil {
				return err
			}
			continue
		}
		info, err := os.Stat(from)
		if err != nil {
			return err
		}
		if info.IsDir() {
			if err := copyTreeLevel(from, to, keepSymlinks, sink, false); err != nil {
				return err
			}
			continue
		}
		if err := sink.file(from, to); err != nil {
			return err
		}
	}
	return sink.finishDir(src, dst)
}

// liveTree performs a tree copy.
type liveTree struct{}

func (liveTree) mkdir(dst string, first bool) error {
	if first {
		if err := os.MkdirAll(filepath.Dir(dst), newDirMode); err != nil {
			return err
		}
	}
	return os.Mkdir(dst, newDirMode)
}

func (liveTree) file(src, dst string) error { return copyFile(src, dst) }

func (liveTree) symlink(dest, dst string) error { return os.Symlink(dest, dst) }

func (liveTree) finishDir(src, dst string) error { return copyStat(src, dst) }

// recordedTree records a tree copy on the effects handle.
type recordedTree struct {
	handle *strictcli.Effects
}

func (r recordedTree) mkdir(dst string, _ bool) error {
	_, err := r.handle.Mkdir(dst)
	return err
}

func (r recordedTree) file(src, dst string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	_, err = r.handle.Write(dst, data)
	return err
}

func (r recordedTree) symlink(dest, dst string) error {
	_, err := r.handle.Run(operands([]string{"ln", "-s", dest, dst}))
	return err
}

func (recordedTree) finishDir(string, string) error { return nil }

// copyFile copies src's content (following a symbolic link) to dst, then its
// permission bits and times. Copying a file onto itself is refused.
func copyFile(src, dst string) error {
	srcInfo, err := os.Stat(src)
	if err != nil {
		return err
	}
	if dstInfo, err := os.Stat(dst); err == nil && os.SameFile(srcInfo, dstInfo) {
		return fmt.Errorf("cannot copy %s onto itself (%s)", src, dst)
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, newFileMode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return copyStat(src, dst)
}

// copyStat gives dst the permission bits (setuid, setgid, and sticky
// included) and the access and modification times of src.
func copyStat(src, dst string) error {
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("cannot read the status of %s", src)
	}
	if err := os.Chtimes(dst, time.Unix(st.Atim.Sec, st.Atim.Nsec), time.Unix(st.Mtim.Sec, st.Mtim.Nsec)); err != nil {
		return err
	}
	if err := syscall.Chmod(dst, st.Mode&0o7777); err != nil {
		return &os.PathError{Op: "chmod", Path: dst, Err: err}
	}
	return nil
}
