// Package workspace owns every claudewheel filesystem path: the workspace
// root ($HOME/.claudewheel) and what lies under it, the shared-store layout,
// and Claude Code's naming of the store directory that holds a project's
// sessions. It computes paths only; the one thing it reads is $HOME.
package workspace

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Workspace holds the root of claudewheel's data and the directories outside
// it that claudewheel writes to. Its methods derive every path from these.
type Workspace struct {
	root           string
	claudeDir      string
	binDir         string
	systemdUserDir string
}

// Default builds the workspace of the current user from $HOME. An unset,
// empty, or relative HOME is an error.
func Default() (Workspace, error) {
	home := os.Getenv("HOME")
	if home == "" {
		return Workspace{}, errors.New("HOME is not set: claudewheel keeps its data in $HOME/.claudewheel")
	}
	return FromHome(home)
}

// FromHome builds the workspace of a user whose home directory is home, an
// absolute path: root home/.claudewheel, Claude Code's default config
// directory home/.claude, the command link directory home/.local/bin, and
// the systemd user unit directory home/.config/systemd/user.
func FromHome(home string) (Workspace, error) {
	if !filepath.IsAbs(home) {
		return Workspace{}, fmt.Errorf("home directory %q is not an absolute path", home)
	}
	return Workspace{
		root:           filepath.Join(home, ".claudewheel"),
		claudeDir:      filepath.Join(home, ".claude"),
		binDir:         filepath.Join(home, ".local", "bin"),
		systemdUserDir: filepath.Join(home, ".config", "systemd", "user"),
	}, nil
}

// Root is claudewheel's data directory, $HOME/.claudewheel.
func (w Workspace) Root() string { return w.root }

// ClaudeDir is Claude Code's own config directory, the default profile.
func (w Workspace) ClaudeDir() string { return w.claudeDir }

// BinDir is where deploy-hooks links the commands it deploys.
func (w Workspace) BinDir() string { return w.binDir }

// SystemdUserDir is where deploy-hooks writes the probe runner's unit.
func (w Workspace) SystemdUserDir() string { return w.systemdUserDir }

// ProfilesDir holds one Claude Code config directory per profile.
func (w Workspace) ProfilesDir() string { return filepath.Join(w.root, "profiles") }

// OptionsFile is options.json.
func (w Workspace) OptionsFile() string { return filepath.Join(w.root, "options.json") }

// StateFile is state.json.
func (w Workspace) StateFile() string { return filepath.Join(w.root, "state.json") }

// ConfigFile is config.json.
func (w Workspace) ConfigFile() string { return filepath.Join(w.root, "config.json") }

// SegmentsFile is segments.json.
func (w Workspace) SegmentsFile() string { return filepath.Join(w.root, "segments.json") }

// ThemesDir holds the theme files.
func (w Workspace) ThemesDir() string { return filepath.Join(w.root, "themes") }

// HooksDir holds the user's pre-launch scripts.
func (w Workspace) HooksDir() string { return filepath.Join(w.root, "hooks") }

// ScriptsDir holds the scripts deploy-hooks deploys.
func (w Workspace) ScriptsDir() string { return filepath.Join(w.root, "scripts") }

// SharedDir is the shared store every profile links into.
func (w Workspace) SharedDir() string { return filepath.Join(w.root, "shared") }

// SkillsDir is the skills directory every profile links to.
func (w Workspace) SkillsDir() string { return filepath.Join(w.root, "skills") }

// SharedSettingsFile is shared-settings.json.
func (w Workspace) SharedSettingsFile() string {
	return filepath.Join(w.root, "shared-settings.json")
}

// InodesFile is the project inode map, shared/inodes.json.
func (w Workspace) InodesFile() string { return filepath.Join(w.SharedDir(), "inodes.json") }

// Shared is the shared-store layout of this workspace.
func (w Workspace) Shared() SharedStore {
	return SharedStore{sharedDir: w.SharedDir(), skillsDir: w.SkillsDir()}
}
