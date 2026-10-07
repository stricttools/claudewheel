package cli

import (
	"fmt"
	"os"
	"slices"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/launch"
)

// clientArgsArg is the launch command's positional argument holding what
// follows "--", passed to the client as it is.
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
			}),
		strictcli.WithArgs(strictcli.NewArg(clientArgsArg,
			"arguments for the client, given after --, passed to it as they are (the miniclaude client takes none)",
			strictcli.Variadic(), strictcli.ArgOptional())),
		strictcli.WithFlags(
			strictcli.MemberChoiceFlag("session", "which session this launch starts in", strictcli.Default("new-session"),
				cont, resume, printPrompt, picker, newSession),
			strictcli.StringFlag("set",
				"preset a segment as KEY=VALUE (e.g. -s version=2.1.119, -s profile=work); repeatable, one per segment. A value the segment does not offer yet is taken for this launch",
				strictcli.Short("s"), strictcli.Repeatable(), strictcli.Unique(false), strictcli.Default([]interface{}{})),
			strictcli.StringFlag("client",
				"the client to launch; when omitted, the launch bar asks (focused on config.json's default_client), and a launch that skips the bar uses default_client. Given, it skips that question",
				strictcli.Optional(), strictcli.Choices(clientChoices...))))
}

func handleLaunch(c *call, kw map[string]interface{}, session launch.Session) error {
	clientArgs := kwStrings(kw, clientArgsArg)
	if err := checkClientArgs(os.Args[1:], clientArgs); err != nil {
		return err
	}
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

// checkClientArgs refuses client arguments that did not come after "--" in
// args (the command line without the program name): a stray word is a
// mistyped command or flag, never something to hand the client.
func checkClientArgs(args, clientArgs []string) error {
	after := 0
	if i := slices.Index(args, "--"); i >= 0 {
		after = len(args) - i - 1
	}
	if len(clientArgs) <= after {
		return nil
	}
	stray := clientArgs[:len(clientArgs)-after]
	return fmt.Errorf("unexpected argument %s: it is not a claudewheel command, and launch takes arguments for the client only after -- (claudewheel -- %s)", strings.Join(stray, " "), strings.Join(stray, " "))
}
