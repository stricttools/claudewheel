package guardrail

// HookOptionKeys returns the per-hook option keys a wiring may carry, spelled
// as settings.json spells them, in the order they are written.
func HookOptionKeys() []string {
	return []string{
		"asyncRewake",
		"timeout",
		"rewakeMessage",
		"rewakeSummary",
	}
}

// HookOptions are the per-hook options of one wiring.
//
// AsyncRewake runs the hook in the background, and its exit status 2 wakes the
// session's main conversation with its output. Timeout is seconds before
// Claude Code stops the hook (0: not set). RewakeMessage prefixes the reminder
// a rewake shows the model and RewakeSummary is its notification's summary
// line (empty: not set); both only mean something with AsyncRewake.
type HookOptions struct {
	AsyncRewake   bool
	Timeout       int
	RewakeMessage string
	RewakeSummary string
}

// HookWiring is one hook a profile must wire: the event, the matcher (empty
// matches every tool), the script name, and its options.
type HookWiring struct {
	Event   string
	Matcher string
	Script  string
	Options HookOptions
}

// The waiter is an async-rewake hook on SessionStart and Stop that waits in
// the background for probe reports queued for the main conversation; its
// timeout, a week, is how long an idle session stays wakeable.
var probeWaiterOptions = HookOptions{
	AsyncRewake:   true,
	Timeout:       7 * 24 * 3600,
	RewakeMessage: "claudewheel probe report:",
	RewakeSummary: "claudewheel probe report",
}

// ExpectedHookWirings returns every hook wiring a profile must have, in
// canonical order. The canonical hooks tree is built from it and health
// verifies profiles against it. The session-start and session-end hooks feed
// the lifecycle store and the last ones hand probe reports to sessions; they
// are wired here because this is the one list of hooks a profile carries.
func ExpectedHookWirings() []HookWiring {
	return []HookWiring{
		{Event: "UserPromptSubmit", Matcher: "", Script: "hook-timestamp"},
		{Event: "PreToolUse", Matcher: "Agent", Script: "hook-block-worktree"},
		{Event: "PreToolUse", Matcher: "Bash", Script: "hook-block-unsafe-commands"},
		{Event: "PostToolUse", Matcher: "Bash", Script: "hook-advise-commands"},
		{Event: "SessionStart", Matcher: "", Script: "hook-session-start"},
		{Event: "SessionEnd", Matcher: "", Script: "hook-session-end"},
		{Event: "SessionStart", Matcher: "", Script: "hook-wait-for-probe-reports", Options: probeWaiterOptions},
		{Event: "Stop", Matcher: "", Script: "hook-wait-for-probe-reports", Options: probeWaiterOptions},
		{Event: "PreToolUse", Matcher: "Bash", Script: "hook-deliver-probe-reports"},
		{Event: "PostToolUse", Matcher: "", Script: "hook-deliver-probe-reports"},
		{Event: "PostToolUseFailure", Matcher: "", Script: "hook-deliver-probe-reports"},
		{Event: "SubagentStop", Matcher: "", Script: "hook-deliver-probe-reports"},
	}
}
