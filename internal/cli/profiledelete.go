package cli

import (
	"context"
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/archiver"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/deletion"
	"github.com/stricttools/claudewheel/internal/tui/sessionsview"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// registerProfileDelete adds profile delete. It is consequential: the
// archival makes the deletion recoverable, not harmless, since the profile
// stops existing, every process holding it loses its configuration
// directory, and putting it back is a second deliberate operation.
func registerProfileDelete(g registrar) {
	mutatingCommand(g, "delete",
		"remove a registered profile: hand its directory to saferm, which archives it (the stored token among it) and then removes it, unlink its shared-store symlinks, drop its options.json registration, and clear any last_config reference in state.json. Prints the archive handle that restores it. At a terminal (outside --dry-run) the deletion checklist first lists every process holding the profile and stops the ones ticked (the daemon and its workers come ticked), and a missing saferm is offered for install. Refuses a profile still holding a live interactive Claude Code session unless --force-delete (background jobs and daemons do not block it), and takes conversation history only with --force-delete-data. saferm must be installed: without it the deletion would be irreversible, so it is refused rather than performed",
		handleProfileDelete,
		strictcli.WithConsequential(),
		strictcli.WithGrants(strictcli.Grant{
			Name:   "archive-delegation",
			Reason: "hands the whole profile directory, its stored OAuth token included, to saferm, which archives it and then removes it",
			Kind:   strictcli.ProcMutate,
		}),
		strictcli.WithArgs(strictcli.NewArg("name",
			"name of the profile to delete (e.g. work, personal, research)", strictcli.ArgRequired())),
		strictcli.WithFlags(
			strictcli.BoolFlag("force-delete",
				"delete anyway when the profile holds a live interactive Claude Code session (at a terminal: one still running after the deletion checklist); background jobs and daemons never block deletion",
				strictcli.Required()),
			strictcli.BoolFlag("force-delete-data",
				"delete even when shared-dir names hold REAL data instead of symlinks; this DESTROYS that data (e.g. conversation history)",
				strictcli.Required())))
}

// deletionAnswers is what the screen shown before a deletion decided.
type deletionAnswers struct {
	// checklistAccepted is false when the checklist was answered with
	// anything but y: nothing was stopped and nothing is deleted.
	checklistAccepted bool
	// stillHolding are the processes holding the profile once the screen
	// closed, probed again.
	stillHolding []sessions.SessionRecord
	failed       []deletion.StopFailure
	// interactiveLeft reports a live interactive session still holding the
	// profile without --force-delete: the install offer was not shown.
	interactiveLeft bool
	// installAccepted reports that the saferm install offer was accepted.
	installAccepted bool
}

func handleProfileDelete(c *call, kw map[string]interface{}) error {
	name := kwString(kw, "name")
	forceDelete := kwBool(kw, "force_delete")
	forceData := kwBool(kw, "force_delete_data")
	// A name claudewheel does not own has no holders worth listing and no
	// deletion to describe.
	if reason, reserved := profiles.ReservedReason(name); reserved {
		return errors.New(reason)
	}
	cfg, err := c.appConfig()
	if err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	configDir := store.PathFor(name)
	atTerminal := terminal.HasControllingTerminal()
	onScreen := atTerminal && !c.previewing()

	// Without the screen, the holders are read while the registry still
	// exists (it lives inside the directory this removes) and only named.
	var holding []sessions.SessionRecord
	if !onScreen {
		holding = sessions.LiveRecords(configDir)
		if !forceDelete && slices.ContainsFunc(holding, sessions.SessionRecord.Interactive) {
			return interactiveRefusal(name)
		}
	}

	tool, missing, err := archiver.Detect(c.fx, c.ws.Root())
	if err != nil {
		return err
	}
	if missing != nil && !archiver.MayOfferInstall(c.previewing(), atTerminal) {
		// RefusalError carries its own "error: " prefix.
		fmt.Fprintln(os.Stderr, missing.RefusalError(name, c.previewing()))
		return exitStatus(1)
	}

	if onScreen {
		holders, err := deletion.GatherHolders(c.fx, configDir)
		if err != nil {
			return err
		}
		if len(holders) > 0 || missing != nil {
			answers, err := askBeforeDeleting(c, cfg, name, configDir, holders, missing, forceDelete)
			for _, f := range answers.failed {
				c.sayf("Could not stop %s: %s", holderLabel(f.Record), stopFailureReason(f))
			}
			if err != nil {
				return err
			}
			if !answers.checklistAccepted {
				return fmt.Errorf("the deletion checklist was cancelled: profile '%s' was not deleted", name)
			}
			if answers.interactiveLeft {
				return interactiveRefusal(name)
			}
			holding = answers.stillHolding
			if missing != nil {
				if !answers.installAccepted {
					c.sayf("Declined. Profile '%s' was not deleted.", name)
					c.say("Install saferm yourself with one of:")
					c.say(missing.Fix())
					return exitStatus(1)
				}
				if tool, err = installSaferm(c, name); err != nil {
					return err
				}
			}
		}
	}

	result, err := store.Delete(c.fx, name, tool, forceData)
	var unreadable *archiver.ArchiveUnreadableError
	if errors.As(err, &unreadable) {
		// The one archival failure after which the directory is gone: saferm
		// succeeded and its answer could not be read, so the registration
		// is stale too, and the same deletion finishes that cleanup.
		c.fail(unreadable.Error())
		c.fail(fmt.Sprintf("claudewheel's own registration was not updated: run `claudewheel profile delete %s --no-force-delete --no-force-delete-data` again to finish it, which archives nothing because the directory is already gone.", name))
		return exitStatus(1)
	}
	if err != nil {
		return err
	}
	printDeletion(c, name, result, holding, atTerminal)
	return nil
}

// interactiveRefusal is the error of a deletion refused for a live
// interactive session.
func interactiveRefusal(name string) error {
	return fmt.Errorf("Profile '%s' has a live interactive session. Use --force-delete to delete anyway.", name)
}

// askBeforeDeleting shows the deletion checklist over holders (when there
// are any), then, when saferm is missing, the offer to install it, on one
// alternate-screen session. The offer comes last, so it never comes before
// a deletion the user may still cancel.
func askBeforeDeleting(c *call, cfg *appconfig.Store, name, configDir string, holders []deletion.Holder, missing *archiver.Unavailable, forceDelete bool) (deletionAnswers, error) {
	ctx, stop := c.signalContext()
	defer stop()
	t, colors, err := c.openScreen(ctx, cfg, "the deletion checklist and the saferm install offer of profile delete")
	if err != nil {
		return deletionAnswers{}, err
	}
	// Close is idempotent: the deferred one restores the terminal on a panic.
	defer t.Close()
	answers, err := askOnScreen(ctx, c, t, colors, name, configDir, holders, missing, forceDelete)
	return answers, errors.Join(err, t.Close())
}

func askOnScreen(ctx context.Context, c *call, t *terminal.Terminal, colors widgets.Colors, name, configDir string, holders []deletion.Holder, missing *archiver.Unavailable, forceDelete bool) (deletionAnswers, error) {
	answers := deletionAnswers{checklistAccepted: true}
	if len(holders) > 0 {
		outcome, err := deletion.Run(ctx, c.fx, t, colors, holders, deletion.Checklist{
			ProfileName: name,
			ConfigDir:   configDir,
			Binary:      install.LocatorFor(c.ws).Fallback(),
			Environ:     os.Environ(),
			NowMS:       time.Now().UnixMilli(),
			Identity:    sessionsview.CurrentIdentity(os.LookupEnv),
		})
		answers.failed = outcome.Failed
		answers.stillHolding = outcome.StillHolding
		if err != nil {
			return answers, err
		}
		if !outcome.Confirmed() {
			answers.checklistAccepted = false
			return answers, nil
		}
	}
	if !forceDelete && slices.ContainsFunc(answers.stillHolding, sessions.SessionRecord.Interactive) {
		answers.interactiveLeft = true
		return answers, nil
	}
	if missing == nil {
		return answers, nil
	}
	answer, err := widgets.Confirm(ctx, t, colors, deletion.SafermInstallOffer(missing, name))
	if err != nil {
		return answers, err
	}
	answers.installAccepted = answer == widgets.Accept
	return answers, nil
}

// installSaferm installs saferm after an accepted offer and detects it
// again: the deletion proceeds only against a saferm that answered the
// probe. A failed install, or one that still lacks a feature, stops the
// deletion; nothing falls back to removing the directory another way.
func installSaferm(c *call, name string) (*archiver.Tool, error) {
	c.info("Installing saferm from its published release...")
	binary, err := archiver.Install(c.fx, c.ws.Root(), nil)
	if err != nil {
		return nil, fmt.Errorf("installing saferm failed, so profile '%s' was not deleted: %w", name, err)
	}
	c.sayf("Installed saferm at %s.", binary)
	tool, missing, err := archiver.Detect(c.fx, c.ws.Root())
	if err != nil {
		return nil, err
	}
	if missing != nil {
		return nil, fmt.Errorf("the saferm just installed still does not ship what claudewheel needs (%s), so profile '%s' was not deleted", missing.Diagnosis(), name)
	}
	return tool, nil
}

// printDeletion reports a deletion that went through, with the processes
// still holding the profile and the handle that restores it.
func printDeletion(c *call, name string, result profiles.DeletionResult, holding []sessions.SessionRecord, atTerminal bool) {
	previewing := c.previewing()
	pick := func(live, preview string) string {
		if previewing {
			return preview
		}
		return live
	}
	c.sayf("%s profile '%s'...", pick("Deleting", "Would delete"), name)
	c.sayf("  %s dir: %d symlinks unlinked, %d real entries removed",
		pick("Removed", "Would remove"), result.RemovedSymlinks, result.RemovedReal)
	if result.RemovedFromOptions {
		c.sayf("  %s from options.json", pick("Removed", "Would remove"))
	} else {
		c.say("  Not found in options.json (already clean)")
	}
	if result.LastConfigPurged {
		c.sayf("  %s last_config profile reference in state.json", pick("Cleared", "Would clear"))
	}
	if len(holding) > 0 {
		labels := make([]string, len(holding))
		for i, r := range holding {
			labels[i] = holderLabel(r)
		}
		advice := "stop them"
		if !atTerminal {
			advice += ", or delete the profile at a terminal, where the deletion checklist offers to stop them for you"
		}
		c.sayf("  %d process(es) still hold this profile: %s. They carry CLAUDE_CONFIG_DIR and will recreate the directory on their next write -- %s.",
			len(holding), strings.Join(labels, ", "), advice)
	}
	switch {
	case previewing:
		c.sayf("Would delete profile '%s'.", name)
	case len(holding) > 0:
		c.sayf("Profile '%s' deleted, but %d process(es) still hold it.", name, len(holding))
	default:
		c.sayf("Profile '%s' deleted.", name)
	}
	if result.Archive != nil {
		// Reported, never recorded: saferm's archive keeps its own record,
		// listed by `saferm list`.
		c.sayf("  Archived as %s", result.Archive.UUID)
		c.sayf("  Restore it with: %s", result.Archive.RestoreCommand())
	}
}

// holderLabel names a process holding a profile: its session name, or its
// session category when it has none, and its pid.
func holderLabel(r sessions.SessionRecord) string {
	label := r.Name
	if label == "" {
		label = r.Category
	}
	return fmt.Sprintf("%s (pid %d)", label, r.PID)
}

// stopFailureReason says why a ticked holder was not stopped.
func stopFailureReason(f deletion.StopFailure) string {
	if f.Err != nil {
		return f.Err.Error()
	}
	return "it was still running when the stop ended"
}
