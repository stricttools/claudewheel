package cli

import (
	"strings"
	"time"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/health"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/reconcile"
)

// registerMaintenance adds the commands that check and repair the managed
// profiles as a whole: health, patch-profiles, and purge-plugins.
func registerMaintenance(app registrar) {
	readOnlyCommand(app, "health",
		"run diagnostic health checks on profiles, tokens, and hooks, print one line per check, and exit 1 when any check is not OK",
		handleHealth)

	// The reconcile is exact: it prunes hand-authored permission rules, hook
	// entries, and disallowedTools drift, with nothing backed up and nothing
	// that reconstructs a pruned entry, which is what makes it consequential.
	patchTarget := newProfileTarget("reconcile every managed profile and shared-settings.json")
	mutatingCommand(app, "patch-profiles",
		"reconcile one managed profile (--profile) or every managed profile and shared-settings.json (--all-profiles) to EXACTLY the canonical guardrail model (hooks, disallowedTools, permissions deny/ask and the canonical settings keys made exact; allow keeps only its non-conflicting entries); prunes drift and user-added extras. Only --all-profiles touches shared-settings.json. Deploys any missing guardrail hook scripts. The 'default' profile (~/.claude) is never touched and cannot be named. Preview the per-target diff with --dry-run; writing needs a terminal to confirm at, or --approve-consequential",
		func(c *call, kw map[string]interface{}) error {
			profile, all := patchTarget.read(kw)
			sel := reconcile.OneProfile(profile)
			if all {
				sel = reconcile.AllProfiles()
			}
			return patchProfiles(c, sel)
		},
		strictcli.WithConsequential(),
		strictcli.WithFlags(patchTarget.flag))

	purgeTarget := newProfileTarget("target every registered profile at once")
	mutatingCommand(app, "purge-plugins",
		"remove the Claude Code plugin tree from the selected profiles: the"+
			" official-marketplace clone and every plugin installed from it, six"+
			" to ten megabytes per profile. Opt-in and separate from the"+
			" canonical reconciliation, which is exact and would otherwise"+
			" delete plugin state on every run. Names the marketplaces and"+
			" plugins it finds before removing them; --dry-run reports the"+
			" inventory without touching anything. New launches do not collect"+
			" a new tree -- the launch environment suppresses the auto-install,"+
			" one-way per profile. The 'default' profile (~/.claude) is never"+
			" touched",
		func(c *call, kw map[string]interface{}) error {
			profile, all := purgeTarget.read(kw)
			return purgePlugins(c, profile, all)
		},
		strictcli.WithFlags(purgeTarget.flag))
}

func handleHealth(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	executable, err := ownExecutable()
	if err != nil {
		return err
	}
	results := health.Run(health.Inputs{FX: c.fx, Workspace: c.ws, Executable: executable, Today: time.Now()})
	for _, r := range results {
		c.say(r.Line())
	}
	if !health.AllOK(results) {
		return exitStatus(1)
	}
	return nil
}

// patchProfiles reconciles the selection and prints the report. A failed
// write stops the run: what was done before it is printed, then the error.
// A selection refused before anything ran prints no report.
func patchProfiles(c *call, sel reconcile.Selection) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	report, err := reconcile.Run(c.fx, c.ws, sel)
	if err == nil || len(report.Targets) > 0 {
		for _, line := range report.Lines() {
			c.say(line)
		}
	}
	return err
}

func purgePlugins(c *call, profile string, all bool) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	targets, err := store.PluginTargets(profile, all)
	if err != nil {
		return err
	}
	verb := "removed"
	if c.previewing() {
		verb = "would remove"
	}
	purged := 0
	var freed int64
	for _, p := range targets {
		found, err := profiles.InventoryPlugins(p.Path)
		if err != nil {
			return err
		}
		if !found.Exists {
			c.sayf("%s: no plugin tree", p.Name)
			continue
		}
		c.sayf("%s: %s %s", p.Name, verb, profiles.FormatSize(found.SizeBytes))
		if len(found.Marketplaces) > 0 {
			c.say("  marketplaces: " + strings.Join(found.Marketplaces, ", "))
		}
		if len(found.Plugins) > 0 {
			c.say("  plugins: " + strings.Join(found.Plugins, ", "))
		}
		if _, err := profiles.PurgePlugins(c.fx, p.Path); err != nil {
			return err
		}
		purged++
		freed += found.SizeBytes
	}
	if purged == 0 {
		c.say("Nothing to purge.")
		return nil
	}
	total := "Freed"
	if c.previewing() {
		total = "Would free"
	}
	c.sayf("%s %s across %d profile(s).", total, profiles.FormatSize(freed), purged)
	return nil
}
