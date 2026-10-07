package guardrail

import (
	"encoding/json"
	"path/filepath"
	"strconv"

	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// CanonicalProfileSettings returns the top-level settings.json keys made equal
// in every managed profile and in shared-settings.json's profileDefaults,
// built fresh. Reconcile writes them, health reports drift on them, and the
// wizard seeds them into a new profile.
//
//   - remoteControlAtStartup: Claude Code otherwise connects Remote Control at
//     startup and reports the attempt in every session.
//   - spinnerTipsEnabled: turns off the client's rotating tips.
//   - disableAgentView: disables the agent view, which moves a running session
//     into a daemon-managed background session on a left-arrow press at an
//     empty prompt; true in any settings scope wins.
//
// The wizard's checkbox keys (awaySummaryEnabled, cleanupPeriodDays,
// autoMemoryEnabled, and others) are not here: the user chooses them when
// creating a profile.
func CanonicalProfileSettings() *jsonfile.Object {
	o := jsonfile.NewObject()
	o.Set("remoteControlAtStartup", false)
	o.Set("spinnerTipsEnabled", false)
	o.Set("disableAgentView", true)
	return o
}

// CanonicalHookCommand returns the hook command for script under scriptsDir.
// Deployment and verification both use it, so they compare the same string.
func CanonicalHookCommand(scriptsDir, script string) string {
	return filepath.Join(scriptsDir, script)
}

// CanonicalHookEntry returns the settings.json hook object for w: type,
// command, then the options that are set, in HookOptionKeys order.
func CanonicalHookEntry(scriptsDir string, w HookWiring) *jsonfile.Object {
	o := jsonfile.NewObject()
	o.Set("type", "command")
	o.Set("command", CanonicalHookCommand(scriptsDir, w.Script))
	if w.Options.AsyncRewake {
		o.Set("asyncRewake", true)
	}
	if w.Options.Timeout != 0 {
		o.Set("timeout", json.Number(strconv.Itoa(w.Options.Timeout)))
	}
	if w.Options.RewakeMessage != "" {
		o.Set("rewakeMessage", w.Options.RewakeMessage)
	}
	if w.Options.RewakeSummary != "" {
		o.Set("rewakeSummary", w.Options.RewakeSummary)
	}
	return o
}

// canonicalHooks groups the expected wirings by event, in first-appearance
// order, and within an event by matcher, so scripts sharing an event and
// matcher sit in one entry's hooks list.
func canonicalHooks(scriptsDir string) *jsonfile.Object {
	hooks := jsonfile.NewObject()
	for _, w := range ExpectedHookWirings() {
		var entries []jsonfile.Value
		if v, ok := hooks.Get(w.Event); ok {
			entries = v.([]jsonfile.Value)
		}
		var entry *jsonfile.Object
		for _, e := range entries {
			eo := e.(*jsonfile.Object)
			if m, _ := eo.Get("matcher"); m == w.Matcher {
				entry = eo
				break
			}
		}
		if entry == nil {
			entry = jsonfile.NewObject()
			entry.Set("matcher", w.Matcher)
			entry.Set("hooks", []jsonfile.Value{})
			entries = append(entries, entry)
		}
		list, _ := entry.Get("hooks")
		entry.Set("hooks", append(list.([]jsonfile.Value), CanonicalHookEntry(scriptsDir, w)))
		hooks.Set(w.Event, entries)
	}
	return hooks
}

// CanonicalSharedSettings returns the canonical shared-settings.json tree,
// built fresh: the hooks from ExpectedHookWirings, the stripped tool names,
// and profileDefaults (the canonical profile settings, the wizard's default
// keys, and the deny and ask arrays).
func CanonicalSharedSettings(scriptsDir string) *jsonfile.Object {
	perms := jsonfile.NewObject()
	perms.Set("deny", jsonfile.StringArray(CanonicalDenyRules()))
	perms.Set("ask", jsonfile.StringArray(CanonicalAskRules()))
	perms.Set("defaultMode", "default")

	defaults := CanonicalProfileSettings()
	defaults.Set("awaySummaryEnabled", false)
	defaults.Set("cleanupPeriodDays", json.Number("3650"))
	defaults.Set("autoMemoryEnabled", false)
	defaults.Set("includeGitInstructions", false)
	defaults.Set("permissions", perms)

	shared := jsonfile.NewObject()
	shared.Set("hooks", canonicalHooks(scriptsDir))
	shared.Set("disallowedTools", jsonfile.StringArray(DisallowedToolNames()))
	shared.Set("profileDefaults", defaults)
	return shared
}
