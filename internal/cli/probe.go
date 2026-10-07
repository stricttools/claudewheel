package cli

import (
	"os"
	"strings"

	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/probe"
	"github.com/stricttools/claudewheel/internal/proberunner"
)

// registerProbe adds the probe group. Every command but list and
// run-service acts for the Claude Code session it runs in, which it learns
// from its own cgroup.
func registerProbe(app *strictcli.App) {
	g := app.Group("probe",
		"watch Claude Code sessions for OOM kills and report them to the sessions subscribed: create, list, stop, subscribe, and unsubscribe probes, and run the probe runner. Every session is told of its own commands' OOM kills without a probe")

	oneSession := strictcli.MemberChoice(
		strictcli.StringFlag("session", "the uuid of the session to watch, as the lifecycle store records it", strictcli.Required()),
		"watch one Claude Code session, by its uuid")
	allSessions := strictcli.MemberChoice(
		strictcli.BoolFlag("all-sessions", "watch every Claude Code session on this machine", strictcli.Required()),
		"watch every Claude Code session on this machine")
	mutatingCommand(g, "create",
		"create a probe of one kind (oom-kill: a unit's process killed by the"+
			" kernel's OOM killer, as systemd reports it) watching one session"+
			" (--session) or every session (--all-sessions), until its --deadline"+
			" or an earlier stop (--count, --until-watched-ends, --until-file, or"+
			" probe stop), and subscribe the session this runs in to it. A probe"+
			" runs no command: an arbitrary command is refused. Run it from a Bash"+
			" tool call of a claudewheel session, whose cgroup names the session;"+
			" the reports go to the conversation that made the call, the main one"+
			" or a subagent, once the hook that reads the call's payload binds the"+
			" subscription to it",
		func(c *call, kw map[string]interface{}) error {
			var watch *string
			if chosen := strictcli.GetElected(kw, "watch"); chosen.Is(oneSession) {
				value := strictcli.Get[string](chosen.Fields, "value")
				watch = &value
			}
			return probeCreate(c, kw, watch)
		},
		strictcli.WithArgs(strictcli.NewArg("kind", "what the probe watches for", strictcli.ArgRequired(),
			strictcli.ArgChoices(strictcli.Ch(probe.ProbeOOMKill,
				"a process in a systemd user unit killed by the kernel's OOM killer (systemd's result term)")))),
		strictcli.WithFlags(
			strictcli.MemberChoiceFlag("watch", "which sessions the probe watches", strictcli.Required(),
				oneSession, allSessions),
			strictcli.StringFlag("deadline",
				"how long the probe lives at the latest, from now: a whole number with an s, m, h, or d suffix (90s, 30m, 2h, 7d). Every probe states one",
				strictcli.Required()),
			strictcli.IntFlag("count",
				"end the probe once it has seen this many kills; when omitted, the count never ends it",
				strictcli.Optional()),
			strictcli.BoolFlag("until-watched-ends",
				"end the probe when the watched session ends (needs --session); when omitted, the session ending does not end it",
				strictcli.Optional()),
			strictcli.StringFlag("until-file",
				"end the probe once this absolute path exists; when omitted, no file ends it",
				strictcli.Optional())))

	readOnlyCommand(g, "list",
		"list every probe with its stops and subscriptions, every report not yet confirmed delivered (with why), every expired report, and every OOM kill no session or subscription took; runs anywhere",
		handleProbeList)

	mutatingCommand(g, "stop",
		"end a live probe the session this runs in created; its undelivered reports to sessions that have ended are expired",
		handleProbeStop,
		strictcli.WithArgs(probeIDArg()))

	mutatingCommand(g, "subscribe",
		"subscribe the session this runs in to a live probe; the reports go to the conversation that made the call, once the hook that reads the call's payload binds the subscription to it",
		handleProbeSubscribe,
		strictcli.WithArgs(probeIDArg()))

	mutatingCommand(g, "unsubscribe",
		"remove one of the subscriptions of the session this runs in; the probe reports nothing more to it",
		handleProbeUnsubscribe,
		strictcli.WithArgs(strictcli.NewArg("subscription",
			"the subscription's id, as probe create, probe subscribe, and probe list print it",
			strictcli.ArgRequired())))

	mutatingCommand(g, "run-service",
		"run the probe runner, the process "+probe.ServiceName+" starts: follow the user journal for OOM kills, report each to the sessions it concerns, and keep the probe store moving, until SIGTERM or SIGINT. systemctl --user stop "+probe.ServiceName+" stops it gracefully; deploy-hooks "+probe.ServiceName+" installs the service",
		handleProbeRunService,
		strictcli.WithDryRunUnsupported("the probe runner is a long-running service that follows the journal and acts on every entry; there is nothing to preview"))
}

// probeIDArg is the probe id argument of stop and subscribe.
func probeIDArg() strictcli.Arg {
	return strictcli.NewArg("probe_id", "the probe's id, as probe create and probe list print it", strictcli.ArgRequired())
}

// probeStore is the workspace's probe store.
func probeStore(c *call) probe.Store {
	return probe.NewStore(c.ws.Shared().ProbesDir())
}

// probeSession opens the workspace and returns the session this command
// runs in.
func probeSession(c *call) (string, error) {
	if _, err := c.appConfig(); err != nil {
		return "", err
	}
	cgroup, err := probe.OwnCgroupText()
	if err != nil {
		return "", err
	}
	return probe.ResolveSession(c.ws.Shared().LifecycleDir(), cgroup, lifecycle.NowMS())
}

func probeCreate(c *call, kw map[string]interface{}, watch *string) error {
	probeType := kwString(kw, "kind")
	session, err := probeSession(c)
	if err != nil {
		return err
	}
	seconds, err := probe.ParseDuration(kwString(kw, "deadline"))
	if err != nil {
		return err
	}
	req := probe.CreateRequest{
		Session:          session,
		ProbeType:        probeType,
		WatchSession:     watch,
		DeadlineSeconds:  seconds,
		UntilWatchedEnds: kwSwitch(kw, "until_watched_ends"),
		NowMS:            lifecycle.NowMS(),
	}
	if count, ok := kwOptInt(kw, "count"); ok {
		n := int64(count)
		req.UntilCount = &n
	}
	if file, ok := kwOptString(kw, "until_file"); ok {
		req.UntilFile = &file
	}
	probeID, subscription, err := probe.CreateProbe(c.fx, probeStore(c), c.ws.Shared().LifecycleDir(), req)
	if err != nil {
		return err
	}
	watched := "all sessions"
	if watch != nil {
		watched = "session " + *watch
	}
	if c.previewing() {
		c.sayf("would create a probe (%s) of %s, subscribing session %s", probeType, watched, session)
		return nil
	}
	c.sayf("probe %s: %s in %s, subscribed by session %s", probeID, probeType, watched, session)
	c.say(probe.BindLine(subscription, probeID))
	return nil
}

func handleProbeList(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	text, err := probe.RenderList(probeStore(c))
	if err != nil {
		return err
	}
	c.say(strings.TrimSuffix(text, "\n"))
	return nil
}

func handleProbeStop(c *call, kw map[string]interface{}) error {
	probeID := kwString(kw, "probe_id")
	session, err := probeSession(c)
	if err != nil {
		return err
	}
	if err := probe.StopProbe(c.fx, probeStore(c), session, probeID); err != nil {
		return err
	}
	verb := "stopped"
	if c.previewing() {
		verb = "would stop"
	}
	c.sayf("%s probe %s", verb, probeID)
	return nil
}

func handleProbeSubscribe(c *call, kw map[string]interface{}) error {
	probeID := kwString(kw, "probe_id")
	session, err := probeSession(c)
	if err != nil {
		return err
	}
	subscription, err := probe.Subscribe(c.fx, probeStore(c), session, probeID)
	if err != nil {
		return err
	}
	if c.previewing() {
		c.sayf("would subscribe session %s to probe %s", session, probeID)
		return nil
	}
	c.sayf("session %s subscribed to probe %s", session, probeID)
	c.say(probe.BindLine(subscription, probeID))
	return nil
}

func handleProbeUnsubscribe(c *call, kw map[string]interface{}) error {
	subscription := kwString(kw, "subscription")
	session, err := probeSession(c)
	if err != nil {
		return err
	}
	probeID, err := probe.Unsubscribe(c.fx, probeStore(c), session, subscription)
	if err != nil {
		return err
	}
	verb := "removed"
	if c.previewing() {
		verb = "would remove"
	}
	c.sayf("%s subscription %s of probe %s", verb, subscription, probeID)
	return nil
}

// handleProbeRunService runs the probe runner until a signal ends it. The
// runner returns nil once the context is cancelled; strictcli then ends the
// command with 128 + the signal's number (143 for systemd's SIGTERM, which
// the unit's SuccessExitStatus accepts).
func handleProbeRunService(c *call, kw map[string]interface{}) error {
	if _, err := c.appConfig(); err != nil {
		return err
	}
	ctx, stop := c.signalContext()
	defer stop()
	return proberunner.Run(ctx, c.fx, c.ws, os.Stderr)
}
