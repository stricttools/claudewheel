package health

import (
	"fmt"
	"path/filepath"
	"sort"
	"strings"

	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/hookscripts"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/pyrepr"
	"github.com/stricttools/claudewheel/internal/reconcile"
)

// The fix the drift checks name: the reconcile of every profile, or of one.
const reconcileAll = "claudewheel patch-profiles --all-profiles"

func reconcileOne(name string) string {
	return "claudewheel patch-profiles --profile " + name
}

// sharedSymlinks checks that each managed profile links every shared
// subdirectory, and skills when the skills store exists, to the shared store.
func (c *checker) sharedSymlinks() Result {
	const label = "shared-symlinks"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	managed := c.managed()
	if len(managed) == 0 {
		return ok(label, "no profiles found")
	}
	_, skillsStore, err := pathState(c.in.Workspace.SkillsDir())
	if err != nil {
		return failed(label, err)
	}
	var broken []string
	for _, p := range managed {
		entries, err := c.store.ClassifySharedDirs(p.Name)
		if err != nil {
			return failed(label, err)
		}
		// Anything not intact is broken here, missing included.
		for _, e := range entries {
			if e.Name == profiles.SkillsLinkName && !skillsStore {
				continue
			}
			if e.State != profiles.SharedIntact {
				broken = append(broken, p.Name+"/"+e.Name)
			}
		}
	}
	if len(broken) > 0 {
		return warn(label, "broken: "+strings.Join(broken, ", "))
	}
	return ok(label, fmt.Sprintf("all %d profiles OK", len(managed)))
}

// hookWired returns "" when hooks wires w, else what is wrong. An entry
// matches when its matcher (absent reads as "") equals the wiring's and it
// holds a hook whose command is the exact canonical command, so a hook under
// a stale scripts directory does not match; that hook must then carry every
// option key as the wiring states it, and none it does not state.
func hookWired(hooks jsonfile.Value, w guardrail.HookWiring, scriptsDir string) string {
	label := fmt.Sprintf("(%s, %s, %s)", w.Event, w.Matcher, w.Script)
	hooksObject, isObject := hooks.(*jsonfile.Object)
	if !isObject {
		return "missing " + label
	}
	v, _ := hooksObject.Get(w.Event)
	entries, _ := v.([]jsonfile.Value)
	expected := guardrail.CanonicalHookEntry(scriptsDir, w)
	expectedCmd, _ := expected.Get("command")
	for _, ev := range entries {
		entry, isObject := ev.(*jsonfile.Object)
		if !isObject {
			continue
		}
		matcher, present := entry.Get("matcher")
		if !present {
			matcher = ""
		}
		if m, isString := matcher.(string); !isString || m != w.Matcher {
			continue
		}
		hv, _ := entry.Get("hooks")
		list, _ := hv.([]jsonfile.Value)
		for _, item := range list {
			h, isObject := item.(*jsonfile.Object)
			if !isObject {
				continue
			}
			cmd, _ := h.Get("command")
			if s, isString := cmd.(string); !isString || s != expectedCmd {
				continue
			}
			var wrong []string
			for _, key := range guardrail.HookOptionKeys() {
				have, _ := h.Get(key)
				want, _ := expected.Get(key)
				if !jsonfile.Equal(have, want) {
					wrong = append(wrong, key)
				}
			}
			if len(wrong) > 0 {
				return fmt.Sprintf("options differ on %s: %s", label, strings.Join(wrong, ", "))
			}
			return ""
		}
	}
	return "missing " + label
}

// hooksWired checks that each managed profile wires every expected hook.
func (c *checker) hooksWired() Result {
	const label = "hooks-wired"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	managed := c.managed()
	if len(managed) == 0 {
		return ok(label, "no profiles found")
	}
	scriptsDir := c.in.Workspace.ScriptsDir()
	var missing []string
	for _, p := range managed {
		settings, found, err := readObject(settingsPath(p))
		if err != nil {
			missing = append(missing, p.Name+": unreadable settings.json")
			continue
		}
		if !found {
			missing = append(missing, p.Name+": no settings.json")
			continue
		}
		hooks, _ := settings.Get("hooks")
		for _, w := range guardrail.ExpectedHookWirings() {
			if problem := hookWired(hooks, w, scriptsDir); problem != "" {
				missing = append(missing, p.Name+": "+problem)
			}
		}
	}
	if len(missing) > 0 {
		return warn(label, strings.Join(missing, "; ")+" -- run '"+reconcileAll+"' to sync")
	}
	return ok(label, fmt.Sprintf("all %d profiles OK", len(managed)))
}

// settingsDefaults checks the settings every managed profile must hold.
func (c *checker) settingsDefaults() Result {
	const label = "settings-defaults"
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	managed := c.managed()
	if len(managed) == 0 {
		return ok(label, "no profiles found")
	}
	canonical := guardrail.CanonicalProfileSettings()
	var issues []string
	for _, p := range managed {
		s, found, err := readObject(settingsPath(p))
		if err != nil {
			issues = append(issues, p.Name+": unreadable settings.json")
			continue
		}
		if !found {
			issues = append(issues, p.Name+": no settings.json")
			continue
		}
		fix := reconcileOne(p.Name)
		if v, _ := s.Get("awaySummaryEnabled"); v != false {
			issues = append(issues, p.Name+": awaySummaryEnabled != false")
		}
		cpd, _ := s.Get("cleanupPeriodDays")
		if days, isNumber := numberValue(cpd); !isNumber || days < 365 {
			issues = append(issues, fmt.Sprintf("%s: cleanupPeriodDays < 365 (%s)", p.Name, pyrepr.Repr(cpd)))
		}
		if v, _ := s.Get("autoMemoryEnabled"); v != false {
			issues = append(issues, p.Name+": autoMemoryEnabled != false")
		}
		for _, key := range canonical.Keys() {
			want, _ := canonical.Get(key)
			have, present := s.Get(key)
			if !present || !jsonfile.Equal(have, want) {
				issues = append(issues, fmt.Sprintf("%s: %s != %s (run '%s')", p.Name, key, pyrepr.Repr(want), fix))
			}
		}
		if v, _ := objectOr(s, "permissions").Get("disableAutoMode"); v != "disable" {
			issues = append(issues, p.Name+": auto mode not disabled")
		}
		current := map[string]bool{}
		dv, _ := objectOr(s, "claudewheel").Get("disallowedTools")
		list, _ := dv.([]jsonfile.Value)
		for _, item := range list {
			if name, isString := item.(string); isString {
				current[name] = true
			}
		}
		var missingTools []string
		for _, name := range guardrail.DisallowedToolNames() {
			if !current[name] {
				missingTools = append(missingTools, name)
			}
		}
		sort.Strings(missingTools)
		if len(missingTools) > 0 {
			issues = append(issues, fmt.Sprintf("%s: missing disallowedTools: %s (run '%s')", p.Name, strings.Join(missingTools, ", "), fix))
		}
		if _, present := s.Get("disallowedTools"); present {
			issues = append(issues, fmt.Sprintf("%s: has inert top-level disallowedTools key (run '%s')", p.Name, fix))
		}
	}
	if len(issues) > 0 {
		return warn(label, strings.Join(issues, "; "))
	}
	return ok(label, fmt.Sprintf("all %d profiles OK", len(managed)))
}

// containsEqual reports whether list holds a value equal to v.
func containsEqual(list []jsonfile.Value, v jsonfile.Value) bool {
	for _, item := range list {
		if jsonfile.Equal(item, v) {
			return true
		}
	}
	return false
}

// allStrings reports whether every item of every list is a string.
func allStrings(lists ...[]jsonfile.Value) bool {
	for _, list := range lists {
		for _, item := range list {
			if _, isString := item.(string); !isString {
				return false
			}
		}
	}
	return true
}

// sameStringSet reports whether two lists of strings hold the same set.
func sameStringSet(a, b []jsonfile.Value) bool {
	setA, setB := map[string]bool{}, map[string]bool{}
	for _, item := range a {
		setA[item.(string)] = true
	}
	for _, item := range b {
		setB[item.(string)] = true
	}
	if len(setA) != len(setB) {
		return false
	}
	for s := range setA {
		if !setB[s] {
			return false
		}
	}
	return true
}

// diffJSON describes how actual differs from canonical: objects key by key
// in sorted key order, lists by their missing and extra items (lists of
// strings compared as sets, other lists in order), and anything else as an
// expected and an actual value.
func diffJSON(label string, canonical, actual jsonfile.Value) []string {
	var diffs []string
	co, canonicalIsObject := canonical.(*jsonfile.Object)
	ao, actualIsObject := actual.(*jsonfile.Object)
	if canonicalIsObject && actualIsObject {
		keySet := map[string]bool{}
		for _, k := range co.Keys() {
			keySet[k] = true
		}
		for _, k := range ao.Keys() {
			keySet[k] = true
		}
		keys := make([]string, 0, len(keySet))
		for k := range keySet {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, key := range keys {
			cv, inCanonical := co.Get(key)
			av, inActual := ao.Get(key)
			switch {
			case !inActual:
				diffs = append(diffs, fmt.Sprintf("%s.%s: missing (expected %s)", label, key, dumps(cv)))
			case !inCanonical:
				diffs = append(diffs, fmt.Sprintf("%s.%s: extra (unexpected)", label, key))
			case !jsonfile.Equal(cv, av):
				diffs = append(diffs, diffJSON(label+"."+key, cv, av)...)
			}
		}
		return diffs
	}
	cl, canonicalIsList := canonical.([]jsonfile.Value)
	al, actualIsList := actual.([]jsonfile.Value)
	if canonicalIsList && actualIsList {
		var differ bool
		if allStrings(cl, al) {
			differ = !sameStringSet(cl, al)
		} else {
			differ = !jsonfile.Equal(cl, al)
		}
		if !differ {
			return nil
		}
		var missing, extra []jsonfile.Value
		for _, x := range cl {
			if !containsEqual(al, x) {
				missing = append(missing, x)
			}
		}
		for _, x := range al {
			if !containsEqual(cl, x) {
				extra = append(extra, x)
			}
		}
		if len(missing) > 0 {
			diffs = append(diffs, fmt.Sprintf("%s: missing %s", label, pyrepr.Repr(missing)))
		}
		if len(extra) > 0 {
			diffs = append(diffs, fmt.Sprintf("%s: extra %s", label, pyrepr.Repr(extra)))
		}
		return diffs
	}
	return []string{fmt.Sprintf("%s: expected %s, got %s", label, dumps(canonical), dumps(actual))}
}

// sharedSettingsDrift compares each managed profile's hooks and
// disallowedTools with shared-settings.json's.
func (c *checker) sharedSettingsDrift() Result {
	const label = "settings-drift"
	shared, found, err := readObject(c.in.Workspace.SharedSettingsFile())
	if err != nil {
		return warn(label, fmt.Sprintf("unreadable shared-settings.json: %v", err))
	}
	if !found {
		return ok(label, "shared-settings.json not found (will be created on next launch)")
	}
	sharedHooks := valueOr(shared, "hooks", jsonfile.NewObject())
	sharedDisallowed := valueOr(shared, "disallowedTools", []jsonfile.Value{})

	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	managed := c.managed()
	if len(managed) == 0 {
		return ok(label, "no profiles found")
	}
	var all []string
	for _, p := range managed {
		settings, found, err := readObject(settingsPath(p))
		if err != nil {
			all = append(all, p.Name+": unreadable settings.json")
			continue
		}
		if !found {
			all = append(all, p.Name+": no settings.json")
			continue
		}
		for _, d := range diffJSON("hooks", sharedHooks, valueOr(settings, "hooks", jsonfile.NewObject())) {
			all = append(all, p.Name+": "+d)
		}
		disallowed := valueOr(objectOr(settings, "claudewheel"), "disallowedTools", []jsonfile.Value{})
		for _, d := range diffJSON("disallowedTools", sharedDisallowed, disallowed) {
			all = append(all, p.Name+": "+d)
		}
	}
	if len(all) > 0 {
		return warn(label, strings.Join(all, "; "))
	}
	return ok(label, fmt.Sprintf("all %d profiles in sync", len(managed)))
}

// canonicalPermissionDiffs compares a permissions block with the canonical
// deny and ask rules and lists the allow entries that are allow conflicts.
func canonicalPermissionDiffs(label string, perms jsonfile.Value) []string {
	block, isObject := perms.(*jsonfile.Object)
	if !isObject {
		block = jsonfile.NewObject()
	}
	var diffs []string
	diffs = append(diffs, diffJSON(label+".deny", stringValues(guardrail.CanonicalDenyRules()), valueOr(block, "deny", []jsonfile.Value{}))...)
	diffs = append(diffs, diffJSON(label+".ask", stringValues(guardrail.CanonicalAskRules()), valueOr(block, "ask", []jsonfile.Value{}))...)
	allow, _ := block.Get("allow")
	list, _ := allow.([]jsonfile.Value)
	var conflicting []string
	for _, item := range list {
		if s, isString := item.(string); isString && guardrail.IsAllowConflict(s) {
			conflicting = append(conflicting, s)
		}
	}
	if len(conflicting) > 0 {
		diffs = append(diffs, fmt.Sprintf("%s.allow: dead/conflicting %s", label, pyrepr.StringList(conflicting)))
	}
	return diffs
}

// canonicalPermissionsDrift compares the permissions of every managed
// profile and of shared-settings.json's profileDefaults (which seeds new
// profiles) with the canonical guardrail model.
func (c *checker) canonicalPermissionsDrift() Result {
	const label = "canonical-drift"
	var all []string
	shared, found, err := readObject(c.in.Workspace.SharedSettingsFile())
	switch {
	case err != nil:
		all = append(all, fmt.Sprintf("profileDefaults: unreadable shared-settings.json: %v", err))
	case found:
		perms, _ := objectOr(shared, "profileDefaults").Get("permissions")
		for _, d := range canonicalPermissionDiffs("permissions", perms) {
			all = append(all, "profileDefaults: "+d)
		}
	}
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	managed := c.managed()
	for _, p := range managed {
		settings, found, err := readObject(settingsPath(p))
		if err != nil {
			all = append(all, p.Name+": unreadable settings.json")
			continue
		}
		if !found {
			all = append(all, p.Name+": no settings.json")
			continue
		}
		perms, _ := settings.Get("permissions")
		for _, d := range canonicalPermissionDiffs("permissions", perms) {
			all = append(all, p.Name+": "+d)
		}
	}
	if len(all) > 0 {
		return warn(label, strings.Join(all, "; "))
	}
	return ok(label, fmt.Sprintf("%d profiles + profileDefaults match canonical", len(managed)))
}

// staleHookCommandPaths returns the commands in hooks that run a script
// claudewheel deploys from a directory other than scriptsDir, as a
// workspace relocation leaves them. Commands running other scripts are
// ignored.
func staleHookCommandPaths(hooks jsonfile.Value, scriptsDir string) []string {
	hooksObject, isObject := hooks.(*jsonfile.Object)
	if !isObject {
		return nil
	}
	var stale []string
	for _, event := range hooksObject.Keys() {
		v, _ := hooksObject.Get(event)
		entries, _ := v.([]jsonfile.Value)
		for _, ev := range entries {
			entry, isObject := ev.(*jsonfile.Object)
			if !isObject {
				continue
			}
			hv, _ := entry.Get("hooks")
			list, _ := hv.([]jsonfile.Value)
			for _, item := range list {
				h, isObject := item.(*jsonfile.Object)
				if !isObject {
					continue
				}
				cmdValue, _ := h.Get("command")
				cmd, _ := cmdValue.(string)
				if cmd == "" {
					continue
				}
				name := reconcile.ScriptBasename(cmd)
				if hookscripts.IsScript(name) && filepath.Dir(filepath.Clean(cmd)) != scriptsDir {
					stale = append(stale, cmd)
				}
			}
		}
	}
	return stale
}

// relocatedHookPaths flags claudewheel hook commands in shared-settings.json
// and the managed profiles that point at a scripts directory other than the
// current one. Files that are missing or unreadable are skipped here; the
// other checks report them.
func (c *checker) relocatedHookPaths() Result {
	const label = "hook-path-drift"
	scriptsDir := c.in.Workspace.ScriptsDir()
	var issues []string
	if shared, found, err := readObject(c.in.Workspace.SharedSettingsFile()); err == nil && found {
		hooks, _ := shared.Get("hooks")
		for _, cmd := range staleHookCommandPaths(hooks, scriptsDir) {
			issues = append(issues, "shared-settings.json: "+cmd)
		}
	}
	if c.enumErr != nil {
		return failed(label, c.enumErr)
	}
	for _, p := range c.managed() {
		settings, found, err := readObject(settingsPath(p))
		if err != nil || !found {
			continue
		}
		hooks, _ := settings.Get("hooks")
		for _, cmd := range staleHookCommandPaths(hooks, scriptsDir) {
			issues = append(issues, p.Name+": "+cmd)
		}
	}
	if len(issues) > 0 {
		return warn(label, fmt.Sprintf("%s -- hook commands should live under %s; run '%s' to fix", strings.Join(issues, "; "), scriptsDir, reconcileAll))
	}
	return ok(label, "all hook commands under current scripts dir")
}

// deployedHookDrift compares every deployed script with the text deploy-hooks
// writes. A script not deployed is not drift.
func (c *checker) deployedHookDrift() Result {
	const label = "hook-drift"
	scriptsDir := c.in.Workspace.ScriptsDir()
	_, isDir, err := pathState(scriptsDir)
	if err != nil {
		return failed(label, err)
	}
	if !isDir {
		return ok(label, "no scripts dir (hooks not deployed)")
	}
	states, err := hookscripts.CheckDeployed(scriptsDir)
	if err != nil {
		return failed(label, err)
	}
	var drifted []string
	checked := 0
	for _, s := range states {
		switch s.State {
		case hookscripts.DeployedAbsent:
			continue
		case hookscripts.DeployedDiffers:
			drifted = append(drifted, s.Name)
		case hookscripts.DeployedUnreadable:
			drifted = append(drifted, fmt.Sprintf("%s: unreadable (%v)", s.Name, s.Err))
		}
		checked++
	}
	if len(drifted) > 0 {
		return warn(label, "deployed scripts differ from model: "+strings.Join(drifted, ", ")+
			" -- run 'claudewheel deploy-hooks <name> --force-overwrite'")
	}
	if checked == 0 {
		return ok(label, "no model hook scripts deployed")
	}
	return ok(label, fmt.Sprintf("all %d deployed hook scripts match model", checked))
}
