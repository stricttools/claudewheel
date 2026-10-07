package profiles

import (
	"errors"
	"fmt"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/jsonfile"
)

// PermissionCategories returns the permission categories of a settings.json
// (allow, deny, ask), the choices of `permission list --category`.
func PermissionCategories() []string {
	return permissionCategories()
}

// toolNamePattern is a permission rule's tool name.
var toolNamePattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_-]*$`)

// ValidateRule refuses a string that is not a permission rule: a bare tool
// name (Bash) or a tool name with a non-empty pattern in parentheses
// (Bash(git diff:*)).
func ValidateRule(rule string) error {
	if strings.TrimSpace(rule) == "" {
		return errors.New("rule must not be empty or whitespace-only")
	}
	if !strings.ContainsAny(rule, "()") {
		if !toolNamePattern.MatchString(rule) {
			return fmt.Errorf("rule %q must match [A-Za-z][A-Za-z0-9_-]*", rule)
		}
		return nil
	}
	idx := strings.Index(rule, "(")
	if idx < 0 {
		return errors.New("rule contains ')' but no '('")
	}
	if !strings.HasSuffix(rule, ")") {
		return errors.New("rule contains '(' but does not end with ')'")
	}
	tool := rule[:idx]
	if tool == "" {
		return errors.New("tool name before '(' must not be empty")
	}
	if !toolNamePattern.MatchString(tool) {
		return fmt.Errorf("tool name %q must match [A-Za-z][A-Za-z0-9_-]*", tool)
	}
	if idx+1 >= len(rule)-1 {
		return errors.New("content inside parentheses must not be empty")
	}
	return nil
}

// checkCategory refuses a name that is not a permission category.
func checkCategory(category string) error {
	if !slices.Contains(permissionCategories(), category) {
		return fmt.Errorf("category must be one of %s, got %q", strings.Join(permissionCategories(), ", "), category)
	}
	return nil
}

// permissionsBlock returns settings' permissions object, nil when absent.
func permissionsBlock(settings *jsonfile.Object) (*jsonfile.Object, error) {
	v, present := settings.Get("permissions")
	if !present {
		return nil, nil
	}
	perms, ok := v.(*jsonfile.Object)
	if !ok {
		return nil, errors.New("permissions is not an object")
	}
	return perms, nil
}

// permissionRules returns the rules of one category, empty when absent or
// null. A category that is not a list of strings is an error.
func permissionRules(settings *jsonfile.Object, category string) ([]string, error) {
	perms, err := permissionsBlock(settings)
	if err != nil || perms == nil {
		return nil, err
	}
	v, _ := perms.Get(category)
	if v == nil {
		return nil, nil
	}
	list, ok := v.([]jsonfile.Value)
	if !ok {
		return nil, fmt.Errorf("permissions.%s is not a list", category)
	}
	rules := make([]string, 0, len(list))
	for _, item := range list {
		s, ok := item.(string)
		if !ok {
			return nil, fmt.Errorf("permissions.%s holds a rule that is not a string", category)
		}
		rules = append(rules, s)
	}
	return rules, nil
}

// Rules returns the rules of category in settings, in file order.
func Rules(settings *jsonfile.Object, category string) ([]string, error) {
	if err := checkCategory(category); err != nil {
		return nil, err
	}
	return permissionRules(settings, category)
}

// AddRule appends rule to settings' permissions[category], creating the
// block and the list when absent, and reports whether it was added (false:
// already present). The list is never reordered.
func AddRule(settings *jsonfile.Object, category, rule string) (bool, error) {
	if err := checkCategory(category); err != nil {
		return false, err
	}
	rules, err := permissionRules(settings, category)
	if err != nil {
		return false, err
	}
	if slices.Contains(rules, rule) {
		return false, nil
	}
	perms, _ := permissionsBlock(settings)
	if perms == nil {
		perms = jsonfile.NewObject()
		settings.Set("permissions", perms)
	}
	v, _ := perms.Get(category)
	list, _ := v.([]jsonfile.Value)
	perms.Set(category, append(list, rule))
	return true, nil
}

// RemoveRule removes the first occurrence of rule from settings'
// permissions[category] and reports whether it was there.
func RemoveRule(settings *jsonfile.Object, category, rule string) (bool, error) {
	if err := checkCategory(category); err != nil {
		return false, err
	}
	rules, err := permissionRules(settings, category)
	if err != nil {
		return false, err
	}
	i := slices.Index(rules, rule)
	if i < 0 {
		return false, nil
	}
	perms, _ := permissionsBlock(settings)
	v, _ := perms.Get(category)
	list := v.([]jsonfile.Value)
	perms.Set(category, slices.Delete(slices.Clone(list), i, i+1))
	return true, nil
}

// LoadSettings reads a profile's settings.json as an ordered tree; a
// missing file is an error.
func LoadSettings(path string) (*jsonfile.Object, error) {
	settings, found, err := readSettingsFile(path)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, fmt.Errorf("%s does not exist", path)
	}
	return settings, nil
}

// SaveSettings writes settings to path atomically, keeping the file's mode.
func SaveSettings(fx *effects.FX, path string, settings *jsonfile.Object) error {
	data, err := jsonfile.MarshalIndented(settings)
	if err != nil {
		return fmt.Errorf("encoding %s: %w", path, err)
	}
	return fx.WriteFileAtomic(path, data)
}

// PermissionTarget is one profile a permission command acts on.
type PermissionTarget struct {
	Name         string
	SettingsPath string
}

// PermissionTargets resolves the profile selection of a permission command:
// the profile named by profile, or every profile (default included) when
// all. Naming neither or both, an unknown profile, and an empty profile set
// are errors.
func (s Store) PermissionTargets(profile string, all bool) ([]PermissionTarget, error) {
	if (profile == "") == !all {
		return nil, errors.New("one of --profile or --all-profiles is required")
	}
	discovered, err := s.Enumerate()
	if err != nil {
		return nil, err
	}
	var out []PermissionTarget
	for _, p := range discovered {
		if all || p.Name == profile {
			out = append(out, PermissionTarget{Name: p.Name, SettingsPath: filepath.Join(p.Path, SettingsFileName)})
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

// RuleChange is the outcome of a permission edit on one profile.
type RuleChange struct {
	Profile string
	// Changed reports whether the rule was added (or removed); false means
	// it was already present (or not found), and the file was not written.
	Changed bool
}

// AddAllowRule adds rule to the allow list of each target. Only allow is
// edited: the launch resets deny and ask to the canonical lists. A rule
// that is not valid, or that the guardrail lists as an allow conflict
// (which the reconcile would remove again), is refused before any file is
// touched.
func AddAllowRule(fx *effects.FX, targets []PermissionTarget, rule string) ([]RuleChange, error) {
	if err := ValidateRule(rule); err != nil {
		return nil, err
	}
	if guardrail.IsAllowConflict(rule) {
		return nil, fmt.Errorf("%s conflicts with the guardrail's deny and ask rules (it is one of its allow conflicts), so the reconcile (every launch, and `claudewheel patch-profiles --all-profiles`) would remove it again; it cannot be allowed", rule)
	}
	return editAllow(fx, targets, func(settings *jsonfile.Object) (bool, error) {
		return AddRule(settings, "allow", rule)
	})
}

// RemoveAllowRule removes rule from the allow list of each target.
func RemoveAllowRule(fx *effects.FX, targets []PermissionTarget, rule string) ([]RuleChange, error) {
	if strings.TrimSpace(rule) == "" {
		return nil, errors.New("rule must not be empty")
	}
	return editAllow(fx, targets, func(settings *jsonfile.Object) (bool, error) {
		return RemoveRule(settings, "allow", rule)
	})
}

// editAllow applies edit to each target's settings.json in turn, writing
// the files edit changed.
func editAllow(fx *effects.FX, targets []PermissionTarget, edit func(*jsonfile.Object) (bool, error)) ([]RuleChange, error) {
	out := make([]RuleChange, 0, len(targets))
	for _, t := range targets {
		settings, err := LoadSettings(t.SettingsPath)
		if err != nil {
			return out, err
		}
		changed, err := edit(settings)
		if err != nil {
			return out, fmt.Errorf("%s: %w", t.SettingsPath, err)
		}
		if changed {
			if err := SaveSettings(fx, t.SettingsPath, settings); err != nil {
				return out, err
			}
		}
		out = append(out, RuleChange{Profile: t.Name, Changed: changed})
	}
	return out, nil
}

// CategoryRules is one category's rules.
type CategoryRules struct {
	Category string
	Rules    []string
}

// ProfileRules is one profile's listed rules.
type ProfileRules struct {
	Profile    string
	Categories []CategoryRules
}

// ListRules reads each target's rules: the one category named, or every
// category in order when category is empty.
func ListRules(targets []PermissionTarget, category string) ([]ProfileRules, error) {
	categories := permissionCategories()
	if category != "" {
		if err := checkCategory(category); err != nil {
			return nil, err
		}
		categories = []string{category}
	}
	out := make([]ProfileRules, 0, len(targets))
	for _, t := range targets {
		settings, err := LoadSettings(t.SettingsPath)
		if err != nil {
			return nil, err
		}
		p := ProfileRules{Profile: t.Name}
		for _, c := range categories {
			rules, err := permissionRules(settings, c)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", t.SettingsPath, err)
			}
			if rules == nil {
				rules = []string{}
			}
			p.Categories = append(p.Categories, CategoryRules{Category: c, Rules: rules})
		}
		out = append(out, p)
	}
	return out, nil
}
