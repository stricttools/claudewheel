package profiles

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"

	"github.com/stricttools/claudewheel/internal/effects"
)

// Claude Code's plugin tree inside a config directory, as observed:
// plugins/marketplaces/<marketplace>/ (the git clone, nearly all of the
// size), plugins/cache/<marketplace>/<plugin>/ (installed plugins), plus
// per-plugin data and index files. Removing it is an opt-in operation of its
// own, never part of the exact settings reconcile, which would otherwise
// delete deliberately installed plugin state on every run.
const (
	PluginsDirName      = "plugins"
	MarketplacesDirName = "marketplaces"
	PluginCacheDirName  = "cache"
)

// PluginInventory is what one profile's plugin tree holds, read without
// touching it.
type PluginInventory struct {
	ConfigDir string
	// Exists is false for a missing tree and for a plugins entry that is a
	// link, which points at data the profile does not own.
	Exists       bool
	Marketplaces []string
	Plugins      []string
	SizeBytes    int64
}

// Path is the plugin tree's path, whether or not it exists.
func (p PluginInventory) Path() string {
	return filepath.Join(p.ConfigDir, PluginsDirName)
}

// ownedPluginTree reports whether configDir holds a plugin tree of its own:
// a real directory, not a link.
func ownedPluginTree(configDir string) (bool, error) {
	info, err := os.Lstat(filepath.Join(configDir, PluginsDirName))
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return info.IsDir(), nil
}

// subdirNames returns the sorted names of dir's subdirectories (following
// links), none when dir is missing.
func subdirNames(dir string) ([]string, error) {
	entries, err := readDirIfExists(dir)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		ok, err := isDir(filepath.Join(dir, e.Name()))
		if err != nil {
			return nil, err
		}
		if ok {
			names = append(names, e.Name())
		}
	}
	return names, nil
}

// InventoryPlugins reads configDir's plugin tree, changing nothing.
func InventoryPlugins(configDir string) (PluginInventory, error) {
	inv := PluginInventory{ConfigDir: configDir}
	owned, err := ownedPluginTree(configDir)
	if err != nil || !owned {
		return inv, err
	}
	root := inv.Path()
	inv.Exists = true
	if inv.Marketplaces, err = subdirNames(filepath.Join(root, MarketplacesDirName)); err != nil {
		return PluginInventory{}, err
	}
	marketplaces, err := subdirNames(filepath.Join(root, PluginCacheDirName))
	if err != nil {
		return PluginInventory{}, err
	}
	installed := map[string]bool{}
	for _, m := range marketplaces {
		plugins, err := subdirNames(filepath.Join(root, PluginCacheDirName, m))
		if err != nil {
			return PluginInventory{}, err
		}
		for _, p := range plugins {
			installed[p] = true
		}
	}
	for p := range installed {
		inv.Plugins = append(inv.Plugins, p)
	}
	sort.Strings(inv.Plugins)
	if inv.SizeBytes, err = regularFilesSize(root); err != nil {
		return PluginInventory{}, err
	}
	return inv, nil
}

// regularFilesSize sums the regular files under root, following no link.
func regularFilesSize(root string) (int64, error) {
	var total int64
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.Type().IsRegular() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		total += info.Size()
		return nil
	})
	return total, err
}

// PurgePlugins removes configDir's plugin tree, reporting whether there was
// one. A plugins entry that is a link is left alone: following it would
// delete a tree belonging to whatever it points at.
func PurgePlugins(fx *effects.FX, configDir string) (bool, error) {
	owned, err := ownedPluginTree(configDir)
	if err != nil || !owned {
		return false, err
	}
	return true, fx.RemoveTree(filepath.Join(configDir, PluginsDirName))
}

// PluginTargets resolves the profile selection of purge-plugins: the
// profile named by profile, or every profile when all. The default profile
// is never a target: ~/.claude is Claude Code's own. Naming neither or both,
// a reserved or unknown name, and an empty set are errors.
func (s Store) PluginTargets(profile string, all bool) ([]Profile, error) {
	if (profile == "") == !all {
		return nil, errors.New("one of --profile or --all-profiles is required")
	}
	if reason, reserved := ReservedReason(profile); reserved {
		return nil, errors.New(reason)
	}
	discovered, err := s.Enumerate()
	if err != nil {
		return nil, err
	}
	var out []Profile
	for _, p := range discovered {
		if _, reserved := ReservedReason(p.Name); reserved {
			continue
		}
		if all || p.Name == profile {
			out = append(out, p)
		}
	}
	if len(out) == 0 {
		if all {
			return nil, errors.New("no profiles found")
		}
		return nil, fmt.Errorf("profile %q not found", profile)
	}
	return out, nil
}
