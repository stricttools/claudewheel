package guardrail

// DisallowedTool is a Claude Code tool stripped from every launched session
// through the --disallowedTools launch argument, and why. A name stays banned
// while the installed Claude Code happens not to offer the tool: such an entry
// is insurance, not an error.
type DisallowedTool struct {
	Name string
	Why  string
}

// DisallowedToolEntries returns the stripped tools in canonical order, built
// fresh. It is the one authority for the strip list.
func DisallowedToolEntries() []DisallowedTool {
	return []DisallowedTool{
		{
			"Artifact",
			"Artifacts are unwanted; when an HTML report is wanted, it will be " +
				"asked for explicitly.",
		},
		{
			"DesignSync",
			"Serves Claude Design, which is unwanted -- and it only works with " +
				"short-term logins, never the long-lived OAuth tokens claudewheel " +
				"prefers.",
		},
		{
			"EnterPlanMode",
			"Plan mode hijacks the session lifecycle: accepting a plan clears the " +
				"session and makes the previous messages unreachable in the TUI. Fresh " +
				"context for implementation is better achieved deliberately -- a new " +
				"session or a subagent orchestrator.",
		},
		{
			"EnterWorktree",
			"Exposing worktrees as tools invites silent, unauthorized use: work " +
				"strays into a temp worktree, later sessions cannot find it, tokens are " +
				"wasted rebuilding it, and stale files linger. Bash covers the rare " +
				"legitimate case, explicitly.",
		},
		{
			"ExitPlanMode",
			"Counterpart of EnterPlanMode; banned with it.",
		},
		{
			"ExitWorktree",
			"Counterpart of EnterWorktree; banned with it.",
		},
		{
			"LSP",
			"Injects compile-time diagnostics mid-work that are stale by the time " +
				"the agent finishes; real errors surface at build time anyway. A net " +
				"distraction left over from the era of slow human typing.",
		},
		{
			"NotebookEdit",
			"No Jupyter notebooks here -- and their non-plaintext format is a " +
				"reason to avoid them entirely. Plain file writes cover everything.",
		},
		{
			"PushNotification",
			"Belongs to Remote Control, which is rejected wholesale. When a Remote " +
				"Control pairing exists it also pushes model-authored text to phone and " +
				"email with no permission prompt.",
		},
		{
			"RemoteTrigger",
			"Client for claude.ai routines: autonomous cloud agents acting as the " +
				"user with no in-run approvals, self-approving locally. Stays banned " +
				"even while dormant behind a server-side feature flag, as insurance " +
				"against the flag flipping.",
		},
		{
			"ReportFindings",
			"Exists solely to serve /code-review, which is unwanted; inert in " +
				"terminal sessions regardless.",
		},
		{
			"Skill",
			"Bloatware: injected prompt payloads. Instructions worth having live " +
				"in the repository.",
		},
		{
			"TaskCreate",
			"The task-tracking system is dead weight: a months-long usage survey " +
				"found this was the only family member ever used (thousands of calls) " +
				"while the conversation itself served as the real task history -- so " +
				"even the one used tool goes.",
		},
		{
			"TaskGet",
			"Task-tracking family: never used once over months of active work; the " +
				"conversation is the task history.",
		},
		{
			"TaskList",
			"Task-tracking family: never used once over months of active work; the " +
				"conversation is the task history.",
		},
		{
			"TaskOutput",
			"Task-tracking family: never used once over months of active work; the " +
				"conversation is the task history.",
		},
		{
			"TaskStop",
			"Task-tracking family: never used once over months of active work; the " +
				"conversation is the task history.",
		},
		{
			"TaskUpdate",
			"Task-tracking family: never used once over months of active work; the " +
				"conversation is the task history.",
		},
	}
}

// DisallowedToolNames returns the stripped tool names, in canonical order.
func DisallowedToolNames() []string {
	entries := DisallowedToolEntries()
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Name)
	}
	return out
}
