package discover

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

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
)

// StateFieldRecentDirs is the one state.json list a discovery declaration's
// state_field or field may name.
const StateFieldRecentDirs = "recent_dirs"

// requiredPath returns d's path, expanded; a declaration without one is an
// error.
func requiredPath(env Env, d appconfig.Discovery) (string, error) {
	if d.Path == nil || *d.Path == "" {
		return "", fmt.Errorf("discovery type %s needs a path", d.Type)
	}
	return ExpandUser(env.Home, *d.Path), nil
}

// directoryListing lists the files in the declared path, newest version
// first.
func directoryListing(env Env, seg appconfig.OptionSegment) (Result, error) {
	d, err := declaration(seg)
	if err != nil {
		return Result{}, err
	}
	dir, err := requiredPath(env, d)
	if err != nil {
		return Result{}, err
	}
	names, err := filesIn(dir)
	if err != nil {
		return Result{}, err
	}
	sortNewestFirst(names)
	return Result{Values: names}, nil
}

// verifyFileIn keeps a value that is still a file in the declared path.
func verifyFileIn(env Env, d appconfig.Discovery) (func(string) (bool, error), error) {
	dir, err := requiredPath(env, d)
	if err != nil {
		return nil, err
	}
	return func(value string) (bool, error) {
		return isFile(filepath.Join(dir, value))
	}, nil
}

// verifyDirectory keeps a value that still names a directory.
func verifyDirectory(env Env, _ appconfig.Discovery) (func(string) (bool, error), error) {
	return func(value string) (bool, error) {
		return isDir(ExpandUser(env.Home, value))
	}, nil
}

// directoryScan lists the recent directories that still exist, then every
// non-hidden subdirectory of each declared parent, in name order, without
// repeats. A directory under the home directory is listed as "~/…". The
// recent directories that no longer exist are pruned from state.json.
func directoryScan(env Env, seg appconfig.OptionSegment) (Result, error) {
	d, err := declaration(seg)
	if err != nil {
		return Result{}, err
	}
	if d.Parents == nil {
		return Result{}, fmt.Errorf("discovery type %s needs parents", d.Type)
	}
	var found []string
	for _, parent := range *d.Parents {
		parentPath := ExpandUser(env.Home, parent)
		ok, err := isDir(parentPath)
		if err != nil {
			return Result{}, err
		}
		if !ok {
			continue
		}
		entries, err := os.ReadDir(parentPath)
		if err != nil {
			return Result{}, err
		}
		for _, e := range entries {
			if strings.HasPrefix(e.Name(), ".") {
				continue
			}
			entry := filepath.Join(parentPath, e.Name())
			ok, err := isDir(entry)
			if err != nil {
				return Result{}, err
			}
			if !ok {
				continue
			}
			if tilde, under := TildePath(env.Home, entry); under {
				found = append(found, tilde)
			} else {
				found = append(found, entry)
			}
		}
	}

	var result Result
	var recent []string
	if d.StateField != nil {
		list, err := stateList(env.State, *d.StateField)
		if err != nil {
			return Result{}, err
		}
		recent = []string{}
		for _, p := range list {
			ok, err := isDir(ExpandUser(env.Home, p))
			if err != nil {
				return Result{}, err
			}
			if ok {
				recent = append(recent, p)
			}
		}
		pruned := slices.Clone(recent)
		result.RecentDirs = &pruned
	}
	result.Values = dedupe(append(recent, found...))
	return result, nil
}

// profileScan lists every profile, sorted by name, with its authentication
// state. An unreadable token file is an error.
func profileScan(env Env, _ appconfig.OptionSegment) (Result, error) {
	return Profiles(env.Profiles)
}

// Profiles is profile discovery on its own, for refreshing the profile
// segment after a profile was created or authenticated.
func Profiles(store profiles.Store) (Result, error) {
	found, err := store.Enumerate()
	if err != nil {
		return Result{}, err
	}
	result := Result{Values: []string{}, Metadata: map[string]Metadata{}}
	for _, p := range found {
		result.Values = append(result.Values, p.Name)
		result.Metadata[p.Name] = Metadata{Auth: &Auth{
			HasToken:       p.HasToken,
			HasCredentials: p.HasCredentials,
			Managed:        p.Name == profiles.DefaultName,
		}}
	}
	return result, nil
}

// stateField lists the declared state.json list, then the segment's own
// values, without repeats.
func stateField(env Env, seg appconfig.OptionSegment) (Result, error) {
	d, err := declaration(seg)
	if err != nil {
		return Result{}, err
	}
	if d.Field == nil {
		return Result{}, fmt.Errorf("discovery type %s needs a field", d.Type)
	}
	list, err := stateList(env.State, *d.Field)
	if err != nil {
		return Result{}, err
	}
	return Result{Values: dedupe(append(slices.Clone(list), seg.Values...))}, nil
}

// stateList returns the state.json list a declaration names.
func stateList(st appconfig.State, field string) ([]string, error) {
	if field != StateFieldRecentDirs {
		return nil, fmt.Errorf("state.json holds no list %q a discovery can read (only %q)", field, StateFieldRecentDirs)
	}
	return st.RecentDirs, nil
}

// filesIn returns the names of the regular files (or links to them) in dir,
// in name order; a dir that is missing or not a directory holds none.
func filesIn(dir string) ([]string, error) {
	ok, err := isDir(dir)
	if err != nil || !ok {
		return []string{}, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	names := []string{}
	for _, e := range entries {
		ok, err := isFile(filepath.Join(dir, e.Name()))
		if err != nil {
			return nil, err
		}
		if ok {
			names = append(names, e.Name())
		}
	}
	return names, nil
}

// absent reports whether a stat error means the path is not there: missing,
// or under something that is not a directory.
func absent(err error) bool {
	return errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ENOTDIR)
}

// isDir reports whether p is a directory, following links. Any stat error
// other than the path being absent is an error.
func isDir(p string) (bool, error) {
	info, err := os.Stat(p)
	if err != nil {
		if absent(err) {
			return false, nil
		}
		return false, err
	}
	return info.IsDir(), nil
}

// isFile reports whether p is a regular file, following links. Any stat
// error other than the path being absent is an error.
func isFile(p string) (bool, error) {
	info, err := os.Stat(p)
	if err != nil {
		if absent(err) {
			return false, nil
		}
		return false, err
	}
	return info.Mode().IsRegular(), nil
}

// sortNewestFirst orders versions newest first by install.CompareVersions,
// keeping the order of versions that compare equal.
func sortNewestFirst(versions []string) {
	slices.SortStableFunc(versions, func(a, b string) int {
		return install.CompareVersions(b, a)
	})
}

// dedupe drops repeats, keeping each value's first position.
func dedupe(values []string) []string {
	seen := make(map[string]bool, len(values))
	out := make([]string, 0, len(values))
	for _, v := range values {
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	return out
}

// ExpandUser expands a leading "~" (to home) or "~name" (to that user's home
// directory) in p, as Python's Path.expanduser does; a "~name" naming no
// user is left as written.
func ExpandUser(home, p string) string {
	if !strings.HasPrefix(p, "~") {
		return p
	}
	i := strings.IndexByte(p, '/')
	if i < 0 {
		i = len(p)
	}
	base := home
	if i > 1 {
		u, err := user.Lookup(p[1:i])
		if err != nil {
			return p
		}
		base = u.HomeDir
	}
	out := strings.TrimRight(base, "/") + p[i:]
	if out == "" {
		return "/"
	}
	return out
}

// TildePath spells p relative to home: "~" for home itself, "~/<rest>" for
// a path under it. It reports false for a path outside home.
func TildePath(home, p string) (string, bool) {
	home = filepath.Clean(home)
	p = filepath.Clean(p)
	if p == home {
		return "~", true
	}
	prefix := home + "/"
	if home == "/" {
		prefix = "/"
	}
	if rest, ok := strings.CutPrefix(p, prefix); ok {
		return "~/" + rest, true
	}
	return "", false
}
