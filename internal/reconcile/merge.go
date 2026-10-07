package reconcile

import (
	"fmt"
	"path/filepath"

	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// ScriptBasename returns the script name at the end of a hook command path,
// "" for an empty command or one with no final name. Health uses it too, so
// both name a hook's script the same way.
func ScriptBasename(command string) string {
	if command == "" {
		return ""
	}
	base := filepath.Base(filepath.Clean(command))
	if base == "/" || base == "." {
		return ""
	}
	return base
}

// stringField returns o[key] when it is a string, "" otherwise.
func stringField(o *jsonfile.Object, key string) string {
	v, _ := o.Get(key)
	s, _ := v.(string)
	return s
}

// matcherOf returns an entry's matcher, "" when absent, and false when it is
// present but not a string (such an entry matches no wiring).
func matcherOf(entry *jsonfile.Object) (string, bool) {
	v, present := entry.Get("matcher")
	if !present {
		return "", true
	}
	s, ok := v.(string)
	return s, ok
}

// ReferencedScripts returns the script names the hook commands in hooks
// refer to, in first-appearance order, each once.
func ReferencedScripts(hooks *jsonfile.Object) []string {
	var names []string
	seen := map[string]bool{}
	for _, event := range hooks.Keys() {
		v, _ := hooks.Get(event)
		entries, ok := v.([]jsonfile.Value)
		if !ok {
			continue
		}
		for _, e := range entries {
			entry, ok := e.(*jsonfile.Object)
			if !ok {
				continue
			}
			hv, _ := entry.Get("hooks")
			list, _ := hv.([]jsonfile.Value)
			for _, h := range list {
				ho, ok := h.(*jsonfile.Object)
				if !ok {
					continue
				}
				base := ScriptBasename(stringField(ho, "command"))
				if base != "" && !seen[base] {
					seen[base] = true
					names = append(names, base)
				}
			}
		}
	}
	return names
}

// MergeHooks merges the canonical hooks into existing, in place, for
// building a new profile's hooks (the wizard) and for the opt-in wiring of
// the default profile; existing profiles are made exactly canonical by Run
// instead. Canonical entries are matched to existing ones by matcher, and
// hooks within an entry by script name:
//
//   - a canonical entry with no existing entry of its matcher is appended;
//   - a canonical hook whose script is absent from the matched entry is
//     appended;
//   - a hook already wiring the script is repathed to the canonical command
//     when it points elsewhere (a relocated workspace), and given the
//     canonical options.
//
// Hooks that are not canonical are matched by neither and kept as they are.
// It returns a description of every hook added, repathed, or given options.
// An event value or a matched entry's hooks that is not an array is a
// *MalformedSettingsError, and so is a canonical entry or hook that is not an
// object (the wizard's canonical hooks come from the hand-editable
// shared-settings.json); canonical is checked before existing is changed.
func MergeHooks(existing, canonical *jsonfile.Object) ([]string, error) {
	if err := checkCanonicalHooks(canonical); err != nil {
		return nil, err
	}
	var added []string
	for _, event := range canonical.Keys() {
		cv, _ := canonical.Get(event)
		canonicalEntries, _ := cv.([]jsonfile.Value)
		existingEntries, err := listAt(existing, event)
		if err != nil {
			return nil, err
		}
		for _, ce := range canonicalEntries {
			cEntry := ce.(*jsonfile.Object)
			matcher := stringField(cEntry, "matcher")
			chv, _ := cEntry.Get("hooks")
			cHooks, _ := chv.([]jsonfile.Value)
			label := matcher
			if label == "" {
				label = "*"
			}
			var target *jsonfile.Object
			for _, e := range existingEntries {
				eo, ok := e.(*jsonfile.Object)
				if !ok {
					continue
				}
				if m, ok := matcherOf(eo); ok && m == matcher {
					target = eo
					break
				}
			}
			if target == nil {
				existingEntries = append(existingEntries, jsonfile.Clone(cEntry))
				existing.Set(event, existingEntries)
				for _, h := range cHooks {
					added = append(added, fmt.Sprintf("%s[%s] %s", event, label, ScriptBasename(stringField(h.(*jsonfile.Object), "command"))))
				}
				continue
			}
			targetHooks, err := listAt(target, "hooks")
			if err != nil {
				return nil, err
			}
			for _, hv := range cHooks {
				h := hv.(*jsonfile.Object)
				canonicalCmd := stringField(h, "command")
				base := ScriptBasename(canonicalCmd)
				if base == "" {
					continue
				}
				var matches []*jsonfile.Object
				for _, tv := range targetHooks {
					th, ok := tv.(*jsonfile.Object)
					if ok && ScriptBasename(stringField(th, "command")) == base {
						matches = append(matches, th)
					}
				}
				if len(matches) == 0 {
					targetHooks = append(targetHooks, jsonfile.Clone(h))
					target.Set("hooks", targetHooks)
					added = append(added, fmt.Sprintf("%s[%s] %s", event, label, base))
					continue
				}
				for _, th := range matches {
					if oldCmd := stringField(th, "command"); oldCmd != canonicalCmd {
						th.Set("command", canonicalCmd)
						added = append(added, fmt.Sprintf("%s[%s] %s repath %s -> %s", event, label, base, oldCmd, canonicalCmd))
					}
					for _, key := range guardrail.HookOptionKeys() {
						have, _ := th.Get(key)
						want, wanted := h.Get(key)
						if jsonfile.Equal(have, want) {
							continue
						}
						if wanted {
							th.Set(key, jsonfile.Clone(want))
						} else {
							th.Delete(key)
						}
						added = append(added, fmt.Sprintf("%s[%s] %s option %s", event, label, base, key))
					}
				}
			}
		}
	}
	return added, nil
}

// checkCanonicalHooks refuses canonical hooks MergeHooks cannot walk: an
// event value or an entry's hooks that is not an array, or an entry or hook
// that is not an object. An entry without hooks has none.
func checkCanonicalHooks(canonical *jsonfile.Object) error {
	for _, event := range canonical.Keys() {
		v, _ := canonical.Get(event)
		entries, ok := v.([]jsonfile.Value)
		if !ok {
			return &MalformedSettingsError{Detail: fmt.Sprintf("hooks %q is %s, expected an array", event, jsonfile.Describe(v))}
		}
		for _, e := range entries {
			entry, ok := e.(*jsonfile.Object)
			if !ok {
				return &MalformedSettingsError{Detail: fmt.Sprintf("a hooks %q entry is %s, expected an object", event, jsonfile.Describe(e))}
			}
			hv, present := entry.Get("hooks")
			if !present {
				continue
			}
			hooks, ok := hv.([]jsonfile.Value)
			if !ok {
				return &MalformedSettingsError{Detail: fmt.Sprintf("a hooks %q entry's hooks is %s, expected an array", event, jsonfile.Describe(hv))}
			}
			for _, h := range hooks {
				if _, ok := h.(*jsonfile.Object); !ok {
					return &MalformedSettingsError{Detail: fmt.Sprintf("a hook in a hooks %q entry is %s, expected an object", event, jsonfile.Describe(h))}
				}
			}
		}
	}
	return nil
}
