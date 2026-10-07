package appconfig

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"syscall"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// State is state.json. The optional keys are absent until first written.
//
// AuthBrowser, ProjectHookApprovals, VanillaGuardrailsOptIn, and
// ScratchpadDismissed are written out of band, straight to the file, by
// code that does not hold the interface's copy of the state; SaveState lets
// their copy on disk win over the one in memory.
type State struct {
	// LastConfig holds the last launch's selections, keyed by segment key.
	LastConfig map[string]string `json:"last_config"`
	// RecentDirs holds the launched directories, most recent first.
	RecentDirs  []string `json:"recent_dirs"`
	LaunchCount int      `json:"launch_count"`
	// NpmVersionsCache and ModelListCache cache the discoveries that need
	// the network.
	NpmVersionsCache *NpmVersionsCache `json:"npm_versions_cache,omitempty"`
	ModelListCache   *ModelListCache   `json:"model_list_cache,omitempty"`
	// AuthBrowser is the browser chosen in the auth wizard: a browser
	// executable's path, or "copy".
	AuthBrowser *string `json:"auth_browser,omitempty"`
	// ProjectHookApprovals maps a project key (see ProjectKey) to the
	// fingerprint of that project's hooks the user approved.
	ProjectHookApprovals *map[string]string `json:"project_hook_approvals,omitempty"`
	// VanillaGuardrailsOptIn records whether the user opted the default
	// profile (~/.claude) into claudewheel's guardrail hooks; absent until
	// the choice is made. It is one machine-wide choice, not per project.
	VanillaGuardrailsOptIn *bool `json:"vanilla_guardrails_opt_in,omitempty"`
	// ScratchpadDismissed lists the per-project scratchpad directories the
	// scratchpad-cleanup preflight step never offers again: the absolute
	// paths of the immediate subdirectories of /tmp/claude-<uid> the user
	// declined to delete, in the order they were declined.
	ScratchpadDismissed *[]string `json:"scratchpad_dismissed,omitempty"`
}

// NpmVersionsCache caches the Claude Code versions npm lists. FetchedAt is
// the fetch time in Unix seconds.
type NpmVersionsCache struct {
	FetchedAt float64  `json:"fetched_at"`
	Versions  []string `json:"versions"`
}

// ModelListCache caches the models the Anthropic models endpoint lists.
// FetchedAt is the fetch time in Unix seconds.
type ModelListCache struct {
	FetchedAt float64       `json:"fetched_at"`
	Models    []CachedModel `json:"models"`
}

// CachedModel is one listed model; CreatedAt is absent when the endpoint
// gave no release date.
type CachedModel struct {
	ID        string  `json:"id"`
	CreatedAt *string `json:"created_at,omitempty"`
}

// RecentDirsLimit is how many directories State.RecentDirs keeps.
const RecentDirsLimit = 20

// ReadState reads state.json fresh from disk.
func ReadState(ws workspace.Workspace) (State, error) {
	return readOwned[State](ws.StateFile(), planState)
}

// UpdateState reads state.json fresh, applies change, and writes it back
// whole. Use it for a key written out of band, so a stale in-memory copy of
// the other keys is never written.
func UpdateState(fx *effects.FX, ws workspace.Workspace, change func(*State) error) error {
	st, err := ReadState(ws)
	if err != nil {
		return err
	}
	if err := change(&st); err != nil {
		return err
	}
	return writeJSON(fx, ws.StateFile(), st)
}

// SaveState writes the store's state to state.json, first taking each
// out-of-band key (see State) that is set on disk from the disk copy, so a
// value written there while this store held its copy is kept. A missing
// state.json is simply written.
func (s *Store) SaveState(fx *effects.FX) error {
	exists, err := fileExists(s.ws.StateFile())
	if err != nil {
		return err
	}
	if exists {
		disk, err := ReadState(s.ws)
		if err != nil {
			return err
		}
		if disk.AuthBrowser != nil {
			s.State.AuthBrowser = disk.AuthBrowser
		}
		if disk.ProjectHookApprovals != nil {
			s.State.ProjectHookApprovals = disk.ProjectHookApprovals
		}
		if disk.VanillaGuardrailsOptIn != nil {
			s.State.VanillaGuardrailsOptIn = disk.VanillaGuardrailsOptIn
		}
		if disk.ScratchpadDismissed != nil {
			s.State.ScratchpadDismissed = disk.ScratchpadDismissed
		}
	}
	return writeJSON(fx, s.ws.StateFile(), s.State)
}

// RecordLaunch records a launch's selections in the store's state and saves
// it: the selections become last_config, the launch count goes up by one,
// and a selected directory moves to the front of the recent directories,
// which keep RecentDirsLimit entries. Pass only the segments that have a
// selection.
func (s *Store) RecordLaunch(fx *effects.FX, selections map[string]string) error {
	last := make(map[string]string, len(selections))
	for k, v := range selections {
		last[k] = v
	}
	s.State.LastConfig = last
	s.State.LaunchCount++
	if dir := selections["directory"]; dir != "" {
		recent := slices.DeleteFunc(slices.Clone(s.State.RecentDirs), func(d string) bool { return d == dir })
		recent = append([]string{dir}, recent...)
		if len(recent) > RecentDirsLimit {
			recent = recent[:RecentDirsLimit]
		}
		s.State.RecentDirs = recent
	}
	return s.SaveState(fx)
}

// ProjectKey returns the key per-project state is stored under for dir: its
// absolute path with every symbolic link resolved, so a link and its target
// share one entry. dir must exist.
func ProjectKey(dir string) (string, error) {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(abs)
}

// ProjectHookApproval returns the approved hooks fingerprint recorded for
// the project in dir, read fresh from disk, and whether there is one.
func ProjectHookApproval(ws workspace.Workspace, dir string) (string, bool, error) {
	key, err := ProjectKey(dir)
	if err != nil {
		return "", false, err
	}
	st, err := ReadState(ws)
	if err != nil {
		return "", false, err
	}
	if st.ProjectHookApprovals == nil {
		return "", false, nil
	}
	fp, ok := (*st.ProjectHookApprovals)[key]
	return fp, ok, nil
}

// SetProjectHookApproval records fingerprint as the approved hooks of the
// project in dir.
func SetProjectHookApproval(fx *effects.FX, ws workspace.Workspace, dir, fingerprint string) error {
	key, err := ProjectKey(dir)
	if err != nil {
		return err
	}
	return UpdateState(fx, ws, func(st *State) error {
		if st.ProjectHookApprovals == nil {
			st.ProjectHookApprovals = &map[string]string{}
		}
		(*st.ProjectHookApprovals)[key] = fingerprint
		return nil
	})
}

// SetAuthBrowser records the browser chosen in the auth wizard.
func SetAuthBrowser(fx *effects.FX, ws workspace.Workspace, browser string) error {
	return UpdateState(fx, ws, func(st *State) error {
		st.AuthBrowser = &browser
		return nil
	})
}

// SetVanillaGuardrailsOptIn records the default profile's guardrail choice.
func SetVanillaGuardrailsOptIn(fx *effects.FX, ws workspace.Workspace, optIn bool) error {
	return UpdateState(fx, ws, func(st *State) error {
		st.VanillaGuardrailsOptIn = &optIn
		return nil
	})
}

// DismissedScratchpadDirs returns the scratchpad directories dismissed for
// good, read fresh from disk.
func DismissedScratchpadDirs(ws workspace.Workspace) ([]string, error) {
	st, err := ReadState(ws)
	if err != nil {
		return nil, err
	}
	if st.ScratchpadDismissed == nil {
		return []string{}, nil
	}
	return *st.ScratchpadDismissed, nil
}

// DismissScratchpadDirs adds the absolute directory paths in dirs to the
// dismissed scratchpad directories, skipping those already listed.
func DismissScratchpadDirs(fx *effects.FX, ws workspace.Workspace, dirs []string) error {
	for _, d := range dirs {
		if !filepath.IsAbs(d) {
			return fmt.Errorf("scratchpad directory %q is not an absolute path", d)
		}
	}
	return UpdateState(fx, ws, func(st *State) error {
		list := []string{}
		if st.ScratchpadDismissed != nil {
			list = *st.ScratchpadDismissed
		}
		for _, d := range dirs {
			if !slices.Contains(list, d) {
				list = append(list, d)
			}
		}
		st.ScratchpadDismissed = &list
		return nil
	})
}

// RecordInode records the inode of the project directory dir in the shared
// store's inodes.json, keyed by dir's absolute path, so a later rename of
// the directory can be recognized. The file is written only when the entry
// changes.
func RecordInode(fx *effects.FX, shared workspace.SharedStore, dir string) error {
	path, err := filepath.Abs(dir)
	if err != nil {
		return err
	}
	info, err := os.Stat(path)
	if err != nil {
		return err
	}
	sys, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fmt.Errorf("%s: the file system reports no inode", path)
	}
	inode := sys.Ino

	inodeText := json.Number(strconv.FormatUint(inode, 10))

	// The map is kept as an ordered tree, so entries keep their order.
	file := shared.InodesFile()
	inodes := jsonfile.NewObject()
	data, err := os.ReadFile(file)
	switch {
	case err == nil:
		if inodes, err = jsonfile.DecodeObject(data); err != nil {
			return fmt.Errorf("%s: %w", file, err)
		}
	case !errors.Is(err, os.ErrNotExist):
		return err
	}
	if old, ok := inodes.Get(path); ok && jsonfile.Equal(old, inodeText) {
		return nil
	}
	inodes.Set(path, inodeText)
	if err := fx.MkdirAll(filepath.Dir(file)); err != nil {
		return err
	}
	return writeJSON(fx, file, inodes)
}
