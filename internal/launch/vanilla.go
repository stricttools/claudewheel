package launch

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/hookscripts"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/reconcile"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// The default profile is Claude Code's own ~/.claude, which claudewheel
// leaves alone: the only write it ever makes to ~/.claude/settings.json is
// claudewheel's hook wiring, added when the user opts in and removed when
// the user opts out, never touching any other key or the user's own hooks.

// vanillaSettingsPath is ~/.claude/settings.json.
func vanillaSettingsPath(ws workspace.Workspace) string {
	return filepath.Join(ws.ClaudeDir(), profiles.SettingsFileName)
}

// loadVanillaSettings reads ~/.claude/settings.json; a missing file reads as
// an empty object and false.
func loadVanillaSettings(path string) (*jsonfile.Object, bool, error) {
	if _, err := os.Stat(path); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return jsonfile.NewObject(), false, nil
		}
		return nil, false, err
	}
	settings, err := profiles.LoadSettings(path)
	if err != nil {
		return nil, false, err
	}
	return settings, true, nil
}

// EnsureVanillaGuardrails merges claudewheel's canonical hook wiring into
// ~/.claude/settings.json, deploying any hook script it refers to that is
// missing first. Hooks already wired are left alone, and nothing is ever
// removed. It reports whether the file was written; a file already holding
// the wiring is not. A settings file that is not valid JSON, or whose hooks
// are not an object, is an error.
func EnsureVanillaGuardrails(fx *effects.FX, ws workspace.Workspace) (bool, error) {
	v, _ := guardrail.CanonicalSharedSettings(ws.ScriptsDir()).Get("hooks")
	canonical, ok := v.(*jsonfile.Object)
	if !ok {
		return false, errors.New("the canonical shared settings hold no hooks object")
	}
	missing, err := hookscripts.MissingScripts(reconcile.ReferencedScripts(canonical), ws.ScriptsDir())
	if err != nil {
		return false, err
	}
	if len(missing) > 0 {
		if _, err := hookscripts.DeployScripts(fx, missing, ws.ScriptsDir(), false); err != nil {
			return false, err
		}
	}

	path := vanillaSettingsPath(ws)
	settings, _, err := loadVanillaSettings(path)
	if err != nil {
		return false, err
	}
	before := jsonfile.Clone(settings)
	hooks := jsonfile.NewObject()
	if hv, present := settings.Get("hooks"); present {
		if hooks, ok = hv.(*jsonfile.Object); !ok {
			return false, fmt.Errorf("%s: hooks is not an object", path)
		}
	} else {
		settings.Set("hooks", hooks)
	}
	if _, err := reconcile.MergeHooks(hooks, canonical); err != nil {
		return false, fmt.Errorf("%s: %w", path, err)
	}
	if jsonfile.Equal(before, settings) {
		return false, nil
	}
	if err := fx.MkdirAll(ws.ClaudeDir()); err != nil {
		return false, err
	}
	return true, profiles.SaveSettings(fx, path, settings)
}

// RemoveVanillaGuardrails removes from ~/.claude/settings.json every hook
// whose command runs a script claudewheel deploys, by the script's file
// name, leaving the user's hooks and every other key as they are. An entry
// left with no hook is dropped, and so is an event left with no entry. It
// reports whether the file was written; a missing file, or one holding no
// claudewheel hook, is not. A settings file that is not valid JSON is an
// error.
func RemoveVanillaGuardrails(fx *effects.FX, ws workspace.Workspace) (bool, error) {
	path := vanillaSettingsPath(ws)
	settings, found, err := loadVanillaSettings(path)
	if err != nil || !found {
		return false, err
	}
	hv, _ := settings.Get("hooks")
	hooks, ok := hv.(*jsonfile.Object)
	if !ok {
		return false, nil
	}
	before := jsonfile.Clone(settings)
	for _, event := range hooks.Keys() {
		ev, _ := hooks.Get(event)
		entries, ok := ev.([]jsonfile.Value)
		if !ok {
			continue
		}
		var kept []jsonfile.Value
		for _, e := range entries {
			entry, ok := e.(*jsonfile.Object)
			if !ok {
				kept = append(kept, e)
				continue
			}
			lv, _ := entry.Get("hooks")
			list, ok := lv.([]jsonfile.Value)
			if !ok {
				kept = append(kept, e)
				continue
			}
			var own []jsonfile.Value
			for _, h := range list {
				if !isClaudewheelHook(h) {
					own = append(own, h)
				}
			}
			switch {
			case len(own) == len(list):
				kept = append(kept, e)
			case len(own) > 0:
				entry.Set("hooks", own)
				kept = append(kept, e)
			}
		}
		if len(kept) > 0 {
			hooks.Set(event, kept)
		} else {
			hooks.Delete(event)
		}
	}
	if jsonfile.Equal(before, settings) {
		return false, nil
	}
	return true, profiles.SaveSettings(fx, path, settings)
}

// isClaudewheelHook reports whether hook is an object whose command runs a
// script claudewheel deploys.
func isClaudewheelHook(hook jsonfile.Value) bool {
	h, ok := hook.(*jsonfile.Object)
	if !ok {
		return false
	}
	cv, _ := h.Get("command")
	command, ok := cv.(string)
	return ok && hookscripts.IsScript(reconcile.ScriptBasename(command))
}

// SetVanillaGuardrails adds (enable) or removes claudewheel's guardrail
// hooks on ~/.claude: what the bar's inspect page does when the user turns
// them on or off.
func SetVanillaGuardrails(fx *effects.FX, ws workspace.Workspace, enable bool) error {
	var err error
	if enable {
		_, err = EnsureVanillaGuardrails(fx, ws)
	} else {
		_, err = RemoveVanillaGuardrails(fx, ws)
	}
	return err
}
