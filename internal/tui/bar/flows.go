package bar

import (
	"context"
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/archiver"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/tokens"
	"github.com/stricttools/claudewheel/internal/tui/sessionsview"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// pageWidth is the width the bar's pages wrap their prose to.
const pageWidth = 56

// hintClose is the hint of an inspect page with no action of its own.
const hintClose = "any key: close"

// mebibyte is the unit the install progress counts in.
const mebibyte = 1024 * 1024

// wrapText breaks text into lines of at most width characters at spaces;
// a word longer than width is split.
func wrapText(text string, width int) []string {
	var lines []string
	current := ""
	for _, word := range strings.Fields(text) {
		for runeLen(word) > width {
			if current != "" {
				lines = append(lines, current)
				current = ""
			}
			lines = append(lines, runePrefix(word, width))
			word = runeSuffixFrom(word, width)
		}
		switch {
		case current == "":
			current = word
		case runeLen(current)+1+runeLen(word) <= width:
			current += " " + word
		default:
			lines = append(lines, current)
			current = word
		}
	}
	if current != "" {
		lines = append(lines, current)
	}
	return lines
}

// page shows a fullscreen page any key closes.
func (a *app) page(ctx context.Context, title string, lines []string) error {
	_, err := widgets.ShowPage(ctx, a.t, a.renderer.Colors, widgets.Page{Title: title, Lines: lines, Hint: widgets.HintAnyKey})
	return err
}

// installFlow offers to install Claude Code version, downloads it with the
// terminal out of cbreak mode (showing the progress), and reports the
// result on a page.
func (a *app) installFlow(ctx context.Context, seg *Segment, version string) error {
	answer, err := widgets.Confirm(ctx, a.t, a.renderer.Colors, widgets.Confirmation{
		Title:   fmt.Sprintf("Install Claude Code v%s?", version),
		Accept:  "install it",
		Decline: "do not install",
		Skip:    "do not install",
	})
	if err != nil || answer != widgets.Accept {
		return err
	}
	var installErr, writeErr error
	err = a.t.Cooked(func() error {
		if err := a.t.Write(fmt.Sprintf("Downloading Claude Code %s...\n", version)); err != nil {
			return err
		}
		progress := func(done, total int64) {
			if total <= 0 || writeErr != nil {
				return
			}
			writeErr = a.t.Write(fmt.Sprintf("\r  %.0f/%.0f MB (%d%%)",
				float64(done)/mebibyte, float64(total)/mebibyte, done*100/total))
		}
		_, installErr = install.Install(a.fx, a.in.Locator, version, progress)
		return writeErr
	})
	if err != nil {
		return err
	}
	// Leaving cbreak mode ended the mode 2031 subscription.
	if a.mode2031 {
		if err := a.t.SubscribeMode2031(); err != nil {
			return err
		}
	}
	if installErr != nil {
		return a.page(ctx, "Install failed", []string{
			"Version: " + version,
			"",
			"Error: " + installErr.Error(),
		})
	}
	seg.State.MarkInstalled(version)
	return a.page(ctx, "Install complete", []string{
		fmt.Sprintf("Claude Code %s installed successfully.", version),
	})
}

// interceptUnauthenticated offers to authenticate profile before the
// launch. After an outcome that may have written credentials, profile
// discovery runs again.
func (a *app) interceptUnauthenticated(ctx context.Context, seg *Segment, profile string) (AuthOutcome, error) {
	outcome, err := a.in.Flows.Authenticate(ctx, profile)
	if err != nil {
		return "", err
	}
	switch outcome {
	case AuthAuthenticated, AuthUnverified, AuthFailed:
		if err := refreshProfiles(seg, a.profiles); err != nil {
			return "", err
		}
	case AuthSkip, AuthCancel:
	default:
		return "", fmt.Errorf("unknown authentication outcome %q", outcome)
	}
	return outcome, nil
}

// profileWizard runs the create-profile flow and selects the new profile.
func (a *app) profileWizard(ctx context.Context, seg *Segment) error {
	name, created, err := a.in.Flows.CreateProfile(ctx)
	if err != nil || !created {
		return err
	}
	seg.State.AddPinned(name)
	// The profile is new, and the authentication may have written
	// credentials: discover again whatever its outcome.
	if err := refreshProfiles(seg, a.profiles); err != nil {
		return err
	}
	seg.SelectValue(name)
	return nil
}

// inspectProfile shows the focused profile's report. On the default
// profile, g turns claudewheel's guardrails on ~/.claude on or off (one
// machine-wide choice); on a profile whose session credentials shadow its
// stored token, f removes them.
func (a *app) inspectProfile(ctx context.Context, seg *Segment) error {
	name, ok := seg.Value()
	if !ok {
		return nil
	}
	report, err := a.profiles.GatherReport(name, a.in.Now())
	var corrupt *tokens.StoreError
	if errors.As(err, &corrupt) {
		a.flash = "Cannot inspect: " + err.Error()
		return nil
	}
	if err != nil {
		return err
	}
	lines := profiles.FormatReport(report)
	isDefault := name == profiles.DefaultName
	optIn := false
	hint := hintClose
	switch {
	case isDefault:
		st, err := appconfig.ReadState(a.store.Workspace())
		if err != nil {
			return err
		}
		optIn = st.VanillaGuardrailsOptIn != nil && *st.VanillaGuardrailsOptIn
		state, toggle := "disabled", "enable"
		if optIn {
			state, toggle = "enabled", "disable"
		}
		lines = append(lines, "cw guardrails: "+state)
		hint = "g: " + toggle + " cw guardrails   " + hintClose
	case report.HasAuthShadow:
		hint = "f: fix auth shadow   " + hintClose
	}
	key, err := widgets.ShowPage(ctx, a.t, a.renderer.Colors, widgets.Page{Title: "Profile: " + name, Lines: lines, Hint: hint})
	if err != nil {
		return err
	}
	switch {
	case isDefault && (key == "g" || key == "G"):
		enable := !optIn
		if err := appconfig.SetVanillaGuardrailsOptIn(a.fx, a.store.Workspace(), enable); err != nil {
			return err
		}
		if err := a.in.Flows.SetVanillaGuardrails(a.fx, enable); err != nil {
			return err
		}
		if enable {
			a.flash = "cw guardrails enabled for ~/.claude"
		} else {
			a.flash = "cw guardrails removed from ~/.claude"
		}
	case !isDefault && key == "f" && report.HasAuthShadow:
		outcome, err := a.profiles.FixAuth(a.fx, name)
		switch {
		case err != nil:
			a.flash = "Could not fix: " + err.Error()
		case outcome == profiles.FixAuthRemoved:
			a.flash = "Auth shadow fixed"
		default:
			a.flash = "Could not fix: " + string(outcome)
		}
	}
	return nil
}

// authSummary names what authentication a profile holds.
func authSummary(r profiles.Report) string {
	switch {
	case r.HasCredentials && r.HasToken:
		return "credentials+token"
	case r.HasCredentials:
		return "credentials"
	case r.HasToken:
		return "token"
	}
	return "no auth"
}

// finishDeleteLines tell the user how to finish a deletion whose
// bookkeeping did not complete.
func finishDeleteLines(name string) []string {
	return []string{
		fmt.Sprintf("Then run: claudewheel profile delete %s --no-force-delete --no-force-delete-data", name),
		"to finish removing it from options.json and state.json.",
	}
}

// deleteProfile deletes the focused profile after asking. A reserved name
// is refused first, with nothing gathered; a profile holding real data at a
// shared-store name is refused, pointing at the command that can delete
// it. Otherwise, after the confirmation, the deletion checklist stops what
// the user ticks among the sessions holding the profile, saferm is made
// available (see resolveArchiver), and a profile still held by a live
// interactive session is not deleted.
func (a *app) deleteProfile(ctx context.Context, seg *Segment) error {
	name, ok := seg.Value()
	if !ok {
		return nil
	}
	if reason, reserved := profiles.ReservedReason(name); reserved {
		return a.page(ctx, fmt.Sprintf("Cannot delete '%s'", name), wrapText(reason, pageWidth))
	}
	report, err := a.profiles.GatherReport(name, a.in.Now())
	if err != nil {
		return err
	}
	if report.Danger {
		var atRisk []string
		for _, e := range report.SharedDirs {
			if e.State == profiles.SharedRealDir {
				atRisk = append(atRisk, e.Name)
			}
		}
		slices.Sort(atRisk)
		lines := []string{"Shared-dir names holding REAL data (not symlinks):"}
		for _, d := range atRisk {
			lines = append(lines, "  "+d)
		}
		lines = append(lines,
			"",
			"Deleting this profile would destroy that data.",
			"The TUI offers no override. If you are certain, run:",
			fmt.Sprintf("  claudewheel profile delete %s --no-force-delete --force-delete-data", name),
		)
		return a.page(ctx, fmt.Sprintf("Cannot delete '%s'", name), lines)
	}

	facts := fmt.Sprintf("%s, %s, %d active sessions (%d interactive)",
		authSummary(report), profiles.FormatSize(report.DiskUsageBytes), report.ActiveSessions, report.InteractiveSessions)
	answer, err := widgets.Confirm(ctx, a.t, a.renderer.Colors, widgets.Confirmation{
		Title:   fmt.Sprintf("Delete profile '%s'?", name),
		Lines:   []string{facts},
		Accept:  "delete it",
		Decline: "keep it",
		Skip:    "keep it",
	})
	if err != nil || answer != widgets.Accept {
		return err
	}

	// Stop first, remove second: a Claude Code process still holding the
	// profile recreates its directory on its next start or write.
	var stillHolding []sessions.SessionRecord
	if report.ActiveSessions > 0 {
		outcome, err := a.in.Flows.DeletionChecklist(ctx, name)
		if err != nil || !outcome.Confirmed {
			return err
		}
		stillHolding = outcome.StillHolding
	}

	// Resolved after the checklist, so an install offer never comes before
	// a deletion the user may still cancel.
	tool, err := a.resolveArchiver(ctx, name)
	if err != nil || tool == nil {
		return err
	}

	if slices.ContainsFunc(stillHolding, func(r sessions.SessionRecord) bool { return r.Interactive() }) {
		a.flash = fmt.Sprintf("Not deleted: '%s' has a live interactive session", name)
		return nil
	}

	result, err := a.profiles.Delete(a.fx, name, tool, false)
	var unreadable *archiver.ArchiveUnreadableError
	var archiveErr *archiver.ArchiveError
	var bookkeeping *profiles.DeletionBookkeepingError
	switch {
	case errors.As(err, &unreadable):
		// The one archival failure after which the profile is gone: saferm
		// succeeded and its answer could not be read.
		lines := wrapText(unreadable.Error(), pageWidth)
		lines = append(lines, "")
		lines = append(lines, finishDeleteLines(name)...)
		a.flash = fmt.Sprintf("'%s' was archived, but its handle is unknown", name)
		return a.page(ctx, fmt.Sprintf("Deleted '%s' -- handle unreadable", name), lines)
	case errors.As(err, &archiveErr):
		a.flash = "Not deleted: " + err.Error()
		return nil
	case errors.As(err, &bookkeeping):
		lines := []string{
			"The profile directory was archived and removed, but",
			"claudewheel could not update its own records:",
			"",
		}
		lines = append(lines, wrapText(bookkeeping.Reason.Error(), pageWidth)...)
		lines = append(lines, "")
		if bookkeeping.Archive != nil {
			lines = append(lines,
				"Archive handle: "+bookkeeping.Archive.UUID,
				"",
				"Restore all of it with:",
				"  "+bookkeeping.Archive.RestoreCommand(),
			)
		} else {
			lines = append(lines, "Find the archive record with `saferm list`.")
		}
		lines = append(lines, "")
		lines = append(lines, finishDeleteLines(name)...)
		a.flash = fmt.Sprintf("'%s' archived, but its registration is stale", name)
		return a.page(ctx, fmt.Sprintf("Deleted '%s' -- registration not updated", name), lines)
	case err != nil:
		// Every other failure comes before the archival: nothing changed.
		a.flash = "Not deleted: " + err.Error()
		return nil
	}

	// The store removed the profile from state.json's last_config on disk;
	// drop it from the bar's copy too, so the next save keeps it removed.
	if a.store.State.LastConfig[keyProfile] == name {
		delete(a.store.State.LastConfig, keyProfile)
	}
	seg.State.RemovePinned(name)
	seg.State.DeleteMetadata(name)
	seg.ClearSelection()
	if err := refreshProfiles(seg, a.profiles); err != nil {
		return err
	}
	if len(stillHolding) > 0 {
		a.flash = fmt.Sprintf("Deleted profile '%s'; %d process(es) still hold it and may recreate the directory", name, len(stillHolding))
	} else {
		a.flash = fmt.Sprintf("Deleted profile '%s'", name)
	}
	if result.Archive == nil {
		return nil
	}
	// A page, not a flash: the handle is what makes the deletion
	// recoverable. It is reported, never stored; `saferm list` keeps it.
	return a.page(ctx, fmt.Sprintf("Deleted '%s' -- recoverable", name), []string{
		"The profile directory was archived before it was removed,",
		"its stored OAuth token included.",
		"",
		"Archive handle: " + result.Archive.UUID,
		"",
		"Restore all of it with:",
		"  " + result.Archive.RestoreCommand(),
		"",
		"It is listed by `saferm list` for as long as it is kept.",
	})
}

// installCommandLines lists the commands that install saferm, indented.
func installCommandLines() []string {
	var lines []string
	for _, c := range archiver.InstallCommands() {
		lines = append(lines, "  "+c)
	}
	return lines
}

// resolveArchiver returns the saferm the deletion of profile name archives
// with, or nil when the deletion stops. When saferm is missing or lacks a
// feature, the page says which and what deleting without it would cost; a
// preview offers nothing (it installs nothing), otherwise the install is
// offered. Declining, a failed install, and an installed saferm that still
// lacks a feature each stop the deletion.
func (a *app) resolveArchiver(ctx context.Context, name string) (*archiver.Tool, error) {
	root := a.store.Workspace().Root()
	tool, missing, err := archiver.Detect(a.fx, root)
	if err != nil || missing == nil {
		return tool, err
	}
	notDeleted := fmt.Sprintf("'%s' was not deleted", name)
	explain := func() []string {
		lines := wrapText(missing.Diagnosis(), pageWidth)
		lines = append(lines, "")
		return append(lines, wrapText(missing.Stakes(name), pageWidth)...)
	}

	if !archiver.MayOfferInstall(a.fx.Previewing(), true) {
		lines := append(explain(), "",
			"This is a preview (--dry-run), which installs nothing.",
			"Install it yourself with one of:")
		return nil, a.page(ctx, notDeleted, append(lines, installCommandLines()...))
	}

	answer, err := widgets.Confirm(ctx, a.t, a.renderer.Colors, widgets.Confirmation{
		Title:   fmt.Sprintf("Cannot delete '%s' without saferm", name),
		Lines:   explain(),
		Accept:  strings.ToLower(missing.Verb()) + " saferm from its published release",
		Decline: "cancel the deletion",
		Skip:    "cancel the deletion",
	})
	if err != nil {
		return nil, err
	}
	if answer != widgets.Accept {
		lines := append(explain(), "", "Install it yourself with one of:")
		return nil, a.page(ctx, notDeleted, append(lines, installCommandLines()...))
	}

	binary, err := archiver.Install(a.fx, root, nil)
	if err != nil {
		lines := append(wrapText("The install failed: "+err.Error(), pageWidth), "", "Install it yourself with one of:")
		return nil, a.page(ctx, notDeleted, append(lines, installCommandLines()...))
	}
	// Detect again: the deletion proceeds only against a saferm that
	// answered the probe.
	fresh, stillMissing, err := archiver.Detect(a.fx, root)
	if err != nil {
		return nil, err
	}
	if stillMissing != nil {
		lines := wrapText(fmt.Sprintf("saferm was installed at %s, but it still does not ship what claudewheel needs.", binary), pageWidth)
		lines = append(lines, "")
		return nil, a.page(ctx, notDeleted, append(lines, wrapText(stillMissing.Diagnosis(), pageWidth)...))
	}
	return fresh, nil
}

// sessionsOverview opens the machine-wide sessions overview.
func (a *app) sessionsOverview(ctx context.Context) error {
	found, err := a.profiles.Enumerate()
	if err != nil {
		return err
	}
	dirs := make([]sessions.ProfileConfigDir, len(found))
	for i, p := range found {
		dirs[i] = sessions.ProfileConfigDir{Name: p.Name, ConfigDir: p.Path}
	}
	src := sessionsview.Sources{
		Workspace: a.store.Workspace(),
		Profiles:  dirs,
		Memory: func(pids []int) (map[int]int64, error) {
			measured, err := profiles.ResidentMemory(a.fx, pids)
			if err != nil {
				return nil, err
			}
			out := make(map[int]int64, len(measured))
			for pid, kib := range measured {
				out[pid] = int64(kib)
			}
			return out, nil
		},
		Clock:    func() int64 { return a.in.Now().UnixMilli() },
		Identity: sessionsview.CurrentIdentity(os.LookupEnv),
		Home:     a.in.Home,
	}
	outcome, err := sessionsview.RunOverview(ctx, a.fx, a.t, a.renderer.Colors, src)
	if err != nil {
		return err
	}
	if len(outcome.Pruned) > 0 {
		a.flash = fmt.Sprintf("Pruned %d crashed record(s)", len(outcome.Pruned))
	}
	return nil
}
