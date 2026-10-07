// Package appconfig owns claudewheel's own files at the workspace root:
// config.json, segments.json, options.json, state.json, and the theme files,
// plus the defaults a new workspace starts with.
//
// Every file is decoded strictly (unknown keys, missing keys, and wrong types
// are errors) and written in the layout of Python's json.dumps(indent=2).
// Three entry points open a workspace:
//
//   - Load reads it and changes nothing; a missing file is an error naming
//     `claudewheel launch`, which creates the workspace.
//   - Ensure creates what is missing first: mutating commands use it.
//   - Upgrade converts a workspace an older claudewheel wrote, and is used
//     only by `claudewheel upgrade-workspace`.
//
// Load, Ensure, and every single-file reader refuse a file still holding a
// retired key or lacking a key the defaults declare, naming
// `claudewheel upgrade-workspace`.
//
// Resolving the "auto" theme needs the terminal, which this package never
// touches: see ResolveThemeName.
package appconfig

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// Config is config.json.
type Config struct {
	Theme               string   `json:"theme"`
	EnabledSegments     []string `json:"enabled_segments"`
	DefaultFlags        []string `json:"default_flags"`
	HealthCheckOnLaunch bool     `json:"health_check_on_launch"`
	Minimap             string   `json:"minimap"`
	DefaultClient       string   `json:"default_client"`
	ToolMemoryMax       string   `json:"tool_memory_max"`
	ToolMemorySwapMax   string   `json:"tool_memory_swap_max"`
	// Clients holds per-client settings; absent when none are set.
	Clients *ClientsConfig `json:"clients,omitempty"`
}

// ClientsConfig is config.json's clients section.
type ClientsConfig struct {
	Miniclaude *ClientConfig `json:"miniclaude,omitempty"`
}

// ClientConfig holds one client's settings. Binary, when set, is the
// client's executable, used instead of a PATH lookup.
type ClientConfig struct {
	Binary *string `json:"binary,omitempty"`
}

// Segment is one entry of segments.json: how the bar shows one segment.
// Freeform and Creatable are absent on the segments that do not declare them.
type Segment struct {
	Key         string `json:"key"`
	Label       string `json:"label"`
	ShowOptions bool   `json:"show_options"`
	Wrap        bool   `json:"wrap"`
	MinWidth    int    `json:"min_width"`
	MaxWidth    int    `json:"max_width"`
	Required    bool   `json:"required"`
	PrintMode   bool   `json:"print_mode"`
	Searchable  bool   `json:"searchable"`
	Freeform    *bool  `json:"freeform,omitempty"`
	TabAdvances bool   `json:"tab_advances"`
	Creatable   *bool  `json:"creatable,omitempty"`
}

// Store holds the four files of one workspace as they were read. Changes go
// through its methods or the single-file functions, which write the file
// and keep the Store's copy current.
type Store struct {
	ws       workspace.Workspace
	Config   Config
	Segments []Segment
	Options  Options
	State    State
}

// Workspace returns the workspace the store was opened on.
func (s *Store) Workspace() workspace.Workspace { return s.ws }

// Load reads the workspace's config, segments, options, and state files and
// changes nothing. A missing file is an error naming `claudewheel launch`; a
// file an older claudewheel wrote is an error naming
// `claudewheel upgrade-workspace`.
func Load(ws workspace.Workspace) (*Store, error) {
	return open(nil, ws)
}

// Ensure creates whatever a first run needs and then reads the workspace as
// Load does: the root, themes, and hooks directories, each of the four files
// and the dark and light theme files with their defaults, and
// shared-settings.json from the canonical guardrail settings. Existing files
// are never changed. Under --dry-run the creations are recorded and the
// store holds the defaults the missing files would have received.
func Ensure(fx *effects.FX, ws workspace.Workspace) (*Store, error) {
	for _, dir := range []string{ws.Root(), ws.ThemesDir(), ws.HooksDir()} {
		if err := fx.MkdirAll(dir); err != nil {
			return nil, err
		}
	}
	s, err := open(fx, ws)
	if err != nil {
		return nil, err
	}
	for _, name := range []string{ThemeDark, ThemeLight} {
		path, err := ThemeFile(ws, name)
		if err != nil {
			return nil, err
		}
		if err := createIfMissing(fx, path, DefaultTheme(name)); err != nil {
			return nil, err
		}
	}
	shared := ws.SharedSettingsFile()
	if err := createIfMissing(fx, shared, guardrail.CanonicalSharedSettings(ws.ScriptsDir())); err != nil {
		return nil, err
	}
	return s, nil
}

// open reads the four files. With fx, a missing file is created with its
// default; without, it is an error.
func open(fx *effects.FX, ws workspace.Workspace) (*Store, error) {
	s := &Store{ws: ws}
	var err error
	if s.Config, err = loadOrCreate(fx, ws.ConfigFile(), planConfig, DefaultConfig); err != nil {
		return nil, err
	}
	if s.Segments, err = loadOrCreate(fx, ws.SegmentsFile(), planSegments, DefaultSegments); err != nil {
		return nil, err
	}
	if s.Options, err = loadOrCreate(fx, ws.OptionsFile(), planOptions, DefaultOptions); err != nil {
		return nil, err
	}
	if s.State, err = loadOrCreate(fx, ws.StateFile(), planState, DefaultState); err != nil {
		return nil, err
	}
	return s, nil
}

// loadOrCreate reads the owned file at path into a T. When the file is
// missing and fx is set, it writes def() there and returns that value.
func loadOrCreate[T any](fx *effects.FX, path string, plan planner, def func() T) (T, error) {
	if fx != nil {
		exists, err := fileExists(path)
		if err != nil {
			var zero T
			return zero, err
		}
		if !exists {
			v := def()
			return v, writeJSON(fx, path, v)
		}
	}
	return readOwned[T](path, plan)
}

// createIfMissing writes v to path when nothing is there.
func createIfMissing(fx *effects.FX, path string, v any) error {
	exists, err := fileExists(path)
	if err != nil || exists {
		return err
	}
	return writeJSON(fx, path, v)
}

// writeJSON writes v to path atomically in the indent=2 layout.
func writeJSON(fx *effects.FX, path string, v any) error {
	data, err := jsonfile.MarshalIndented(v)
	if err != nil {
		return fmt.Errorf("encoding %s: %w", path, err)
	}
	return fx.WriteFileAtomic(path, data)
}

// ErrNotSetUp is wrapped by the error a missing owned file produces.
var ErrNotSetUp = errors.New("the claudewheel workspace is not set up")

// readOwnedFile reads an owned file. A missing one is an error wrapping both
// fs.ErrNotExist and ErrNotSetUp, naming the command that creates it.
func readOwnedFile(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, fmt.Errorf("%s does not exist: %w (`claudewheel launch` creates it): %w", path, ErrNotSetUp, err)
	}
	return data, err
}

// readOwned reads and strictly decodes the owned file at path, refusing it
// when plan finds anything Upgrade would change.
func readOwned[T any](path string, plan planner) (T, error) {
	var v T
	data, err := readOwnedFile(path)
	if err != nil {
		return v, err
	}
	tree, err := jsonfile.Decode(data)
	if err != nil {
		return v, fmt.Errorf("%s: %w", path, err)
	}
	if changes := plan(tree); len(changes) > 0 {
		return v, upgradeNeeded(path, changes)
	}
	if err := jsonfile.DecodeStrict(data, &v); err != nil {
		return v, fmt.Errorf("%s: %w", path, err)
	}
	return v, nil
}

// ErrUpgradeNeeded is wrapped by the error a file an older claudewheel wrote
// produces.
var ErrUpgradeNeeded = errors.New("the workspace was written by an older claudewheel")

func upgradeNeeded(path string, changes []Change) error {
	return fmt.Errorf("%s: %w and needs converting (%s): `claudewheel upgrade-workspace` converts it",
		path, ErrUpgradeNeeded, describeChanges(changes))
}

// fileName is the name a Change reports for path.
func fileName(ws workspace.Workspace, path string) string {
	rel, err := filepath.Rel(ws.Root(), path)
	if err != nil {
		return path
	}
	return rel
}
