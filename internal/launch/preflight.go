package launch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/discover"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/jsonfile"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/projecthooks"
	"github.com/stricttools/claudewheel/internal/reconcile"
	"github.com/stricttools/claudewheel/internal/scratchpad"
	"github.com/stricttools/claudewheel/internal/tokens"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/tui/wizard"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// PreflightContext is what every preflight step reads.
type PreflightContext struct {
	Ctx       context.Context
	FX        *effects.FX
	Workspace workspace.Workspace
	Locator   install.Locator
	// Selections maps each segment with a value to it.
	Selections map[string]string
	// Client is the client being launched.
	Client string
	// Interactive is false when nobody can be asked: print mode, or no
	// terminal.
	Interactive bool
	// Stderr receives the reports a step prints.
	Stderr  io.Writer
	screens *screens
}

// abortError is a step's decision to stop the launch, with the message
// saying why and what to do.
type abortError struct {
	msg string
}

func (e *abortError) Error() string { return e.msg }

// abort stops the launch with msg.
func abort(format string, args ...any) error {
	return &abortError{msg: fmt.Sprintf(format, args...)}
}

// PreflightStep is one step of the preflight. A step that has nothing to do
// returns nil; an error stops the launch.
type PreflightStep struct {
	// Name identifies the step in errors.
	Name string
	// Clients are the clients the step applies to; it is skipped for any
	// other.
	Clients []string
	// NonInteractive is true when the step also runs when nobody can be
	// asked; otherwise it is skipped then.
	NonInteractive bool
	Run            func(PreflightContext) error
}

// claudeOnly is the client list of a step that concerns Claude Code alone.
func claudeOnly() []string { return []string{ClientClaude} }

// PreflightSteps returns the preflight steps in the order they run.
func PreflightSteps() []PreflightStep {
	return []PreflightStep{
		// It runs without a terminal too, applying an existing opt-in; an
		// unanswered choice stays unanswered there.
		{Name: "vanilla-choice", Clients: claudeOnly(), NonInteractive: true, Run: vanillaChoice},
		{Name: "reconcile-guardrails", Clients: claudeOnly(), NonInteractive: true, Run: reconcileGuardrails},
		{Name: "model-version-guard", Clients: claudeOnly(), NonInteractive: true, Run: modelVersionGuard},
		// After the version guard: it resolves the same version, and must not
		// mark as seen a version the guard refused to launch.
		{Name: "release-notes-seen", Clients: claudeOnly(), NonInteractive: true, Run: releaseNotesSeen},
		// Without a terminal it refuses rather than launching with the tier
		// resolved to null.
		{Name: "plan-declaration", Clients: claudeOnly(), NonInteractive: true, Run: planDeclaration},
		// Every client runs the project's hooks, so every client reviews them.
		{Name: "approved-hooks", Clients: AdapterNames(), NonInteractive: true, Run: approvedHooks},
		{Name: "scratchpad-cleanup", Clients: AdapterNames(), NonInteractive: false, Run: scratchpadCleanup},
		{Name: "prune-stale-inodes", Clients: AdapterNames(), NonInteractive: true, Run: pruneStaleInodes},
	}
}

// RunPreflight runs steps in order, skipping those that do not apply to the
// client and, when nobody can be asked, those that need someone. The first
// error stops it.
func RunPreflight(pc PreflightContext, steps []PreflightStep) error {
	for _, step := range steps {
		if !slices.Contains(step.Clients, pc.Client) {
			continue
		}
		if !pc.Interactive && !step.NonInteractive {
			continue
		}
		if err := step.Run(pc); err != nil {
			var aborted *abortError
			if errors.As(err, &aborted) {
				return err
			}
			return fmt.Errorf("preflight %s: %w", step.Name, err)
		}
	}
	return nil
}

// isDefaultProfile reports whether the launch is of Claude Code's own
// ~/.claude: the default profile, or no profile at all.
func isDefaultProfile(selections map[string]string) bool {
	p := selections[segProfile]
	return p == "" || p == profiles.DefaultName
}

// vanillaChoice asks once whether claudewheel's guardrail hooks go into
// ~/.claude when the default profile is launched, and applies an opt-in.
// Unanswered and nobody to ask: the launch goes ahead vanilla and the
// question stays open. Escape leaves it open too.
func vanillaChoice(pc PreflightContext) error {
	if !isDefaultProfile(pc.Selections) {
		return nil
	}
	st, err := appconfig.ReadState(pc.Workspace)
	if err != nil {
		return err
	}
	if st.VanillaGuardrailsOptIn == nil {
		if !pc.Interactive {
			return nil
		}
		answer, err := pc.screens.confirm(pc.Ctx, widgets.Confirmation{
			Title: "Launching the default profile (~/.claude)",
			Lines: []string{
				"The 'default' profile is Claude Code's own ~/.claude.",
				"cw treats it as vanilla and strictly read-only: no guardrails,",
				"no hooks, no settings are applied unless you opt in.",
				"",
				"You can opt in to cw's guardrail hooks (block unsafe commands,",
				"worktree isolation, timestamps, advice). This ADDITIVELY writes",
				"cw's hooks into ~/.claude/settings.json and never prunes your own.",
				"",
				"You can change this later from the profile inspect page (press 'i').",
			},
			Accept:  "enable cw guardrails",
			Decline: "stay vanilla",
			Skip:    "stay vanilla this time, ask again next launch",
		})
		if err != nil {
			return err
		}
		if answer == widgets.Skip {
			return nil
		}
		optIn := answer == widgets.Accept
		if err := appconfig.SetVanillaGuardrailsOptIn(pc.FX, pc.Workspace, optIn); err != nil {
			return err
		}
		st.VanillaGuardrailsOptIn = &optIn
	}
	if *st.VanillaGuardrailsOptIn {
		_, err := EnsureVanillaGuardrails(pc.FX, pc.Workspace)
		return err
	}
	return nil
}

// reconcileGuardrails makes every managed profile and shared-settings.json
// exactly canonical before every launch. A reconcile error stops the launch;
// a reconcile that changed anything, or skipped a target, prints its report.
func reconcileGuardrails(pc PreflightContext) error {
	report, err := reconcile.Run(pc.FX, pc.Workspace, reconcile.AllProfiles())
	if err != nil {
		return err
	}
	if report.ChangedAny() || len(report.Skipped()) > 0 {
		fmt.Fprintln(pc.Stderr, "claudewheel: guardrail reconcile before launch:")
		for _, line := range report.Lines() {
			fmt.Fprintln(pc.Stderr, line)
		}
	}
	return nil
}

// modelVersionGuard refuses to launch a model on a Claude Code binary older
// than the model needs. A model with no minimum, and a launch whose version
// cannot be determined, pass.
func modelVersionGuard(pc PreflightContext) error {
	model := strings.TrimSuffix(pc.Selections[segModel], discover.Context1MSuffix)
	if model == "" {
		return nil
	}
	minVersion, ok := appconfig.ModelMinCLIVersion()[model]
	if !ok {
		return nil
	}
	version, ok, err := install.EffectiveCLIVersion(pc.Selections[segVersion], pc.Locator)
	if err != nil {
		return err
	}
	if !ok || install.CompareVersions(version, minVersion) >= 0 {
		return nil
	}
	return abort("Model %s requires Claude Code %s or newer, but the effective binary version is %s. Run `claudewheel install %s` (or a newer version) and select it before launching.", model, minVersion, version, minVersion)
}

// lastReleaseNotesSeenKey is the key of a profile's .claude.json in which
// Claude Code records the version whose release notes were shown.
const lastReleaseNotesSeenKey = "lastReleaseNotesSeen"

// releaseVersionRE is a version the release-notes key may be given.
var releaseVersionRE = regexp.MustCompile(`^\d+\.\d+\.\d+$`)

// releaseNotesSeen marks the launched Claude Code version as seen in the
// profile's .claude.json, so the client shows no "Updated to latest"
// summary. At startup Claude Code prints that summary whenever the key holds
// a version lower than the running one, then writes the running version
// there itself; no setting or variable turns it off. This is undocumented
// client behavior, read out of the Claude Code 2.1.263 binary.
//
// Nothing is done for the default profile (claudewheel never writes
// ~/.claude), for a version that cannot be determined or is not
// MAJOR.MINOR.PATCH (a non-version written there would be rewritten on every
// launch), for a profile with no .claude.json yet (the client creates it
// without the key, which shows nothing), and when the key already holds the
// version or a later one. A file that cannot be read, is not a JSON object,
// or cannot be written stops the launch.
func releaseNotesSeen(pc PreflightContext) error {
	if isDefaultProfile(pc.Selections) {
		return nil
	}
	version, ok, err := install.EffectiveCLIVersion(pc.Selections[segVersion], pc.Locator)
	if err != nil {
		return err
	}
	if !ok || !releaseVersionRE.MatchString(version) {
		return nil
	}
	path := filepath.Join(profiles.New(pc.Workspace).PathFor(pc.Selections[segProfile]), profiles.GlobalConfigName)
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("reading %s to mark release notes as seen: %w", path, err)
	}
	doc, err := jsonfile.DecodeObject(data)
	if err != nil {
		return fmt.Errorf("reading %s to mark release notes as seen: %w", path, err)
	}
	if v, ok := doc.Get(lastReleaseNotesSeenKey); ok {
		if stored, ok := v.(string); ok && stored != "" && install.CompareVersions(stored, version) >= 0 {
			return nil
		}
	}
	doc.Set(lastReleaseNotesSeenKey, version)
	out, err := jsonfile.MarshalIndented(doc)
	if err != nil {
		return fmt.Errorf("encoding %s: %w", path, err)
	}
	if err := pc.FX.WriteFileAtomic(path, out); err != nil {
		return fmt.Errorf("writing %s to mark release notes as seen: %w", path, err)
	}
	return nil
}

// planDeclaration requires a declared plan for a profile launching on a
// stored token. With a setup token Claude Code reads the subscription tier
// from the launch environment alone, so an undeclared plan launches with the
// tier null and tier-dependent features failing closed. A profile without a
// stored token authenticates from Claude Code's own credentials, which carry
// the tier. Interactive, the plan picker asks and its answer is stored; a
// cancelled picker, and nobody to ask, stop the launch naming the command
// that declares the plan.
func planDeclaration(pc PreflightContext) error {
	if isDefaultProfile(pc.Selections) {
		return nil
	}
	profile := pc.Selections[segProfile]
	data := profiles.New(pc.Workspace).Data(profile)
	entry, _, err := data.Load()
	if err != nil {
		return err
	}
	if _, hasToken := entry.TokenValue(); !hasToken || entry.DeclaresPlan() {
		return nil
	}
	refusal := abort("Profile '%s' launches on its stored token but declares no plan, so Claude Code resolves the subscription tier to null and tier-dependent features fail closed. Declare it with `claudewheel profile set-plan %s <plan>` (one of: %s).",
		profile, profile, strings.Join(tokens.PlanKeys(), ", "))
	if !pc.Interactive {
		return refusal
	}
	plan, chosen, err := pickPlan(pc, profile)
	if err != nil {
		return err
	}
	if !chosen {
		return refusal
	}
	return data.SetPlan(pc.FX, plan)
}

// pickPlan runs the plan picker on its own screen.
func pickPlan(pc PreflightContext, profile string) (plan tokens.PlanTier, chosen bool, err error) {
	t, err := pc.screens.open(pc.Ctx)
	if err != nil {
		return tokens.PlanTier{}, false, err
	}
	defer func() { err = errors.Join(err, t.Close()) }()
	return wizard.PickPlan(pc.Ctx, t, pc.screens.colors, fmt.Sprintf("Plan for profile '%s'", profile))
}

// approvedHooks stops the launch until the target project's Claude Code
// hooks (.claude/settings.json and settings.local.json) are approved: a
// project with no hooks passes, and so does one whose hooks match the
// fingerprint approved for it. New or changed hooks are shown for approval;
// a decline, and nobody to ask, stop the launch. A settings file that is not
// valid JSON stops it naming the file.
func approvedHooks(pc PreflightContext) error {
	directory, err := projecthooks.TargetDirectory(pc.Selections[segDirectory])
	if err != nil {
		return err
	}
	hooks, err := projecthooks.Read(directory)
	var malformed *projecthooks.MalformedError
	if errors.As(err, &malformed) {
		return abort("The target project's Claude Code hooks config is malformed: .claude/%s could not be parsed as JSON. Fix or remove it before launching.", malformed.Filename)
	}
	if err != nil {
		return err
	}
	if !hooks.HasHooks() {
		return nil
	}
	fingerprint, err := hooks.Fingerprint()
	if err != nil {
		return err
	}
	stored, found, err := appconfig.ProjectHookApproval(pc.Workspace, directory)
	if err != nil {
		return err
	}
	if found && stored == fingerprint {
		return nil
	}
	verb := "contributes"
	if found {
		verb = "changed its"
	}
	if !pc.Interactive {
		return abort("The target project %s Claude Code hooks that have not been approved. These hooks run arbitrary commands. Run an interactive launch (the TUI) to review and approve them before launching.", verb)
	}
	lines := []string{"The target project would run these hooks:", ""}
	lines = append(lines, hooks.ListingLines()...)
	lines = append(lines, "", "Approve only if you trust them -- they run arbitrary commands.")
	answer, err := pc.screens.confirm(pc.Ctx, widgets.Confirmation{
		Title:   fmt.Sprintf("This project %s Claude Code hooks", verb),
		Lines:   lines,
		Accept:  "approve and launch",
		Decline: "decline and abort",
		Skip:    "abort",
	})
	if err != nil {
		return err
	}
	if answer != widgets.Accept {
		return abort("Declined the target project's Claude Code hooks. Launch aborted.")
	}
	return appconfig.SetProjectHookApproval(pc.FX, pc.Workspace, directory, fingerprint)
}

// scratchpadCleanup offers to delete each stale Claude Code scratchpad
// directory under /tmp, one confirmation per directory: y deletes it, n
// keeps it and never offers it again, Escape keeps it for now. A deletion
// that fails stops the launch.
func scratchpadCleanup(pc PreflightContext) error {
	dismissed, err := appconfig.DismissedScratchpadDirs(pc.Workspace)
	if err != nil {
		return err
	}
	dirs, err := scratchpad.ScanDirs(scratchpad.TmpClaudeDir())
	if err != nil {
		return err
	}
	now := time.Now()
	var remove []scratchpad.Dir
	var dismiss []string
	for _, d := range dirs {
		if !d.IsStale(now, scratchpad.StaleDays) || slices.Contains(dismissed, d.Path) {
			continue
		}
		answer, err := pc.screens.confirm(pc.Ctx, widgets.Confirmation{
			Title: "Stale Claude Code scratchpad data under /tmp",
			Lines: []string{
				"This per-project scratchpad directory looks stale:",
				"",
				fmt.Sprintf("  %s   %s   %dd old", d.Name, profiles.FormatSize(d.SizeBytes), int(d.AgeDays(now))),
				"",
				"Deleting it frees /tmp space.",
			},
			Accept:  "delete it",
			Decline: "keep it, never ask again",
			Skip:    "keep it for now",
		})
		if err != nil {
			return err
		}
		switch answer {
		case widgets.Accept:
			remove = append(remove, d)
		case widgets.Decline:
			dismiss = append(dismiss, d.Path)
		}
	}
	if len(dismiss) > 0 {
		if err := appconfig.DismissScratchpadDirs(pc.FX, pc.Workspace, dismiss); err != nil {
			return err
		}
	}
	if err := scratchpad.Remove(pc.FX, remove); err != nil {
		return fmt.Errorf("could not delete scratchpad directories: %w", err)
	}
	return nil
}

// pruneStaleInodes removes from the shared store's inode map the entries
// of directories deleted rather than renamed (appconfig.PruneStaleInodes),
// and prints a line naming them when it removed any.
func pruneStaleInodes(pc PreflightContext) error {
	pruned, err := appconfig.PruneStaleInodes(pc.FX, pc.Workspace.Shared())
	if err != nil {
		return err
	}
	if len(pruned) > 0 {
		fmt.Fprintf(pc.Stderr, "claudewheel: removed %d entries of directories that no longer exist from %s: %s\n", len(pruned), pc.Workspace.InodesFile(), strings.Join(pruned, ", "))
	}
	return nil
}
