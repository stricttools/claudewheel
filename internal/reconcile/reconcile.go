// Package reconcile makes every managed guardrail target exactly canonical:
// each managed profile's settings.json and, when every profile is selected,
// shared-settings.json.
//
// Within each target the hooks are replaced by the canonical wiring, the
// stripped-tools list (claudewheel.disallowedTools in a profile, the
// top-level disallowedTools in shared-settings.json) is made canonical,
// permissions.deny and permissions.ask are made to hold the canonical rules
// and nothing else, the guardrail's allow conflicts are removed from
// permissions.allow, and the canonical settings keys are set (at the top
// level of a profile, inside profileDefaults in shared-settings.json).
// User-added extras in those sections are pruned; every other key is left
// alone. Missing guardrail hook scripts are deployed, since wiring that
// points at a missing script is not canonical.
//
// The default profile (Claude Code's own ~/.claude) is never read or
// written. A file already canonical is not written. A target whose file is
// missing, unreadable, or malformed is skipped with its reason in the
// report and left untouched; the other targets are still reconciled.
package reconcile

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/guardrail"
	"github.com/stricttools/claudewheel/internal/hookscripts"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// SharedSettingsLabel is the report label of the shared-settings.json target.
const SharedSettingsLabel = "shared-settings.json"

// Selection is the choice of targets: one managed profile, or every managed
// profile together with shared-settings.json. Build it with OneProfile or
// AllProfiles; the zero value selects nothing and Run refuses it.
type Selection struct {
	profile string
	all     bool
}

// OneProfile selects the managed profile name alone; shared-settings.json is
// left untouched.
func OneProfile(name string) Selection {
	return Selection{profile: name}
}

// AllProfiles selects every managed profile and shared-settings.json.
func AllProfiles() Selection {
	return Selection{all: true}
}

// TargetReport is the outcome of reconciling one target file.
type TargetReport struct {
	// Label is the profile name, or SharedSettingsLabel.
	Label string
	// Changed reports whether the file differed from canonical.
	Changed bool
	// Written reports whether the canonical file was written (false under
	// --dry-run, where the write is recorded instead).
	Written bool
	// Changes describes each change, in the order made.
	Changes []string
	// SkipReason is set when the target was skipped and left untouched: no
	// file, an unreadable file, or a malformed guardrail container.
	SkipReason string
}

// Report is the outcome of one reconcile.
type Report struct {
	// Previewed reports a --dry-run reconcile: nothing was written.
	Previewed bool
	// ScriptsDeployed are the hook scripts deployed; ScriptsWouldDeploy
	// those a preview would deploy.
	ScriptsDeployed    []string
	ScriptsWouldDeploy []string
	Targets            []TargetReport
}

// ChangedAny reports whether anything was (or, in a preview, would be)
// written.
func (r Report) ChangedAny() bool {
	if len(r.ScriptsDeployed) > 0 || len(r.ScriptsWouldDeploy) > 0 {
		return true
	}
	for _, t := range r.Targets {
		if t.Changed {
			return true
		}
	}
	return false
}

// Skipped returns the targets that were skipped, in report order.
func (r Report) Skipped() []TargetReport {
	var out []TargetReport
	for _, t := range r.Targets {
		if t.SkipReason != "" {
			out = append(out, t)
		}
	}
	return out
}

// Lines renders the report as the lines patch-profiles prints.
func (r Report) Lines() []string {
	var lines []string
	switch {
	case len(r.ScriptsWouldDeploy) > 0:
		for _, name := range r.ScriptsWouldDeploy {
			lines = append(lines, "hook script: would deploy "+name)
		}
	case len(r.ScriptsDeployed) > 0:
		for _, name := range r.ScriptsDeployed {
			lines = append(lines, "hook script: deployed "+name)
		}
	default:
		lines = append(lines, "hook scripts: all present")
	}
	for _, t := range r.Targets {
		switch {
		case t.SkipReason != "":
			lines = append(lines, t.Label+": "+t.SkipReason)
		case !t.Changed:
			lines = append(lines, t.Label+": already canonical, no changes")
		default:
			verb := "reconciled"
			if r.Previewed {
				verb = "would reconcile"
			}
			lines = append(lines, t.Label+": "+verb)
			for _, c := range t.Changes {
				lines = append(lines, "    "+c)
			}
		}
	}
	lines = append(lines, "")
	switch {
	case !r.ChangedAny():
		lines = append(lines, "Everything already canonical.")
	case r.Previewed:
		lines = append(lines, "Dry run: no files were written.")
	default:
		lines = append(lines, "Reconciled to canonical.")
	}
	return lines
}

// target is one settings file to reconcile.
type target struct {
	label     string
	path      string
	reconcile func(settings, canonicalHooks *jsonfile.Object) ([]string, error)
}

// Run reconciles the selected targets to exactly canonical through fx: it
// checks the selection, deploys the missing guardrail hook scripts, then
// reconciles each selected profile in name order and, for AllProfiles,
// shared-settings.json last. An empty selection, a profile that is not a
// managed profile, a failed profile enumeration, a failed script
// deployment, and a failed write are errors; a write error stops the run,
// and the report holds what was done before it.
func Run(fx *effects.FX, ws workspace.Workspace, sel Selection) (Report, error) {
	report := Report{Previewed: fx.Previewing()}
	targets, err := selectTargets(ws, sel)
	if err != nil {
		return report, err
	}
	canonicalHooks := canonicalHooksTree(ws)

	if err := deployMissingScripts(fx, ws, canonicalHooks, &report); err != nil {
		return report, err
	}

	for _, t := range targets {
		tr, err := processFile(fx, t, canonicalHooks)
		report.Targets = append(report.Targets, tr)
		if err != nil {
			return report, err
		}
	}
	return report, nil
}

// canonicalHooksTree is the canonical hooks object for ws's scripts directory.
func canonicalHooksTree(ws workspace.Workspace) *jsonfile.Object {
	v, _ := guardrail.CanonicalSharedSettings(ws.ScriptsDir()).Get("hooks")
	return v.(*jsonfile.Object)
}

// selectTargets resolves sel to the settings files to reconcile. Profiles are
// enumerated with an unreadable token file read as no token: the reconcile
// touches settings, not tokens.
func selectTargets(ws workspace.Workspace, sel Selection) ([]target, error) {
	if sel.all == (sel.profile != "") {
		return nil, errors.New("reconcile: select one profile or all profiles")
	}
	if sel.profile == profiles.DefaultName {
		return nil, fmt.Errorf("profile %q is Claude Code's own ~/.claude, which the reconcile never touches", profiles.DefaultName)
	}
	discovered, err := profiles.New(ws).Discover(profiles.CorruptTokenAsNone)
	if err != nil {
		return nil, err
	}
	var managed []string
	var targets []target
	for _, p := range discovered {
		if p.Name == profiles.DefaultName {
			continue
		}
		managed = append(managed, p.Name)
		if sel.all || p.Name == sel.profile {
			targets = append(targets, target{
				label:     p.Name,
				path:      filepath.Join(p.Path, profiles.SettingsFileName),
				reconcile: ReconcileProfileSettings,
			})
		}
	}
	if !sel.all {
		if len(targets) == 0 {
			known := "there are none"
			if len(managed) > 0 {
				known = "they are " + strings.Join(managed, ", ")
			}
			return nil, fmt.Errorf("profile %q not found among the managed profiles: %s", sel.profile, known)
		}
		return targets, nil
	}
	return append(targets, target{
		label:     SharedSettingsLabel,
		path:      ws.SharedSettingsFile(),
		reconcile: ReconcileSharedSettings,
	}), nil
}

// deployMissingScripts deploys the deployable scripts canonicalHooks refers
// to that are not in the scripts directory, recording their names on report.
func deployMissingScripts(fx *effects.FX, ws workspace.Workspace, canonicalHooks *jsonfile.Object, report *Report) error {
	missing, err := hookscripts.MissingScripts(ReferencedScripts(canonicalHooks), ws.ScriptsDir())
	if err != nil {
		return err
	}
	if len(missing) == 0 {
		return nil
	}
	results, err := hookscripts.DeployScripts(fx, missing, ws.ScriptsDir(), false)
	if err != nil {
		return err
	}
	for _, r := range results {
		if fx.Previewing() {
			report.ScriptsWouldDeploy = append(report.ScriptsWouldDeploy, r.Name)
		} else {
			report.ScriptsDeployed = append(report.ScriptsDeployed, r.Name)
		}
	}
	return nil
}

// processFile reads, reconciles, compares, and writes one target. The file is
// written only when reconciling changed it. A missing, unreadable, or
// malformed file is a skip, not an error.
func processFile(fx *effects.FX, t target, canonicalHooks *jsonfile.Object) (TargetReport, error) {
	tr := TargetReport{Label: t.label}
	data, err := os.ReadFile(t.path)
	if pathstat.NotFoundOrParentNotDirectory(err) {
		tr.SkipReason = "no settings.json"
		return tr, nil
	}
	if err != nil {
		tr.SkipReason = fmt.Sprintf("unreadable (%v)", err)
		return tr, nil
	}
	settings, err := jsonfile.DecodeObject(data)
	if err != nil {
		tr.SkipReason = fmt.Sprintf("unreadable (%s: %v)", t.path, err)
		return tr, nil
	}
	original := jsonfile.Clone(settings)
	changes, err := t.reconcile(settings, canonicalHooks)
	var malformed *MalformedSettingsError
	if errors.As(err, &malformed) {
		tr.SkipReason = fmt.Sprintf("malformed (%s: %s)", t.path, malformed.Detail)
		return tr, nil
	}
	if err != nil {
		return tr, fmt.Errorf("%s: %w", t.path, err)
	}
	tr.Changes = changes
	tr.Changed = !jsonfile.Equal(settings, original)
	if !tr.Changed {
		return tr, nil
	}
	if err := profiles.SaveSettings(fx, t.path, settings); err != nil {
		return tr, err
	}
	tr.Written = !fx.Previewing()
	return tr, nil
}
