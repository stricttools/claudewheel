// Package wizard is the profile creation wizard: the new-profile form, the
// writing of the profile with its shared-store links, and the auth flow that
// follows (a browser session login, a long-lived token from `claude
// setup-token` captured from its output, or a pasted token), with the plan
// picker and the detection of installed browsers across native, flatpak,
// and snap installs.
//
// Every screen is a widget on a terminal the caller has put in cbreak mode
// on the alternate screen; the logins run in cooked windows under a
// pseudo-terminal. Restoring the terminal is the caller's. A token never
// appears in anything this package draws, returns, or records.
package wizard

import (
	"context"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// CreateSkipLabel is the skip choice of the auth flow that follows creation.
const CreateSkipLabel = "Skip for now"

// Summary is what a create run did, for the caller to print once the
// terminal is restored.
type Summary struct {
	// Cancelled reports that the form was cancelled and nothing was made.
	Cancelled bool
	// Previewing reports a --dry-run, where the writes were recorded.
	Previewing bool
	// Profile is the new profile's name.
	Profile string
	// Lines describe the profile created.
	Lines []string
	// Auth is how the auth flow ended.
	Auth AuthResult
}

// Report returns the lines to print: "Cancelled." for a cancelled form;
// otherwise the summary, the auth flow's notes, and a closing line on the
// auth outcome (none after a skip).
func (s Summary) Report() []string {
	if s.Cancelled {
		return []string{"Cancelled."}
	}
	lines := append([]string{}, s.Lines...)
	lines = append(lines, s.Auth.Notes...)
	switch s.Auth.Outcome {
	case AuthAuthenticated:
		if s.Previewing {
			lines = append(lines, "Profile would be authenticated.")
		} else {
			lines = append(lines, "Profile authenticated.")
		}
	case AuthUnverified:
		if s.Previewing {
			lines = append(lines, "Token would be saved without validation (API unreachable).")
		} else {
			lines = append(lines, "Token saved without validation (API unreachable).")
		}
	case AuthCancelled:
		lines = append(lines, "Auth setup cancelled -- you can authenticate later by launching the profile.")
	case AuthFailed:
		lines = append(lines, "Auth setup failed -- you can retry by launching the profile.")
	}
	return lines
}

// RunCreate is the profile create flow in one alternate-screen session: the
// form, the profile written (CreateProfile, wiring hooks with merge), the
// auth flow, and a page showing what was created and how auth ended. The
// terminal must be in cbreak mode on the alternate screen; restoring it is
// the caller's.
//
// The profile is created before auth is attempted, and a failed auth does
// not undo it. An error after the profile was created comes with the
// summary so far, so the caller can still print it. When ctx is done the
// error is its cancellation cause.
func RunCreate(ctx context.Context, fx *effects.FX, t *terminal.Terminal, c widgets.Colors, ws workspace.Workspace, merge HookMerge) (Summary, error) {
	summary := Summary{Previewing: fx.Previewing()}
	existing, err := profiles.New(ws).Enumerate()
	if err != nil {
		return summary, err
	}
	names := make([]string, len(existing))
	for i, p := range existing {
		names[i] = p.Name
	}
	choices, ok, err := RunProfileForm(ctx, t, c, ws, names)
	if err != nil {
		return summary, err
	}
	if !ok {
		summary.Cancelled = true
		return summary, nil
	}
	summary.Profile = choices.Name
	summary.Lines, err = CreateProfile(fx, ws, choices, merge)
	if err != nil {
		return summary, err
	}
	summary.Auth, err = RunAuthFlow(ctx, fx, t, c, ws, choices.Name, CreateSkipLabel)
	if err != nil {
		return summary, err
	}

	title := "Profile created"
	if summary.Previewing {
		title = "Profile would be created"
	}
	lines := append([]string{}, summary.Lines...)
	if len(summary.Auth.Notes) > 0 {
		lines = append(lines, "")
		lines = append(lines, summary.Auth.Notes...)
	}
	if _, err := widgets.ShowPage(ctx, t, c, widgets.Page{Title: title, Lines: lines, Hint: widgets.HintAnyKey}); err != nil {
		return summary, err
	}
	return summary, nil
}
