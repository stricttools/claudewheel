package cli

import (
	"errors"
	"fmt"
	"os"
	"time"
	"unicode/utf8"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/auth"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/tokens"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/tui/wizard"
)

// registerProfile adds the profile group.
func registerProfile(app *strictcli.App) {
	g := app.Group("profile",
		"create, inspect, rename, delete, and manage Claude Code profiles and their stored tokens, and run commands in a profile's environment")

	mutatingCommand(g, "create",
		"run the create-profile wizard in one continuous alt-screen session: prompt for the profile name, config directory and launch options, write the profile directory together with its symlinks into the shared store, then drive an interactive Claude Code OAuth login so the profile is authenticated before you leave. Requires a real terminal, and prints the summary and auth outcome afterwards",
		handleProfileCreate,
		strictcli.WithGrants(strictcli.Grant{
			Name:   "auth-login",
			Reason: "the wizard drives an interactive Claude Code OAuth login for the new profile",
			Kind:   strictcli.ProcMutate,
		}))

	registerProfileDelete(g)

	readOnlyCommand(g, "show",
		"print a detailed report for one profile: whether its directory exists on disk, whether it is registered or pinned in options.json, the state of its stored token, its resolved configuration and the session data it holds. Inspects default (~/.claude) like any other profile, and exits non-zero when the name matches no directory, registration or token",
		handleProfileShow,
		strictcli.WithArgs(strictcli.NewArg("name",
			"name of the profile to inspect (e.g. work, personal, default)", strictcli.ArgRequired())))

	mutatingCommand(g, "rename",
		"move a profile to a new name, taking its directory (with the token stored inside it), its options.json registration and its session data with it. Validates that the old name exists, that the new one is free in both the directory tree and the options file, and that it fits the lowercase-letters-digits-hyphens charset. Refuses a profile holding a live interactive Claude Code session, and the reserved name default. A rename interrupted part way leaves a breadcrumb every other command refuses to work past; running the same rename again finishes it",
		handleProfileRename,
		strictcli.WithArgs(
			strictcli.NewArg("old", "current name of the profile to rename (must be an existing, non-running profile)", strictcli.ArgRequired()),
			strictcli.NewArg("new", "new name for the profile (lowercase letters, digits, and hyphens; must be unused)", strictcli.ArgRequired())))

	mutatingCommand(g, "fix-auth",
		"repair one profile's authentication: strip the session credentials that shadow its stored long-lived token so the token is used again. Says so plainly when there is nothing to repair, and refuses a name with no profile directory behind it",
		handleProfileFixAuth,
		strictcli.WithArgs(strictcli.NewArg("name",
			"name of the profile to repair: its shadowing session credentials are removed", strictcli.ArgRequired())))

	plans := make([]strictcli.ChoiceValue, 0, len(tokens.PlanTiers()))
	for _, plan := range tokens.PlanTiers() {
		stores := planFields(plan)
		if plan.RateLimitTier == "" {
			stores += ", no rate-limit tier"
		}
		plans = append(plans, strictcli.Ch(plan.Key, fmt.Sprintf("%s (%s)", plan.Label, stores)))
	}
	mutatingCommand(g, "set-plan",
		"declare which plan a profile's Claude account is on, without a "+
			"prompt. Claude Code resolves its subscription tier from the launch "+
			"environment and only from there when auth is a stored setup token, "+
			"so an undeclared profile launches with the tier null and "+
			"tier-dependent features failing closed. Writes both plan fields "+
			"into the profile's token entry, leaving the token itself alone; "+
			"the interactive picker in the create flow and the pre-launch "+
			"prompt write exactly the same thing",
		handleProfileSetPlan,
		strictcli.WithArgs(
			strictcli.NewArg("name", "name of the profile to declare a plan for (e.g. work, personal)", strictcli.ArgRequired()),
			strictcli.NewArg("plan",
				"the plan this profile's Claude account is on; each one stores a subscription type and, where Claude Code has one, a rate-limit tier",
				strictcli.ArgRequired(), strictcli.ArgChoices(plans...))))

	readOnlyCommand(g, "check-tokens",
		"read every discovered profile's own stored OAuth token and validate each one against the Anthropic API, then print a table of profile name, status and a truncated token preview. The status distinguishes a valid token from an invalid one, an unreachable API and an indeterminate answer, and profiles holding no token are listed too. Exits 1 when any stored token is not valid",
		handleProfileCheckTokens)

	mutatingCommand(g, "exec",
		"run a command in a profile's launch environment by replacing this process with it (exec: pipes, the process id, and signals pass straight through), for programs that start Claude Code themselves. The environment is the one a launch of the profile gets: CLAUDE_CONFIG_DIR, the profile's stored OAuth token, its declared plan tier, and the switches a launch sets; a variable of that set the profile does not set is removed, and for default every one of them is removed. An unknown profile is refused, listing the profiles. Prints nothing on success. The command follows a bare --, e.g. claudewheel profile exec --name work -- claude -p hello",
		handleProfileExec,
		strictcli.WithDryRunUnsupported("it replaces this process with the command it is given, so nothing would be left to show what the command did"),
		strictcli.WithFlags(strictcli.StringFlag("name",
			"the profile whose launch environment the command runs in (default is Claude Code's own ~/.claude)",
			strictcli.Required())),
		strictcli.WithArgs(strictcli.NewArg("argv",
			"the command to run and its arguments, after a bare -- so its own flags are not read as this command's",
			strictcli.ArgRequired(), strictcli.Variadic())))
}

// planFields lists the token entry fields plan writes.
func planFields(plan tokens.PlanTier) string {
	fields := tokens.SubscriptionField + "=" + plan.SubscriptionType
	if plan.RateLimitTier != "" {
		fields += ", " + tokens.RateLimitField + "=" + plan.RateLimitTier
	}
	return fields
}

// handleProfileCreate runs the wizard on the alternate screen, then prints
// what it did once the terminal is restored. An error after the profile was
// made still prints the summary so far.
func handleProfileCreate(c *call, kw map[string]interface{}) error {
	// Refused before the workspace is opened, which may write first-run files.
	if err := widgets.RequireTerminal("profile create"); err != nil {
		return err
	}
	cfg, err := c.appConfig()
	if err != nil {
		return err
	}
	ctx, stop := c.signalContext()
	defer stop()
	t, colors, err := widgets.OpenScreen(ctx, c.ws, cfg.Config.Theme, "profile create")
	if err != nil {
		return err
	}
	// Close is idempotent: the deferred one restores the terminal when the
	// wizard ends a --dry-run preview by panicking at the login's result.
	defer t.Close()
	summary, runErr := wizard.RunCreate(ctx, c.fx, t, colors, c.ws)
	closeErr := t.Close()
	if runErr == nil || len(summary.Lines) > 0 {
		for _, line := range summary.Report() {
			c.say(line)
		}
	}
	return errors.Join(runErr, closeErr)
}

func handleProfileShow(c *call, kw map[string]interface{}) error {
	name := kwString(kw, "name")
	if _, err := c.appConfig(); err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	report, err := store.GatherReport(name, time.Now())
	if err != nil {
		return err
	}
	if !report.Exists && !report.Registered && !report.Pinned && !report.HasToken {
		return fmt.Errorf("Profile '%s' not found: no profile directory, no options.json registration, no token.", name)
	}
	for _, line := range profiles.FormatReport(report) {
		c.say(line)
	}
	return nil
}

// handleProfileRename does not go through profileStore: a leftover rename
// breadcrumb is what a rerun of the same rename finishes.
func handleProfileRename(c *call, kw map[string]interface{}) error {
	oldName, newName := kwString(kw, "old"), kwString(kw, "new")
	if _, err := c.appConfig(); err != nil {
		return err
	}
	resumed, err := profiles.New(c.ws).Rename(c.fx, oldName, newName)
	if err != nil {
		return err
	}
	switch {
	case resumed && c.previewing():
		c.sayf("Would finish the interrupted rename of profile '%s' -> '%s'.", oldName, newName)
	case resumed:
		c.sayf("Finished the interrupted rename of profile '%s' -> '%s'.", oldName, newName)
	case c.previewing():
		c.sayf("Would rename profile '%s' -> '%s'.", oldName, newName)
	default:
		c.sayf("Renamed profile '%s' -> '%s'.", oldName, newName)
	}
	return nil
}

func handleProfileFixAuth(c *call, kw map[string]interface{}) error {
	name := kwString(kw, "name")
	if _, err := c.appConfig(); err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	outcome, err := store.FixAuth(c.fx, name)
	if err != nil {
		return err
	}
	switch outcome {
	case profiles.FixAuthNoToken:
		return fmt.Errorf("No long-lived token for '%s', nothing to fix.", name)
	case profiles.FixAuthNoShadow:
		c.sayf("No auth shadow detected for '%s'.", name)
		return nil
	}
	verb := "Removed"
	if c.previewing() {
		verb = "Would remove"
	}
	c.sayf("%s session credentials from %s. Long-lived token will now be used.", verb, name)
	return nil
}

func handleProfileSetPlan(c *call, kw map[string]interface{}) error {
	name := kwString(kw, "name")
	if _, err := c.appConfig(); err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	plan, err := store.SetPlan(c.fx, name, kwString(kw, "plan"))
	if err != nil {
		return err
	}
	verb := "Declared"
	if c.previewing() {
		verb = "Would declare"
	}
	c.sayf("%s plan %s for '%s' (%s).", verb, plan.Label, name, planFields(plan))
	return nil
}

// tokenRow is one line of the check-tokens table.
type tokenRow struct {
	profile, status, preview string
}

func handleProfileCheckTokens(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	stored, err := store.StoredTokens()
	if err != nil {
		return err
	}
	if len(stored) == 0 {
		c.say("No profiles found.")
		return nil
	}
	header := tokenRow{"Profile", "Status", "Token"}
	rows := make([]tokenRow, 0, len(stored))
	anyBad := false
	for _, s := range stored {
		if s.Token == "" {
			rows = append(rows, tokenRow{s.Profile, "no token", "-"})
			continue
		}
		status, err := auth.ValidateToken(c.fx, s.Token)
		if err != nil {
			return err
		}
		if status != auth.Valid {
			anyBad = true
		}
		rows = append(rows, tokenRow{s.Profile, string(status), profiles.TokenPreview(s.Token)})
	}
	widths := [3]int{}
	for _, r := range append([]tokenRow{header}, rows...) {
		for i, cell := range [3]string{r.profile, r.status, r.preview} {
			widths[i] = max(widths[i], utf8.RuneCountInString(cell))
		}
	}
	for _, r := range append([]tokenRow{header}, rows...) {
		c.sayf("%-*s  %-*s  %-*s", widths[0], r.profile, widths[1], r.status, widths[2], r.preview)
	}
	if anyBad {
		return exitStatus(1)
	}
	return nil
}

// handleProfileExec replaces this process with the command, in the
// profile's launch environment. The token is redacted from every error and
// record; on success nothing returns here.
func handleProfileExec(c *call, kw map[string]interface{}) error {
	name := kwString(kw, "name")
	argv := kwStrings(kw, "argv")
	if _, err := c.appConfig(); err != nil {
		return err
	}
	store, err := c.profileStore()
	if err != nil {
		return err
	}
	env, err := store.LaunchEnv(name)
	if err != nil {
		return err
	}
	cwd, err := os.Getwd()
	if err != nil {
		return err
	}
	cmd := effects.Cmd{Argv: argv, Dir: cwd, Env: env.Apply(os.Environ())}
	if env.Token != "" {
		cmd.Redact = []string{env.Token}
	}
	return c.fx.Exec(cmd)
}
