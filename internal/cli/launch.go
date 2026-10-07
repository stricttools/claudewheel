package cli

import (
	"fmt"
	"os"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/launch"
)

// clientArgsArg names the launch command's receiver of what follows "--",
// passed to the client as it is.
const clientArgsArg = "client-args"

// registerLaunch adds the launch command, which a bare `claudewheel` runs.
func registerLaunch(app registrar) {
	cont := strictcli.MemberChoice(
		strictcli.BoolFlag("cont", "continue the most recent conversation in the current directory", strictcli.Required(), strictcli.Short("c")),
		"continue the most recent conversation in the current directory")
	resume := strictcli.MemberChoice(
		strictcli.StringFlag("resume", "session to resume, by its session id (a lowercase UUID) or by title; an empty string opens Claude Code's own picker", strictcli.Required(), strictcli.Short("r")),
		"resume one specific session")
	printPrompt := strictcli.MemberChoice(
		strictcli.StringFlag("print-prompt", "the prompt to run non-interactively", strictcli.Required(), strictcli.Short("p")),
		"run one prompt in non-interactive print mode and exit")
	picker := strictcli.MemberChoice(
		strictcli.BoolFlag("picker", "open Claude Code's session picker to choose the session to resume", strictcli.Required()),
		"pick the session to resume from Claude Code's session picker")
	newSession := strictcli.MemberChoice(
		strictcli.BoolFlag("new-session", "start a new session (what a bare launch does)", strictcli.Required()),
		"start a new session (what a bare launch does)")

	adapters := launch.Adapters()
	clientChoices := make([]strictcli.ChoiceValue, len(adapters))
	for i, a := range adapters {
		clientChoices[i] = strictcli.Ch(a.Name, a.Help)
	}
	set := strictcli.StringFlag("set",
		"preset a segment as KEY=VALUE (e.g. -s version=2.1.119, -s profile=work); repeatable, one per segment. A value a fixed-choice segment does not offer, once its discovery has run, is refused, naming the values it offers; a freeform segment such as directory takes any value",
		strictcli.Short("s"), strictcli.Repeatable(), strictcli.Unique(false), strictcli.Default([]interface{}{}))
	client := strictcli.StringFlag("client",
		"the client to launch; when omitted, the launch bar asks (focused on config.json's default_client), and a launch that skips the bar uses default_client. Given, it skips that question",
		strictcli.Optional(), strictcli.Choices(clientChoices...))
	mutatingCommand(app, "launch",
		"start a Claude Code session: the launch bar picks the profile, version, model, directory, and the rest, unless -s presets every required segment or --print-prompt runs one prompt; then the health check, the pre-launch hooks in ~/.claudewheel/hooks, and the preflight steps run, and the client starts in the session's systemd units. A bare claudewheel runs it. Arguments after -- go to the client",
		func(c *call, kw map[string]interface{}) error {
			chosen := strictcli.GetElected(kw, "session")
			session := launch.Session{Mode: launch.SessionNew}
			switch {
			case chosen.Is(cont):
				session.Mode = launch.SessionContinue
			case chosen.Is(resume):
				session = launch.Session{Mode: launch.SessionResume, Value: strictcli.Get[string](chosen.Fields, "value")}
			case chosen.Is(printPrompt):
				session = launch.Session{Mode: launch.SessionPrint, Value: strictcli.Get[string](chosen.Fields, "value")}
			case chosen.Is(picker):
				session.Mode = launch.SessionPicker
			case chosen.Is(newSession):
			default:
				return fmt.Errorf("unknown session choice %s", chosen.Name())
			}
			return handleLaunch(c, kw, session)
		},
		strictcli.WithGrants(
			strictcli.Grant{
				Name:   launch.ExecClientGrant,
				Reason: "the launcher replaces this process with the selected client binary",
				Kind:   strictcli.ProcMutate,
			},
			strictcli.Grant{
				Name:   "auth-login",
				Reason: "the launch bar offers an interactive Claude Code login for a profile that is not authenticated, and for a profile it creates",
				Kind:   strictcli.ProcMutate,
			},
			strictcli.Grant{
				Name:   "download",
				Reason: "the launch bar installs a Claude Code version it is asked for, an executable fetched from the Claude Code release bucket",
				Kind:   strictcli.NetMutate,
			},
			strictcli.Grant{
				Name:   "archive-delegation",
				Reason: "the launch bar deletes a profile by handing its whole directory, its stored OAuth token included, to saferm, which archives it and then removes it",
				Kind:   strictcli.ProcMutate,
			}),
		strictcli.WithArgsAfterSeparator(clientArgsArg,
			"arguments for the client, given after --, passed to it as they are (the miniclaude client takes none)"),
		strictcli.WithFlags(
			strictcli.MemberChoiceFlag("session", "which session this launch starts in", strictcli.Default("new-session"),
				cont, resume, printPrompt, picker, newSession),
			set,
			client))
}

func handleLaunch(c *call, kw map[string]interface{}, session launch.Session) error {
	clientArgs := c.ctx.ArgsAfterSeparator()
	store, err := c.appConfig()
	if err != nil {
		return err
	}
	executable, err := ownExecutable()
	if err != nil {
		return err
	}
	client, _ := kwOptString(kw, "client")
	ctx, stop := c.signalContext()
	defer stop()
	return launch.Run(launch.Env{
		Ctx:        ctx,
		FX:         c.fx,
		Store:      store,
		Executable: executable,
		Stderr:     os.Stderr,
	}, launch.Request{
		Session:    session,
		Presets:    kwStrings(kw, "set"),
		Client:     client,
		ClientArgs: clientArgs,
	})
}
