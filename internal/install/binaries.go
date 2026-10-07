package install

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Locator finds Claude Code's binaries: one file per installed version in
// VersionsDir, and the claude symbolic link a launch runs when no version is
// selected. Both lie outside ~/.claudewheel.
type Locator struct {
	VersionsDir   string
	ClaudeSymlink string
}

// LocatorFor builds the locator of the user whose workspace is ws:
// ~/.local/share/claude/versions and ~/.local/bin/claude.
func LocatorFor(ws workspace.Workspace) Locator {
	// The workspace root is $HOME/.claudewheel.
	home := filepath.Dir(ws.Root())
	return Locator{
		VersionsDir:   filepath.Join(home, ".local", "share", "claude", "versions"),
		ClaudeSymlink: filepath.Join(ws.BinDir(), "claude"),
	}
}

// BinaryFor returns the path of an installed version's binary.
func (l Locator) BinaryFor(version string) string {
	return filepath.Join(l.VersionsDir, version)
}

// Fallback returns the claude symbolic link run when no version is selected.
func (l Locator) Fallback() string {
	return l.ClaudeSymlink
}

// InstalledVersions lists the installed version names, newest first by
// VersionSortKey: the regular files (or links to them) in VersionsDir. A
// missing VersionsDir has none.
func (l Locator) InstalledVersions() ([]string, error) {
	if info, err := os.Stat(l.VersionsDir); err != nil || !info.IsDir() {
		return nil, nil
	}
	entries, err := os.ReadDir(l.VersionsDir)
	if err != nil {
		return nil, err
	}
	var versions []string
	for _, e := range entries {
		info, err := os.Stat(filepath.Join(l.VersionsDir, e.Name()))
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		versions = append(versions, e.Name())
	}
	sort.SliceStable(versions, func(i, j int) bool {
		return CompareVersions(versions[i], versions[j]) > 0
	})
	return versions, nil
}

// SymlinkTarget returns the path the claude link resolves to, resolving as
// far as the links lead even when the final target is missing (Python's
// non-strict Path.resolve). The current version is its base name. It
// reports false when the link does not exist or cannot be resolved.
func (l Locator) SymlinkTarget() (string, bool) {
	if _, err := os.Lstat(l.ClaudeSymlink); err != nil {
		return "", false
	}
	return resolveLenient(l.ClaudeSymlink)
}

// maxLinkHops bounds symbolic link chains, as the kernel's ELOOP limit does.
const maxLinkHops = 40

// resolveLenient resolves every symbolic link in p; when the chain ends at a
// missing path, that path (absolute and clean) is the answer.
func resolveLenient(p string) (string, bool) {
	p, err := filepath.Abs(p)
	if err != nil {
		return "", false
	}
	for hop := 0; hop < maxLinkHops; hop++ {
		resolved, err := filepath.EvalSymlinks(p)
		if err == nil {
			return resolved, true
		}
		if !errors.Is(err, fs.ErrNotExist) {
			return "", false
		}
		target, err := os.Readlink(p)
		if err != nil {
			// p itself is missing: the chain ends here.
			if errors.Is(err, fs.ErrNotExist) {
				return filepath.Clean(p), true
			}
			return "", false
		}
		if !filepath.IsAbs(target) {
			target = filepath.Join(filepath.Dir(p), target)
		}
		p = target
	}
	return "", false
}

// EffectiveCLIVersion resolves the Claude Code version a launch runs: the
// version selection when set, else the base name of the claude link's
// target. It reports false when neither is available. Both the pre-launch
// model-version guard and the model picker's dimming read it, so they cannot
// disagree.
func EffectiveCLIVersion(selected string, l Locator) (string, bool) {
	if selected != "" {
		return selected, true
	}
	target, ok := l.SymlinkTarget()
	if !ok {
		return "", false
	}
	return filepath.Base(target), true
}

// VersionSortKey splits a version on "." into integers for numeric
// ordering; a part that is not an integer counts as 0.
func VersionSortKey(version string) []int {
	parts := strings.Split(version, ".")
	key := make([]int, len(parts))
	for i, part := range parts {
		n, err := strconv.Atoi(part)
		if err == nil {
			key[i] = n
		}
	}
	return key
}

// CompareVersions orders two versions by VersionSortKey, element by element,
// a key that is a prefix of the other ordering first: -1, 0, or +1.
func CompareVersions(a, b string) int {
	return slices.Compare(VersionSortKey(a), VersionSortKey(b))
}

// CheckVersionName refuses a version that cannot name one file in the
// versions directory: empty, ".", "..", or holding a "/".
func CheckVersionName(version string) error {
	if version == "" || version == "." || version == ".." || strings.Contains(version, "/") {
		return fmt.Errorf("%q is not a Claude Code version name", version)
	}
	return nil
}

// Uninstall deletes an installed version's binary and returns its path. It
// refuses a version that is not installed and the version the claude link
// currently resolves to, since deleting it would break the claude command.
func Uninstall(fx *effects.FX, l Locator, version string) (string, error) {
	if err := CheckVersionName(version); err != nil {
		return "", err
	}
	target := l.BinaryFor(version)
	if _, err := os.Stat(target); err != nil {
		return "", fmt.Errorf("version %s is not installed at %s", version, target)
	}
	if resolved, ok := l.SymlinkTarget(); ok && filepath.Base(resolved) == version {
		return "", fmt.Errorf("refusing to uninstall %s: it is the current `claude` symlink target (%s); switch to another version first",
			version, l.ClaudeSymlink)
	}
	if err := fx.Remove(target); err != nil {
		return "", fmt.Errorf("failed to delete %s: %w", target, err)
	}
	return target, nil
}
