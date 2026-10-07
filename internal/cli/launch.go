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
	set := strictcli.StringFlag("set",
		"preset a segment as KEY=VALUE (e.g. -s version=2.1.119, -s profile=work); repeatable, one per segment. A value the segment does not offer yet is taken for this launch",
		strictcli.Short("s"), strictcli.Repeatable(), strictcli.Unique(false), strictcli.Default([]interface{}{}))
	client := strictcli.StringFlag("client",
		"the client to launch; when omitted, the launch bar asks (focused on config.json's default_client), and a launch that skips the bar uses default_client. Given, it skips that question",
		strictcli.Optional(), strictcli.Choices(clientChoices...))
	// The spellings that take the next token as their value, read off the
	// declarations, so the client-argument check splits the command line
	// where strictcli does.
	valueFlags := valueFlagSpellings(cont.Flags[0], resume.Flags[0], printPrompt.Flags[0], picker.Flags[0], newSession.Flags[0], set, client)

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
			return handleLaunch(c, kw, session, valueFlags)
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
		strictcli.WithArgs(strictcli.NewArg(clientArgsArg,
			"arguments for the client, given after --, passed to it as they are (the miniclaude client takes none)",
			strictcli.Variadic(), strictcli.ArgOptional())),
		strictcli.WithFlags(
			strictcli.MemberChoiceFlag("session", "which session this launch starts in", strictcli.Default("new-session"),
				cont, resume, printPrompt, picker, newSession),
			set,
			client))
}

func handleLaunch(c *call, kw map[string]interface{}, session launch.Session, valueFlags map[string]bool) error {
	clientArgs := kwStrings(kw, clientArgsArg)
	if err := checkClientArgs(os.Args[1:], valueFlags, clientArgs); err != nil {
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
//
// strictcli has no declaration for "only what follows --": a variadic
// positional also takes the words before it. So the command line is split
// here as strictcli's tokenizer splits it: the framework switches before the
// first "--" are dropped (strictcli takes them out wherever they stand), and
// a "--" that is the value of a flag in valueFlags (-p --, say) is that
// flag's value, not the separator.
func checkClientArgs(args []string, valueFlags map[string]bool, clientArgs []string) error {
	end := slices.Index(args, "--")
	if end < 0 {
		end = len(args)
	}
	tokens := make([]string, 0, len(args))
	for i, tok := range args {
		if i < end && anywhereSwitches[tok] {
			continue
		}
		tokens = append(tokens, tok)
	}
	after := 0
	for i := 0; i < len(tokens); i++ {
		if tokens[i] == "--" {
			after = len(tokens) - i - 1
			break
		}
		if valueFlags[tokens[i]] {
			i++
		}
	}
	if len(clientArgs) <= after {
		return nil
	}
	stray := clientArgs[:len(clientArgs)-after]
	return fmt.Errorf("unexpected argument %s: it is not a claudewheel command, and launch takes arguments for the client only after -- (claudewheel -- %s)", strings.Join(stray, " "), strings.Join(stray, " "))
}

// valueFlagSpellings returns the spellings of the flags among flags that take
// the next token as their value: --name, and -x for one with a short.
func valueFlagSpellings(flags ...strictcli.Flag) map[string]bool {
	out := map[string]bool{}
	for _, f := range flags {
		if f.Type == strictcli.TypeBool {
			continue
		}
		out["--"+f.Name] = true
		if f.Short != "" {
			out["-"+f.Short] = true
		}
	}
	return out
}
