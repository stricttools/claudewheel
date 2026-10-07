package wizard

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/reconcile"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// loadSharedSettings reads shared-settings.json. A missing file is the
// canonical shared settings, which is what the workspace setup writes there;
// a file that does not hold a JSON object is an error.
func loadSharedSettings(ws workspace.Workspace) (*jsonfile.Object, error) {
	path := ws.SharedSettingsFile()
	data, err := os.ReadFile(path)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return guardrail.CanonicalSharedSettings(ws.ScriptsDir()), nil
	}
	if err != nil {
		return nil, err
	}
	shared, err := jsonfile.DecodeObject(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return shared, nil
}

// readSettings reads a profile's settings.json as an ordered tree; a profile
// without one has empty settings.
func readSettings(path string) (*jsonfile.Object, error) {
	data, err := os.ReadFile(path)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		return jsonfile.NewObject(), nil
	}
	if err != nil {
		return nil, err
	}
	settings, err := jsonfile.DecodeObject(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return settings, nil
}

// objectAt returns the object under key in o, and false when the key is
// absent. A value that is not an object is an error naming where.
func objectAt(o *jsonfile.Object, key, where string) (*jsonfile.Object, bool, error) {
	v, ok := o.Get(key)
	if !ok {
		return nil, false, nil
	}
	obj, isObject := v.(*jsonfile.Object)
	if !isObject {
		return nil, false, fmt.Errorf("%s: %q is not a JSON object", where, key)
	}
	return obj, true, nil
}

// childObject returns the object under key in o, adding an empty one when
// the key is absent (Python's setdefault).
func childObject(o *jsonfile.Object, key, where string) (*jsonfile.Object, error) {
	obj, ok, err := objectAt(o, key, where)
	if err != nil {
		return nil, err
	}
	if !ok {
		obj = jsonfile.NewObject()
		o.Set(key, obj)
	}
	return obj, nil
}

// BuildSettings assembles a new profile's settings.json from the form's
// choices: the cloned profile's settings or shared-settings.json's
// profileDefaults, the checkbox overrides, the canonical profile settings,
// auto mode disabled, the managed tool list, and the canonical hooks when
// they are to be wired.
func BuildSettings(ws workspace.Workspace, choices Choices) (*jsonfile.Object, error) {
	shared, err := loadSharedSettings(ws)
	if err != nil {
		return nil, err
	}
	sharedWhere := ws.SharedSettingsFile()
	store := profiles.New(ws)

	settings := jsonfile.NewObject()
	if choices.CloneFrom != "" {
		source := filepath.Join(store.PathFor(choices.CloneFrom), profiles.SettingsFileName)
		if settings, err = readSettings(source); err != nil {
			return nil, err
		}
	} else {
		defaults, ok, err := objectAt(shared, "profileDefaults", sharedWhere)
		if err != nil {
			return nil, err
		}
		if ok {
			settings = jsonfile.Clone(defaults).(*jsonfile.Object)
		}
	}

	if choices.DisableRecap {
		settings.Set("awaySummaryEnabled", false)
	}
	if choices.Cleanup10y {
		settings.Set("cleanupPeriodDays", json.Number("3650"))
	}
	if choices.DisableMemory {
		settings.Set("autoMemoryEnabled", false)
	}
	if choices.DisableAttribution {
		attribution := jsonfile.NewObject()
		attribution.Set("commit", "")
		attribution.Set("pr", "")
		settings.Set("attribution", attribution)
	}

	// The canonical keys are set whatever the source: a clone never has them
	// otherwise, and the reconcile makes them exact on every managed profile,
	// so a new profile starts where a reconciled one ends up.
	canonical := guardrail.CanonicalProfileSettings()
	for _, key := range canonical.Keys() {
		v, _ := canonical.Get(key)
		settings.Set(key, v)
	}

	permissions, err := childObject(settings, "permissions", "the new settings")
	if err != nil {
		return nil, err
	}
	permissions.Set("disableAutoMode", "disable")

	// The tools claudewheel manages are recorded here; the launch enforces
	// them with --disallowedTools.
	managed, err := childObject(settings, "claudewheel", "the new settings")
	if err != nil {
		return nil, err
	}
	tools, ok := shared.Get("disallowedTools")
	if !ok {
		names := guardrail.DisallowedToolNames()
		list := make([]jsonfile.Value, len(names))
		for i, n := range names {
			list[i] = n
		}
		tools = list
	}
	managed.Set("disallowedTools", jsonfile.Clone(tools))

	if choices.WireHooks {
		canonicalHooks, ok, err := objectAt(shared, "hooks", sharedWhere)
		if err != nil {
			return nil, err
		}
		if !ok {
			canonicalHooks = jsonfile.NewObject()
		}
		canonicalHooks = jsonfile.Clone(canonicalHooks).(*jsonfile.Object)
		existing, ok, err := objectAt(settings, "hooks", "the new settings")
		if err != nil {
			return nil, err
		}
		if ok && existing.Len() > 0 {
			if _, err := reconcile.MergeHooks(existing, canonicalHooks); err != nil {
				return nil, err
			}
		} else {
			settings.Set("hooks", canonicalHooks)
		}
	}
	return settings, nil
}

// pyBool spells a boolean as the summary always has.
func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}

// CreateProfile writes the profile the form describes: its settings
// (BuildSettings), the onboarding flag, the shared-store links when chosen,
// and its pinned registration. It returns the summary lines; their verb is
// conditional under --dry-run, where the writes are recorded instead.
func CreateProfile(fx *effects.FX, ws workspace.Workspace, choices Choices) ([]string, error) {
	settings, err := BuildSettings(ws, choices)
	if err != nil {
		return nil, err
	}
	store := profiles.New(ws)
	if _, err := store.Create(fx, choices.Name, settings, profiles.CreateOptions{
		SetOnboarding: true,
		SymlinkShared: choices.SymlinkShared,
	}); err != nil {
		return nil, err
	}
	verb := "Created"
	if fx.Previewing() {
		verb = "Would create"
	}
	source := choices.CloneFrom
	if source == "" {
		source = "defaults"
	}
	return []string{
		fmt.Sprintf("%s profile '%s':", verb, choices.Name),
		"  Config dir:     " + store.PathFor(choices.Name),
		"  Settings from:  " + source,
		"  Hooks wired:    " + pyBool(choices.WireHooks),
		"  Shared symlinks:" + pyBool(choices.SymlinkShared),
		"  Recap disabled: " + pyBool(choices.DisableRecap),
		"  Cleanup 10y:    " + pyBool(choices.Cleanup10y),
		"  Auto-memory:    " + pyBool(!choices.DisableMemory),
		"  Attribution:    " + pyBool(!choices.DisableAttribution),
	}, nil
}

// setOnboardingFlag merges hasCompletedOnboarding into the profile's
// .claude.json, keeping what Claude Code wrote there. Claude Code asks for
// it in interactive mode and sets it on its own login success, which a
// stored token bypasses. A config directory that does not exist (a preview
// created nothing) is left alone; a .claude.json that is not a JSON object is
// an error.
func setOnboardingFlag(fx *effects.FX, configDir string) error {
	isDir, err := pathstat.IsDir(configDir)
	if err != nil {
		return err
	}
	if !isDir {
		return nil
	}
	path := filepath.Join(configDir, profiles.GlobalConfigName)
	global := jsonfile.NewObject()
	data, err := os.ReadFile(path)
	switch {
	case err == nil:
		if global, err = jsonfile.DecodeObject(data); err != nil {
			return fmt.Errorf("%s: %w", path, err)
		}
	case !errors.Is(err, fs.ErrNotExist):
		return err
	}
	global.Set("hasCompletedOnboarding", true)
	text, err := jsonfile.MarshalIndented(global)
	if err != nil {
		return err
	}
	return fx.WriteFileAtomic(path, text)
}
