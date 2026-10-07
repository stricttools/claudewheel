package wizard

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// The settings choices of the form.
const (
	defaultsTemplate = "Defaults template"
	hideAdvanced     = "Hide advanced"
	showAdvanced     = "Show advanced"
)

// The keys of the advanced checkboxes.
const (
	keyWireHooks          = "wire_hooks"
	keySymlinkShared      = "symlink_shared"
	keyDisableRecap       = "disable_recap"
	keyCleanup10y         = "cleanup_10y"
	keyDisableMemory      = "disable_memory"
	keyDisableAttribution = "disable_attribution"
)

// checkbox is one advanced checkbox: its field key and label.
type checkbox struct {
	key   string
	label string
}

// advancedCheckboxes are the form's advanced options, all on by default.
func advancedCheckboxes() []checkbox {
	return []checkbox{
		{keyWireHooks, "Wire common hooks"},
		{keySymlinkShared, "Symlink to shared store"},
		{keyDisableRecap, "Disable recap"},
		{keyCleanup10y, "10-year cleanup period"},
		{keyDisableMemory, "Disable auto-memory"},
		{keyDisableAttribution, "Disable Co-Authored-By"},
	}
}

// Choices are the answers of a submitted profile form.
type Choices struct {
	Name string
	// ConfigDir is the profile's config directory, a real absolute path.
	ConfigDir string
	// CloneFrom names the profile whose settings.json is copied; empty
	// means the defaults template.
	CloneFrom          string
	WireHooks          bool
	SymlinkShared      bool
	DisableRecap       bool
	Cleanup10y         bool
	DisableMemory      bool
	DisableAttribution bool
}

// displayConfigDir is the config directory of name as the form shows it:
// the real path, with the home directory shortened to ~ when it lies under
// it. It is never used as a path.
func displayConfigDir(store profiles.Store, home, name string) string {
	real := store.PathFor(name)
	if real == home || strings.HasPrefix(real, home+string(filepath.Separator)) {
		return "~" + real[len(home):]
	}
	return real
}

// homeOf is the home directory of the user whose workspace is ws: the
// workspace root is $HOME/.claudewheel.
func homeOf(ws workspace.Workspace) string {
	return filepath.Dir(ws.Root())
}

// validateName returns the form's error message for name, or "" when a new
// profile can take it.
func validateName(store profiles.Store, home, name string, existing []string) string {
	if name == "" {
		return "Name cannot be empty"
	}
	if err := profiles.CheckNewName(name); err != nil {
		return err.Error()
	}
	path := store.PathFor(name)
	_, err := os.Lstat(path)
	if err == nil {
		return displayConfigDir(store, home, name) + " already exists"
	}
	if !pathstat.NotFoundOrNotDirectory(err) {
		return fmt.Sprintf("cannot check %s: %v", path, err)
	}
	if slices.Contains(existing, name) {
		return fmt.Sprintf("Profile '%s' already registered", name)
	}
	return ""
}

// buildFields returns the form's fields in order.
func buildFields(store profiles.Store, home string, existing []string) []*widgets.Field {
	sources := append([]string{defaultsTemplate}, existing...)
	advancedShown := func(fields []*widgets.Field) bool {
		return widgets.Get(fields, "advanced").Value == showAdvanced
	}
	syncConfigDir := func(fields []*widgets.Field) {
		name := widgets.Get(fields, "name").Value
		widgets.Get(fields, "config_dir").Value = displayConfigDir(store, home, name)
	}
	fields := []*widgets.Field{
		{Key: "name", Type: widgets.FieldText, Label: "Name", OnChange: syncConfigDir},
		{Key: "config_dir", Type: widgets.FieldReadonly, Label: "Config dir", Value: displayConfigDir(store, home, "")},
		{Key: "settings_source", Type: widgets.FieldRadio, Label: "Settings source", Value: sources[0], Choices: sources},
		{Key: "advanced", Type: widgets.FieldRadio, Label: "Advanced", Value: hideAdvanced, Choices: []string{hideAdvanced, showAdvanced}},
	}
	for _, cb := range advancedCheckboxes() {
		fields = append(fields, &widgets.Field{
			Key: cb.key, Type: widgets.FieldCheckbox, Label: cb.label, Checked: true, Visible: advancedShown,
		})
	}
	return append(fields, &widgets.Field{Key: "create", Type: widgets.FieldButton, Label: "Create"})
}

// RunProfileForm runs the new-profile form and returns the user's choices,
// or false when the form was cancelled. existing are the names of the
// profiles there are, which the settings can be cloned from. The terminal
// must be in cbreak mode. When ctx is done it returns the cancellation
// cause.
func RunProfileForm(ctx context.Context, t *terminal.Terminal, c widgets.Colors, ws workspace.Workspace, existing []string) (Choices, bool, error) {
	store := profiles.New(ws)
	home := homeOf(ws)
	fields := buildFields(store, home, existing)
	validate := func(fields []*widgets.Field) string {
		name := strings.TrimSpace(widgets.Get(fields, "name").Value)
		return validateName(store, home, name, existing)
	}
	submitted, err := widgets.RunForm(ctx, t, c, widgets.Form{Title: "New Profile", Fields: fields, Validate: validate})
	if err != nil || !submitted {
		return Choices{}, false, err
	}
	name := strings.TrimSpace(widgets.Get(fields, "name").Value)
	clone := widgets.Get(fields, "settings_source").Value
	if clone == defaultsTemplate {
		clone = ""
	}
	checked := func(key string) bool { return widgets.Get(fields, key).Checked }
	return Choices{
		Name:               name,
		ConfigDir:          store.PathFor(name),
		CloneFrom:          clone,
		WireHooks:          checked(keyWireHooks),
		SymlinkShared:      checked(keySymlinkShared),
		DisableRecap:       checked(keyDisableRecap),
		Cleanup10y:         checked(keyCleanup10y),
		DisableMemory:      checked(keyDisableMemory),
		DisableAttribution: checked(keyDisableAttribution),
	}, true, nil
}
