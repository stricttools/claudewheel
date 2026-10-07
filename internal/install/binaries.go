package install

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/realpath"
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
	isDir, err := pathstat.IsDir(l.VersionsDir)
	if err != nil {
		return nil, err
	}
	if !isDir {
		return nil, nil
	}
	entries, err := os.ReadDir(l.VersionsDir)
	if err != nil {
		return nil, err
	}
	var versions []string
	for _, e := range entries {
		file, err := pathstat.IsFile(filepath.Join(l.VersionsDir, e.Name()))
		if err != nil {
			return nil, err
		}
		if !file {
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
// reports false when nothing is at the link's path (pathstat.Lexists); a
// link that cannot be checked or resolved is an error.
func (l Locator) SymlinkTarget() (string, bool, error) {
	there, err := pathstat.Lexists(l.ClaudeSymlink)
	if err != nil {
		return "", false, fmt.Errorf("cannot check the claude link %s: %w", l.ClaudeSymlink, err)
	}
	if !there {
		return "", false, nil
	}
	target, err := realpath.Resolve(l.ClaudeSymlink)
	if err != nil {
		return "", false, fmt.Errorf("cannot resolve the claude link %s: %w", l.ClaudeSymlink, err)
	}
	return target, true, nil
}

// EffectiveCLIVersion resolves the Claude Code version a launch runs: the
// version selection when set, else the base name of the claude link's
// target. It reports false when neither is available, and returns the error
// of a claude link that cannot be checked or resolved. Both the pre-launch
// model-version guard and the model picker's dimming read it, so they cannot
// disagree.
func EffectiveCLIVersion(selected string, l Locator) (string, bool, error) {
	if selected != "" {
		return selected, true, nil
	}
	target, ok, err := l.SymlinkTarget()
	if err != nil || !ok {
		return "", false, err
	}
	return filepath.Base(target), true, nil
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
	installed, err := pathstat.Exists(target)
	if err != nil {
		return "", err
	}
	if !installed {
		return "", fmt.Errorf("version %s is not installed at %s", version, target)
	}
	resolved, linked, err := l.SymlinkTarget()
	if err != nil {
		return "", err
	}
	if linked && filepath.Base(resolved) == version {
		return "", fmt.Errorf("refusing to uninstall %s: it is the current `claude` symlink target (%s); switch to another version first",
			version, l.ClaudeSymlink)
	}
	if err := fx.Remove(target); err != nil {
		return "", fmt.Errorf("failed to delete %s: %w", target, err)
	}
	return target, nil
}
