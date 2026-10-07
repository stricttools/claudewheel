package wizard

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/stricttools/claudewheel/internal/appconfig"
	"github.com/stricttools/claudewheel/internal/auth"
	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/pathstat"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tokens"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// AuthOutcome is how an auth flow ended.
type AuthOutcome string

const (
	// AuthAuthenticated: the login completed, or a token was validated
	// against the API and saved.
	AuthAuthenticated AuthOutcome = "authenticated"
	// AuthUnverified: the API could not be asked and the user chose to
	// save the token without validation.
	AuthUnverified AuthOutcome = "unverified"
	// AuthSkipped: the user chose to skip.
	AuthSkipped AuthOutcome = "skip"
	// AuthCancelled: the user cancelled a choice with Escape.
	AuthCancelled AuthOutcome = "cancel"
	// AuthFailed: auth was attempted and did not complete.
	AuthFailed AuthOutcome = "failed"
)

// AuthResult is an auth flow's outcome and the lines saying what happened,
// in order, for the caller to show once the screen is gone. No line ever
// holds a token.
type AuthResult struct {
	Outcome AuthOutcome
	Notes   []string
}

// The auth method choices.
const (
	methodSession = "session"
	methodToken   = "token"
	methodPaste   = "paste"
	methodSkip    = "skip"
)

// copyBrowser is the browser choice that opens no browser: Claude Code runs
// $BROWSER with the URL, and BROWSER=false makes that fail, so it prints the
// URL to copy instead.
const copyBrowser = "copy"

// authLoginGrant is the grant the profile create command declares for the
// login it runs.
const authLoginGrant = "auth-login"

// The messages of the token steps.
const (
	pasteManuallyPrompt  = "Paste the token manually, or press Enter to abort"
	pasteCorrectedPrompt = "Paste the corrected token, or press Enter to abort"
	noTokenProvided      = "No token provided."
	notATokenMessage     = "Error: that does not look like an API token (expected an 'sk-ant-' prefix)."
	stillNotATokenNote   = "Error: that still does not look like an API token; aborting."
	rejectedMessage      = "Error: the token was rejected by the API (401)."
	rejectedAgainNote    = "Error: the token was rejected by the API (401) again."
	savedNote            = "Token validated and saved successfully."
	savedUnverifiedNote  = "Token saved WITHOUT validation."
)

// PickPlan asks which plan a profile's account is on: one list, one choice,
// both stored fields, so the creation flow, the pre-launch prompt, and the
// set-plan command can only store pairings this list has. It reports false
// when the user cancels.
func PickPlan(ctx context.Context, t *terminal.Terminal, c widgets.Colors, title string) (tokens.PlanTier, bool, error) {
	tiers := tokens.PlanTiers()
	options := make([]widgets.Option, len(tiers))
	for i, p := range tiers {
		options[i] = widgets.Option{Key: p.Key, Label: p.Label}
	}
	key, ok, err := widgets.RunSelection(ctx, t, c, title, options, "")
	if err != nil || !ok {
		return tokens.PlanTier{}, false, err
	}
	plan, err := tokens.PlanByKey(key)
	if err != nil {
		return tokens.PlanTier{}, false, err
	}
	return plan, true, nil
}

// authRun is one run of the auth flow.
type authRun struct {
	ctx       context.Context
	fx        *effects.FX
	t         *terminal.Terminal
	c         widgets.Colors
	ws        workspace.Workspace
	store     profiles.Store
	profile   string
	configDir string
	notes     []string
}

func (a *authRun) note(line string) {
	a.notes = append(a.notes, line)
}

func (a *authRun) result(o AuthOutcome) AuthResult {
	return AuthResult{Outcome: o, Notes: a.notes}
}

// RunAuthFlow sets up authentication for profile: a session login in the
// browser, a long-lived token from `claude setup-token`, a pasted token, or
// skipping (skipLabel names what skipping means to the caller). Both token
// methods ask for the plan first, since a stored token is the case where
// Claude Code cannot work its plan out itself. The browser the last
// successful auth used is focused first and remembered again on success.
//
// Every token is checked for its shape before any network call and validated
// against the API before it is saved; a token the API rejects is never
// saved. A failed attempt is AuthFailed with notes saying why, never an
// error: the profile already exists, and auth can be done later. The error
// is for the terminal, the workspace files, and ctx being done (its
// cancellation cause). The terminal must be in cbreak mode; the logins run in
// a cooked window under a pseudo-terminal.
func RunAuthFlow(ctx context.Context, fx *effects.FX, t *terminal.Terminal, c widgets.Colors, ws workspace.Workspace, profile, skipLabel string) (AuthResult, error) {
	store := profiles.New(ws)
	a := &authRun{ctx: ctx, fx: fx, t: t, c: c, ws: ws, store: store, profile: profile, configDir: store.PathFor(profile)}

	method, ok, err := widgets.RunSelection(ctx, t, c, fmt.Sprintf("Authenticate profile '%s'", profile), []widgets.Option{
		{Key: methodSession, Label: "Session login (recommended)"},
		{Key: methodToken, Label: "Long-lived token"},
		{Key: methodPaste, Label: "Paste token directly"},
		{Key: methodSkip, Label: skipLabel},
	}, "")
	if err != nil {
		return AuthResult{}, err
	}
	if !ok {
		return a.result(AuthCancelled), nil
	}
	if method == methodSkip {
		return a.result(AuthSkipped), nil
	}

	var plan tokens.PlanTier
	if method == methodToken || method == methodPaste {
		plan, ok, err = PickPlan(ctx, t, c, fmt.Sprintf("Plan for profile '%s'", profile))
		if err != nil {
			return AuthResult{}, err
		}
		if !ok {
			return a.result(AuthCancelled), nil
		}
	}

	var outcome AuthOutcome
	browser := ""
	if method == methodPaste {
		outcome, err = a.pasteToken(plan)
	} else {
		browser, ok, err = a.chooseBrowser()
		if err != nil {
			return AuthResult{}, err
		}
		if !ok {
			return a.result(AuthCancelled), nil
		}
		if method == methodSession {
			outcome, err = a.sessionLogin(browser)
		} else {
			outcome, err = a.longLivedToken(browser, plan)
		}
	}
	if err != nil {
		return a.result(outcome), err
	}

	if outcome == AuthAuthenticated || outcome == AuthUnverified {
		// The browser step worked, even when the token went unverified.
		if method != methodPaste {
			if err := appconfig.SetAuthBrowser(fx, ws, browser); err != nil {
				return a.result(outcome), err
			}
		}
		if err := setOnboardingFlag(fx, a.configDir); err != nil {
			return a.result(outcome), err
		}
	}
	return a.result(outcome), nil
}

// chooseBrowser asks which browser opens the auth URL, focusing the one the
// last successful auth used.
func (a *authRun) chooseBrowser() (string, bool, error) {
	state, err := appconfig.ReadState(a.ws)
	if err != nil {
		return "", false, err
	}
	remembered := ""
	if state.AuthBrowser != nil {
		remembered = *state.AuthBrowser
	}
	browsers, err := DetectBrowsers(SystemBrowserDirs(homeOf(a.ws)))
	if err != nil {
		return "", false, err
	}
	options := make([]widgets.Option, 0, len(browsers)+1)
	for _, b := range browsers {
		options = append(options, widgets.Option{Key: b.Path, Label: b.Name})
	}
	options = append(options, widgets.Option{Key: copyBrowser, Label: "Copy URL instead"})
	// A remembered browser no longer installed focuses the first option.
	return widgets.RunSelection(a.ctx, a.t, a.c, "Choose browser", options, remembered)
}

// claudeBinary finds the Claude Code binary: the target of the managed
// claude link when the link exists, and claude on PATH otherwise. A link
// that leads to no regular file is reported, not stepped over.
func claudeBinary(ws workspace.Workspace) (string, string) {
	link := install.LocatorFor(ws).Fallback()
	if _, err := os.Lstat(link); err == nil {
		resolved, err := filepath.EvalSymlinks(link)
		if err != nil {
			return "", fmt.Sprintf("Error: %s does not lead to a Claude Code binary: %v", link, err)
		}
		info, err := os.Stat(resolved)
		if err != nil || !info.Mode().IsRegular() {
			return "", fmt.Sprintf("Error: %s does not lead to a Claude Code binary file (%s).", link, resolved)
		}
		return resolved, ""
	} else if !pathstat.NotFoundOrNotDirectory(err) {
		return "", fmt.Sprintf("Error: cannot check %s: %v", link, err)
	}
	path, ok, err := lookPath(os.Getenv("PATH"), "claude")
	if err != nil {
		return "", fmt.Sprintf("Error: cannot search PATH for claude: %v", err)
	}
	if !ok {
		return "", "Error: Claude binary not found. Install it or add it to PATH."
	}
	return path, ""
}

// runLogin runs argv under a pseudo-terminal proxying the real one, in a
// cooked window outside the alternate screen, with CLAUDE_CONFIG_DIR set to
// the profile and BROWSER set from the choice. runErr is the child's failure
// to run, which ends the attempt; err is the terminal's, or ctx being done.
func (a *authRun) runLogin(argv []string, browser, resource string) (result effects.Result, runErr, err error) {
	browserVar := browser
	if browser == copyBrowser {
		browserVar = "false"
	}
	env := profiles.LaunchEnv{Set: map[string]string{
		profiles.ConfigDirVar: a.configDir,
		"BROWSER":             browserVar,
	}}.Apply(os.Environ())
	err = a.t.Cooked(func() error {
		if browser == copyBrowser {
			if err := a.t.Write("Browser opening suppressed -- copy the URL shown below.\r\n"); err != nil {
				return err
			}
		}
		result, runErr = a.fx.RunPTY(effects.Cmd{
			Argv:     argv,
			Env:      env,
			Resource: resource,
			Grant:    authLoginGrant,
		}, effects.PTY{ProxyTerminal: true})
		return nil
	})
	if err == nil && a.ctx.Err() != nil {
		err = context.Cause(a.ctx)
	}
	return result, runErr, err
}

// sessionLogin runs `claude auth login`. Nothing of Claude Code's credential
// file is copied into claudewheel's store: the profile launches on those
// credentials, and Claude Code reads its tier from them.
func (a *authRun) sessionLogin(browser string) (AuthOutcome, error) {
	binary, problem := claudeBinary(a.ws)
	if problem != "" {
		a.note(problem)
		return AuthFailed, nil
	}
	result, runErr, err := a.runLogin([]string{binary, "auth", "login"}, browser, "profile-credentials:"+a.configDir)
	if err != nil {
		return AuthFailed, err
	}
	if runErr != nil {
		a.note("Error running claude auth login: " + runErr.Error())
		return AuthFailed, nil
	}
	// Under --dry-run nothing ran, and reading the exit code ends the
	// preview here.
	if result.ExitCode() != 0 {
		a.note("Auth login exited with an error.")
		return AuthFailed, nil
	}
	_, statErr := os.Stat(filepath.Join(a.configDir, profiles.CredentialsFileName))
	switch {
	case statErr == nil:
		a.note("Authentication successful.")
		return AuthAuthenticated, nil
	case pathstat.NotFoundOrNotDirectory(statErr):
		a.note("Authentication did not complete (.credentials.json not found).")
		return AuthFailed, nil
	}
	return AuthFailed, statErr
}

// entryTitle is the title of the token entry pages.
func (a *authRun) entryTitle() string {
	return fmt.Sprintf("Token for profile '%s'", a.profile)
}

// askToken asks for a token on the entry page, showing errMsg from the step
// before. Escape and an empty answer both return "".
func (a *authRun) askToken(prompt, errMsg string) (string, error) {
	token, err := readToken(a.ctx, a.t, a.c, tokenEntry{title: a.entryTitle(), prompt: prompt, errMsg: errMsg})
	if errors.Is(err, errEntryCancelled) {
		return "", nil
	}
	return token, err
}

// formatGate returns a token of the expected shape, asking once for a
// corrected one; it never makes a network call. It reports false, with the
// reason noted, when no token of that shape was given.
func (a *authRun) formatGate(token string) (string, bool, error) {
	if auth.LooksLikeToken(token) {
		return token, true, nil
	}
	a.note(notATokenMessage)
	retyped, err := a.askToken(pasteCorrectedPrompt, notATokenMessage)
	if err != nil {
		return "", false, err
	}
	if retyped == "" {
		a.note(noTokenProvided)
		return "", false, nil
	}
	if auth.LooksLikeToken(retyped) {
		return retyped, true, nil
	}
	a.note(stillNotATokenNote)
	return "", false, nil
}

// captureSetupToken runs `claude setup-token` and takes the token from what
// it printed. When no token can be found there, that is said plainly and a
// paste is offered as the recovery, never a silent switch.
func (a *authRun) captureSetupToken(browser string) (string, bool, error) {
	binary, problem := claudeBinary(a.ws)
	if problem != "" {
		a.note(problem)
		return "", false, nil
	}
	result, runErr, err := a.runLogin([]string{binary, "setup-token"}, browser, "profile-token:"+a.configDir)
	if err != nil {
		return "", false, err
	}
	if runErr != nil {
		a.note("Error running claude setup-token: " + runErr.Error())
		return "", false, nil
	}
	// Under --dry-run nothing ran, and reading the exit code ends the
	// preview here.
	if result.ExitCode() != 0 {
		a.note("setup-token exited with an error.")
		return "", false, nil
	}
	if token, ok := auth.ExtractToken([]byte(result.Stdout())); ok {
		return token, true, nil
	}
	const scrapeFailed = "Error: could not extract the token from setup-token's output."
	a.note(scrapeFailed)
	token, err := a.askToken(pasteManuallyPrompt, scrapeFailed)
	if err != nil {
		return "", false, err
	}
	if token == "" {
		a.note(noTokenProvided)
		return "", false, nil
	}
	return token, true, nil
}

// longLivedToken runs setup-token, then checks, validates, and saves the
// token it gave. A setup-token is valid for a year.
func (a *authRun) longLivedToken(browser string, plan tokens.PlanTier) (AuthOutcome, error) {
	token, ok, err := a.captureSetupToken(browser)
	if err != nil || !ok {
		return AuthFailed, err
	}
	return a.checkAndSave(token, tokens.ExpiryTTL, plan, pasteManuallyPrompt,
		rejectedMessage+" The captured token may be stale or truncated.")
}

// pasteToken takes a token the user already has. Its expiry is unknown, so
// none is assumed.
func (a *authRun) pasteToken(plan tokens.PlanTier) (AuthOutcome, error) {
	token, err := a.askToken("Paste your API token", "")
	if err != nil {
		return AuthFailed, err
	}
	if token == "" {
		return AuthCancelled, nil
	}
	return a.checkAndSave(token, tokens.ExpiryNotKnown, plan, pasteCorrectedPrompt, rejectedMessage)
}

// checkAndSave runs a token through the shape check and the API, then saves
// it. A token the API rejects gets one re-entry (a scrape may have picked a
// stale frame), then the attempt fails; one the API cannot judge is saved
// only when the user chooses to.
func (a *authRun) checkAndSave(token string, expiry tokens.ExpiryDisposition, plan tokens.PlanTier, retryPrompt, rejected string) (AuthOutcome, error) {
	token, ok, err := a.formatGate(token)
	if err != nil || !ok {
		return AuthFailed, err
	}
	status, err := auth.ValidateToken(a.fx, token)
	if err != nil {
		return AuthFailed, err
	}
	if status == auth.Invalid {
		a.note(rejected)
		retyped, err := a.askToken(retryPrompt, rejected)
		if err != nil {
			return AuthFailed, err
		}
		if retyped == "" {
			a.note(noTokenProvided)
			return AuthFailed, nil
		}
		if token, ok, err = a.formatGate(retyped); err != nil || !ok {
			return AuthFailed, err
		}
		if status, err = auth.ValidateToken(a.fx, token); err != nil {
			return AuthFailed, err
		}
		if status == auth.Invalid {
			a.note(rejectedAgainNote)
			return AuthFailed, nil
		}
	}

	if status == auth.Valid {
		if !a.save(token, expiry, plan) {
			return AuthFailed, nil
		}
		a.note(savedNote)
		return AuthAuthenticated, nil
	}

	reason := "validation inconclusive"
	if status == auth.Unreachable {
		reason = "API unreachable"
	}
	choice, ok, err := widgets.RunSelection(a.ctx, a.t, a.c, fmt.Sprintf("Token could not be validated (%s)", reason), []widgets.Option{
		{Key: "save", Label: "Save unvalidated"},
		{Key: "abort", Label: "Abort"},
	}, "")
	if err != nil {
		return AuthFailed, err
	}
	if !ok || choice != "save" {
		return AuthFailed, nil
	}
	if !a.save(token, expiry, plan) {
		return AuthFailed, nil
	}
	a.note(savedUnverifiedNote)
	return AuthUnverified, nil
}

// save writes the token into the profile's own store with its declared
// plan, noting a failure with the token redacted.
func (a *authRun) save(token string, expiry tokens.ExpiryDisposition, plan tokens.PlanTier) bool {
	err := a.store.Data(a.profile).WriteToken(a.fx, token, expiry, plan, time.Now())
	if err != nil {
		a.note("Error saving token: " + effects.RedactError(err, token).Error())
		return false
	}
	return true
}
