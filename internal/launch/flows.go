package launch

import (
	"context"
	"fmt"
	"os"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/install"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/terminal"
	"github.com/stricttools/claudewheel/internal/tui/bar"
	"github.com/stricttools/claudewheel/internal/tui/deletion"
	"github.com/stricttools/claudewheel/internal/tui/sessionsview"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
	"github.com/stricttools/claudewheel/internal/tui/wizard"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// authSkipLabel names the skip choice of the authentication offered before a
// launch.
const authSkipLabel = "Launch without auth"

// BarFlows builds the screens the bar opens that live above it: the
// create-profile wizard, the authentication offered before a launch, the
// deletion checklist, and adding or removing the guardrails on ~/.claude.
// Each draws on t, the bar's terminal, with colors.
func BarFlows(fx *effects.FX, ws workspace.Workspace, locator install.Locator, t *terminal.Terminal, colors widgets.Colors) bar.Flows {
	return bar.Flows{
		CreateProfile: func(ctx context.Context) (string, bool, error) {
			summary, err := wizard.RunCreate(ctx, fx, t, colors, ws)
			if err != nil {
				return "", false, err
			}
			if summary.Cancelled || summary.Profile == "" {
				return "", false, nil
			}
			return summary.Profile, true, nil
		},
		Authenticate: func(ctx context.Context, profile string) (widgets.AuthOutcome, error) {
			result, err := wizard.RunAuthFlow(ctx, fx, t, colors, ws, profile, authSkipLabel)
			if err != nil {
				return "", err
			}
			if len(result.Notes) > 0 {
				if _, err := widgets.ShowPage(ctx, t, colors, widgets.Page{
					Title: fmt.Sprintf("Authentication of '%s'", profile),
					Lines: result.Notes,
					Hint:  widgets.HintAnyKey,
				}); err != nil {
					return "", err
				}
			}
			return result.Outcome, nil
		},
		DeletionChecklist: func(ctx context.Context, profile string) (bar.ChecklistOutcome, error) {
			configDir := profiles.New(ws).PathFor(profile)
			holders, err := deletion.GatherHolders(fx, configDir)
			if err != nil {
				return bar.ChecklistOutcome{}, err
			}
			if len(holders) == 0 {
				// The holders ended between the report and here: nothing to ask.
				return bar.ChecklistOutcome{Confirmed: true}, nil
			}
			out, err := deletion.Run(ctx, fx, t, colors, holders, deletion.Checklist{
				ProfileName: profile,
				ConfigDir:   configDir,
				Binary:      locator.Fallback(),
				Environ:     os.Environ(),
				NowMS:       lifecycle.NowMS(),
				Identity:    sessionsview.CurrentIdentity(os.LookupEnv),
			})
			if err != nil {
				return bar.ChecklistOutcome{}, err
			}
			return bar.ChecklistOutcome{Confirmed: out.Confirmed(), StillHolding: out.StillHolding}, nil
		},
		SetVanillaGuardrails: func(fx *effects.FX, enable bool) error {
			return SetVanillaGuardrails(fx, ws, enable)
		},
	}
}
