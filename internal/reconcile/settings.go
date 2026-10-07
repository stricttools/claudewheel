package reconcile

import (
	"encoding/json"
	"fmt"

	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// MalformedSettingsError reports a settings file holding a JSON value of the
// wrong type where the reconcile must descend or edit ("profileDefaults":
// null, "claudewheel": [], a string "permissions"). The target is skipped and
// left untouched: a value nobody can interpret is never repaired.
type MalformedSettingsError struct {
	Detail string
}

func (e *MalformedSettingsError) Error() string { return e.Detail }

// jsonTypeName names v's JSON type the way a settings file spells it.
func jsonTypeName(v jsonfile.Value) string {
	switch v.(type) {
	case nil:
		return "null"
	case bool:
		return "a boolean"
	case json.Number:
		return "a number"
	case string:
		return "a string"
	case []jsonfile.Value:
		return "an array"
	case *jsonfile.Object:
		return "an object"
	}
	return fmt.Sprintf("a %T", v)
}

// objectAt returns container[key] as an object. A missing key is created
// empty (ordinary bootstrap); a present value that is not an object is a
// *MalformedSettingsError, and container is left as it was.
func objectAt(container *jsonfile.Object, key string) (*jsonfile.Object, error) {
	v, present := container.Get(key)
	if !present {
		created := jsonfile.NewObject()
		container.Set(key, created)
		return created, nil
	}
	o, ok := v.(*jsonfile.Object)
	if !ok {
		return nil, &MalformedSettingsError{Detail: fmt.Sprintf("%q is %s, expected an object", key, jsonTypeName(v))}
	}
	return o, nil
}

// listAt returns container[key] as an array. A missing key is created empty;
// a present value that is not an array is a *MalformedSettingsError. Callers
// that append must store the grown array back with container.Set.
func listAt(container *jsonfile.Object, key string) ([]jsonfile.Value, error) {
	v, present := container.Get(key)
	if !present {
		created := []jsonfile.Value{}
		container.Set(key, created)
		return created, nil
	}
	list, ok := v.([]jsonfile.Value)
	if !ok {
		return nil, &MalformedSettingsError{Detail: fmt.Sprintf("%q is %s, expected an array", key, jsonTypeName(v))}
	}
	return list, nil
}

// ruleList reads permissions[category] as rule strings for the deny and ask
// reconciliation: absent is empty; anything but an array of strings is
// malformed, since the reconcile must edit it.
func ruleList(perms *jsonfile.Object, category string) ([]string, error) {
	v, present := perms.Get(category)
	if !present {
		return nil, nil
	}
	list, ok := v.([]jsonfile.Value)
	if !ok {
		return nil, &MalformedSettingsError{Detail: fmt.Sprintf(`"permissions.%s" is %s, expected an array`, category, jsonTypeName(v))}
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		s, ok := item.(string)
		if !ok {
			return nil, &MalformedSettingsError{Detail: fmt.Sprintf(`"permissions.%s" holds %s, expected only strings`, category, jsonTypeName(item))}
		}
		out = append(out, s)
	}
	return out, nil
}

// reconcileList computes what makes current hold the canonical entries and
// nothing else: toAdd in canonical order, toRemove in current order (every
// occurrence of each entry not canonical). Order and duplicates of canonical
// entries already present are left alone.
func reconcileList(current, canonical []string) (toAdd, toRemove []string) {
	canonicalSet := map[string]bool{}
	for _, r := range canonical {
		canonicalSet[r] = true
	}
	currentSet := map[string]bool{}
	for _, r := range current {
		currentSet[r] = true
	}
	for _, r := range canonical {
		if !currentSet[r] {
			toAdd = append(toAdd, r)
		}
	}
	for _, r := range current {
		if !canonicalSet[r] {
			toRemove = append(toRemove, r)
		}
	}
	return toAdd, toRemove
}

// removeFirst removes the first string entry equal to rule from
// perms[category], which the caller has checked is an array.
func removeFirst(perms *jsonfile.Object, category, rule string) {
	v, _ := perms.Get(category)
	list := v.([]jsonfile.Value)
	for i, item := range list {
		if s, ok := item.(string); ok && s == rule {
			out := make([]jsonfile.Value, 0, len(list)-1)
			out = append(out, list[:i]...)
			out = append(out, list[i+1:]...)
			perms.Set(category, out)
			return
		}
	}
}

// appendRule appends rule to perms[category], creating the array when absent.
func appendRule(perms *jsonfile.Object, category, rule string) {
	var list []jsonfile.Value
	if v, present := perms.Get(category); present {
		list = v.([]jsonfile.Value)
	}
	out := make([]jsonfile.Value, 0, len(list)+1)
	out = append(out, list...)
	perms.Set(category, append(out, rule))
}

// reconcilePermissions makes container's permissions.deny and
// permissions.ask hold the canonical rules and nothing else, and removes the
// guardrail's allow conflicts from permissions.allow; nothing is ever added
// to allow. It returns one description per change, empty when already
// canonical. Removals run before additions.
func reconcilePermissions(container *jsonfile.Object) ([]string, error) {
	perms, err := objectAt(container, "permissions")
	if err != nil {
		return nil, err
	}
	deny, err := ruleList(perms, "deny")
	if err != nil {
		return nil, err
	}
	ask, err := ruleList(perms, "ask")
	if err != nil {
		return nil, err
	}
	// allow is only read: an allow value that is not an array, and entries
	// that are not strings, are left as they are.
	var allowRemove []string
	if v, _ := perms.Get("allow"); v != nil {
		if list, ok := v.([]jsonfile.Value); ok {
			for _, item := range list {
				if s, ok := item.(string); ok && guardrail.IsAllowConflict(s) {
					allowRemove = append(allowRemove, s)
				}
			}
		}
	}
	denyAdd, denyRemove := reconcileList(deny, guardrail.CanonicalDenyRules())
	askAdd, askRemove := reconcileList(ask, guardrail.CanonicalAskRules())

	var changes []string
	for _, r := range denyRemove {
		changes = append(changes, "deny -"+r)
	}
	for _, r := range denyAdd {
		changes = append(changes, "deny +"+r)
	}
	for _, r := range askRemove {
		changes = append(changes, "ask -"+r)
	}
	for _, r := range askAdd {
		changes = append(changes, "ask +"+r)
	}
	for _, r := range allowRemove {
		changes = append(changes, "allow -"+r)
	}

	for _, r := range denyRemove {
		removeFirst(perms, "deny", r)
	}
	for _, r := range askRemove {
		removeFirst(perms, "ask", r)
	}
	for _, r := range allowRemove {
		removeFirst(perms, "allow", r)
	}
	for _, r := range denyAdd {
		appendRule(perms, "deny", r)
	}
	for _, r := range askAdd {
		appendRule(perms, "ask", r)
	}
	return changes, nil
}

// reconcileHooks sets container's hooks to the canonical hooks, replacing
// the whole structure, unless they are already equal.
func reconcileHooks(container *jsonfile.Object, canonicalHooks *jsonfile.Object) []string {
	current, _ := container.Get("hooks")
	if jsonfile.Equal(current, canonicalHooks) {
		return nil
	}
	container.Set("hooks", jsonfile.Clone(canonicalHooks))
	return []string{"hooks -> canonical"}
}

// disallowedTools is the canonical stripped-tools list as a JSON array.
func disallowedTools() []jsonfile.Value {
	names := guardrail.DisallowedToolNames()
	out := make([]jsonfile.Value, 0, len(names))
	for _, n := range names {
		out = append(out, n)
	}
	return out
}

// reconcileProfileDisallowed makes a profile's claudewheel.disallowedTools
// canonical and drops the inert top-level disallowedTools key, which Claude
// Code ignores.
func reconcileProfileDisallowed(settings *jsonfile.Object) ([]string, error) {
	var changes []string
	cw, err := objectAt(settings, "claudewheel")
	if err != nil {
		return nil, err
	}
	current, _ := cw.Get("disallowedTools")
	if want := disallowedTools(); !jsonfile.Equal(current, want) {
		cw.Set("disallowedTools", want)
		changes = append(changes, "disallowedTools -> canonical")
	}
	if settings.Delete("disallowedTools") {
		changes = append(changes, "removed inert top-level disallowedTools")
	}
	return changes, nil
}

// reconcileSharedDisallowed makes shared-settings.json's top-level
// disallowedTools canonical.
func reconcileSharedDisallowed(shared *jsonfile.Object) []string {
	current, _ := shared.Get("disallowedTools")
	if want := disallowedTools(); !jsonfile.Equal(current, want) {
		shared.Set("disallowedTools", want)
		return []string{"disallowedTools -> canonical"}
	}
	return nil
}

// reconcileCanonicalSettings sets every guardrail.CanonicalProfileSettings
// key in container that is absent or different, one description per key
// written.
func reconcileCanonicalSettings(container *jsonfile.Object) ([]string, error) {
	canonical := guardrail.CanonicalProfileSettings()
	var changes []string
	for _, key := range canonical.Keys() {
		want, _ := canonical.Get(key)
		current, _ := container.Get(key)
		if jsonfile.Equal(current, want) {
			continue
		}
		container.Set(key, want)
		text, err := jsonfile.MarshalSpacedASCII(want)
		if err != nil {
			return nil, err
		}
		changes = append(changes, key+" -> "+string(text))
	}
	return changes, nil
}

// ReconcileProfileSettings makes one profile settings.json tree canonical in
// place: the hooks, claudewheel.disallowedTools, permissions deny, ask, and
// allow conflicts, and the canonical settings keys. Other keys are left
// alone. It returns one description per change, empty when already
// canonical; a malformed container is a *MalformedSettingsError.
func ReconcileProfileSettings(settings, canonicalHooks *jsonfile.Object) ([]string, error) {
	changes := reconcileHooks(settings, canonicalHooks)
	disallowed, err := reconcileProfileDisallowed(settings)
	if err != nil {
		return nil, err
	}
	changes = append(changes, disallowed...)
	perms, err := reconcilePermissions(settings)
	if err != nil {
		return nil, err
	}
	changes = append(changes, perms...)
	keys, err := reconcileCanonicalSettings(settings)
	if err != nil {
		return nil, err
	}
	return append(changes, keys...), nil
}

// ReconcileSharedSettings makes the shared-settings.json tree canonical in
// place: the top-level hooks and disallowedTools, and inside
// profileDefaults the permissions and the canonical settings keys. Other
// keys are left alone.
func ReconcileSharedSettings(shared, canonicalHooks *jsonfile.Object) ([]string, error) {
	changes := reconcileHooks(shared, canonicalHooks)
	changes = append(changes, reconcileSharedDisallowed(shared)...)
	defaults, err := objectAt(shared, "profileDefaults")
	if err != nil {
		return nil, err
	}
	perms, err := reconcilePermissions(defaults)
	if err != nil {
		return nil, err
	}
	for _, c := range perms {
		changes = append(changes, "profileDefaults "+c)
	}
	keys, err := reconcileCanonicalSettings(defaults)
	if err != nil {
		return nil, err
	}
	for _, c := range keys {
		changes = append(changes, "profileDefaults "+c)
	}
	return changes, nil
}
