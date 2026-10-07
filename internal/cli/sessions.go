package cli

import (
	"fmt"
	"os"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/profiles"
	"github.com/stricttools/claudewheel/internal/sessionmove"
	"github.com/stricttools/claudewheel/internal/sessions"
)

// registerSessions adds the commands that move Claude Code session data:
// migrate, mv, move-session, and import.
func registerSessions(app registrar) {
	oneSession := strictcli.MemberChoice(
		strictcli.StringFlag("session", "the session's id: a full lowercase UUID, as Claude Code records it; a prefix or any other spelling is refused", strictcli.Required()),
		"move one session, by its id")
	allSessions := strictcli.MemberChoice(
		strictcli.BoolFlag("all-sessions", "move every session the source profile holds", strictcli.Required()),
		"move every session the source profile holds")
	mutatingCommand(app, "migrate",
		"move session data files from one profile to another: one session (--session) or every session (--all-sessions)",
		func(c *call, kw map[string]interface{}) error {
			choice := sessionmove.SessionChoice{}
			chosen := strictcli.GetElected(kw, "sessions")
			if chosen.Is(oneSession) {
				choice.Session = strictcli.Get[string](chosen.Fields, "value")
			} else {
				choice.All = true
			}
			return migrate(c, kwString(kw, "src"), kwString(kw, "dst"), choice)
		},
		strictcli.WithArgs(
			strictcli.NewArg("src", "source profile name whose sessions will be moved (e.g. work)", strictcli.ArgRequired()),
			strictcli.NewArg("dst", "destination profile name to receive the migrated sessions (e.g. personal)", strictcli.ArgRequired())),
		strictcli.WithFlags(strictcli.MemberChoiceFlag("sessions", "which sessions move", strictcli.Required(),
			oneSession, allSessions)))

	mutatingCommand(app, "mv",
		"rename a project directory and migrate session data",
		handleMv,
		strictcli.WithArgs(
			strictcli.NewArg("old", "current path of the project directory to rename (absolute or relative)", strictcli.ArgRequired()),
			strictcli.NewArg("new", "target path for the renamed project directory (absolute or relative)", strictcli.ArgRequired())),
		strictcli.WithFlags(strictcli.BoolFlag("post-hoc",
			"skip filesystem rename, migrate sessions only (directory already renamed); when omitted, the directory is renamed too",
			strictcli.Optional())))

	mutatingCommand(app, "move-session",
		"move one Claude Code session, by its id, to another project "+
			"directory's session store, so Claude Code resumes it from that "+
			"directory: its transcript and folder move together, the paths "+
			"in its transcript that point into its own store folder follow "+
			"it, and Claude Code's relocated record is appended. Refuses a "+
			"session that is running or starting, one a background job or "+
			"another session's symlink refers to, and one that more than one "+
			"store dir holds. An interrupted move is finished by running the "+
			"same command again",
		handleMoveSession,
		strictcli.WithArgs(
			strictcli.NewArg("session", "the session id: a full lowercase UUID, as Claude Code records it; a prefix or any other spelling is refused", strictcli.ArgRequired()),
			strictcli.NewArg("directory", "the existing project directory the session moves to (absolute or relative)", strictcli.ArgRequired())))

	// A path mapping is a pair: neither half means anything alone. The empty
	// defaults declare no element, so nothing the invocation did not state
	// reaches a write; the handler pairs the two lists by position.
	mutatingCommand(app, "import",
		"import session data from an external Claude Code directory",
		handleImport,
		strictcli.WithArgs(strictcli.NewArg("source", "path to the source directory (e.g., /path/to/backup/.claude)", strictcli.ArgRequired())),
		strictcli.WithFlagSets(strictcli.FlagSet{Name: "mapping", Flags: []strictcli.Flag{
			strictcli.StringFlag("from", "original project path as recorded in the source session data (repeatable)",
				strictcli.Repeatable(), strictcli.Unique(false), strictcli.Default([]interface{}{})),
			strictcli.StringFlag("to", "local directory path that corresponds to the --from path on this machine (repeatable)",
				strictcli.Repeatable(), strictcli.Unique(false), strictcli.Default([]interface{}{})),
		}}),
		strictcli.WithFlags(strictcli.BoolFlag("reid",
			"assign new UUIDs to sessions that collide with existing local sessions; when omitted, a collision is reported and nothing is imported",
			strictcli.Optional())),
		strictcli.WithConstraints(strictcli.AllOrNone("path-mapping",
			strictcli.Member("from"), strictcli.Member("to"))))
}

// migrate resolves the two profile names and moves the chosen sessions.
func migrate(c *call, srcName, dstName string, choice sessionmove.SessionChoice) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	dirs, err := profiles.New(c.ws).ConfigDirs()
	if err != nil {
		return err
	}
	src, err := profileDir(dirs, srcName)
	if err != nil {
		return err
	}
	dst, err := profileDir(dirs, dstName)
	if err != nil {
		return err
	}
	_, err = sessionmove.Migrate(c.fx, src, dst, choice)
	return err
}

// profileDir finds the profile name among dirs; an unknown name is refused,
// naming the profiles that exist.
func profileDir(dirs []sessions.ProfileConfigDir, name string) (sessions.ProfileConfigDir, error) {
	names := make([]string, len(dirs))
	for i, d := range dirs {
		if d.Name == name {
			return d, nil
		}
		names[i] = d.Name
	}
	return sessions.ProfileConfigDir{}, fmt.Errorf("no profile '%s'; the profiles are: %s", name, strings.Join(names, ", "))
}

func handleMv(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	dirs, err := profiles.New(c.ws).ConfigDirs()
	if err != nil {
		return err
	}
	_, err = sessionmove.Mv(c.fx, c.ws, dirs, kwString(kw, "old"), kwString(kw, "new"),
		sessionmove.MvOptions{PostHoc: kwSwitch(kw, "post_hoc")})
	return err
}

func handleMoveSession(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	dirs, err := profiles.New(c.ws).ConfigDirs()
	if err != nil {
		return err
	}
	_, err = sessionmove.MoveSession(c.fx, c.ws, dirs, kwString(kw, "session"), kwString(kw, "directory"))
	return err
}

func handleImport(c *call, kw map[string]interface{}) error {
	from, to := kwStrings(kw, "from"), kwStrings(kw, "to")
	if len(from) != len(to) {
		return fmt.Errorf("--from and --to must appear the same number of times (got %d --from and %d --to)", len(from), len(to))
	}
	if _, err := c.appConfig(); err != nil {
		return err
	}
	mappings := make([]sessionmove.PathMapping, len(from))
	for i := range from {
		resolved, err := sessionmove.ResolveUserPath(to[i])
		if err != nil {
			return err
		}
		info, err := os.Stat(resolved)
		if err != nil || !info.IsDir() {
			return fmt.Errorf("--to path does not exist or is not a directory: %s", to[i])
		}
		mappings[i] = sessionmove.PathMapping{From: from[i], To: resolved}
	}
	reid := kwSwitch(kw, "reid")
	result, err := sessionmove.Import(c.fx, c.ws.Shared(), kwString(kw, "source"), mappings,
		sessionmove.ImportOptions{Reid: reid, Warnings: os.Stderr})
	if err != nil {
		return err
	}
	if len(result.Collisions) > 0 && !reid {
		c.say("Collisions detected (use --reid to assign new UUIDs):")
		for _, collision := range result.Collisions {
			c.say("  " + collision)
		}
		return exitStatus(1)
	}
	return nil
}
