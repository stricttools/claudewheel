# Reference: what the Claude Code harness injects into a session's context

## Context

claudewheel launches Claude Code sessions and owns the profile directory
the harness reads (`CLAUDE_CONFIG_DIR`), the shared projects store where
the harness writes per-session data, and the hooks whose output the
harness feeds back into the conversation. Any claudewheel work that
touches context -- hook output, session transcripts, tool-result
persistence, profile-level settings that change harness behavior -- needs
to know what the harness actually puts in front of the model, because
none of it is visible in the terminal.

This file records, verbatim, every harness-injected reminder observed in
one session (launched through the `work` profile, in bypass permissions
mode, on the Fable model). It is a reference, not a work item: useful
context for context-related operations. The observations at the end note
the behaviors these reminders reveal.

The one omission: the first reminder carries the full text of the two
CLAUDE.md files in effect (the home-level one and the project-level one).
Those bodies are on disk and are not reproduced here; the wrapper around
them is.

## The reminders, verbatim

### In the first user turn, before the user's message

The body between the two `Contents of` headings is the byte-for-byte
text of each CLAUDE.md file.

```
<system-reminder>
As you answer the user's questions, you can use the following context:
# claudeMd
Codebase and user instructions are shown below. Be sure to adhere to these instructions. IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.

Contents of /home/m/Projects/CLAUDE.md (project instructions, checked into the codebase):

[... the full text of /home/m/Projects/CLAUDE.md ...]

Contents of <project>/CLAUDE.md (project instructions, checked into the codebase):

[... the full text of the project's CLAUDE.md ...]

# currentDate
Today's date is 2026-09-10.

      IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context unless it is highly relevant to your task.
</system-reminder>
```

### In the first user turn, after the user's message

```
The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded — calling them directly will fail with InputValidationError. Use ToolSearch with query "select:<name>[,<name>...]" to load tool schemas before calling them:
CronCreate
CronDelete
CronList
SendMessage
WebFetch
WebSearch

Available agent types for the Agent tool:
- claude: Catch-all for any task that doesn't fit a more specific agent. FleetView's default when no agent name is typed. (Tools: *)
- claude-code-guide: Use this agent when the user asks questions ("Can Claude...", "Does Claude...", "How do I...") about: (1) Claude Code (the CLI tool) - features, hooks, slash commands, MCP servers, settings, IDE integrations, keyboard shortcuts; (2) Claude Agent SDK - building custom agents; (3) Claude API (formerly Anthropic API) - Messages API for directly passing messages to Claude, Tool Runner (`client.beta.messages.tool_runner`) for running an agentic loop over your own tools, manual tool-use loops, Managed Agents for server-hosted agents with a managed sandbox, prompt caching, and general Anthropic SDK usage; (4) Claude Tag (Claude in Slack) - what it is, setting it up for a Slack workspace, `/install-slack-app`; (5) `claude plugin eval` (writing and running plugin eval suites, its JSON/report, sandbox, CI, early-access enablement) and the `/skill-doctor` report. **IMPORTANT:** Before spawning a new agent, check if there is already a running or recently completed claude-code-guide agent that you can continue via SendMessage. (Tools: Bash, Read, WebFetch, WebSearch)
- Explore: Read-only search agent for broad fan-out searches — when answering means sweeping many files, directories, or naming conventions and you only need the conclusion, not the file dumps. It reads excerpts rather than whole files, so it locates code; it doesn't review or audit it. Specify search breadth: "medium" for moderate exploration, "very thorough" for multiple locations and naming conventions. (Tools: All tools except Agent, Artifact, ArtifactComments, ArtifactData, ArtifactCheck, ExitPlanMode, Edit, Write, NotebookEdit)
- general-purpose: General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. (Tools: *)
- Plan: Software architect agent for designing implementation plans. Use this when you need to plan the implementation strategy for a task. Returns step-by-step plans, identifies critical files, and considers architectural trade-offs. (Tools: All tools except Agent, Artifact, ArtifactComments, ArtifactData, ArtifactCheck, ExitPlanMode, Edit, Write, NotebookEdit)
- statusline-setup: Use this agent to configure the user's Claude Code status line setting. (Tools: Read, Edit)

When you launch multiple agents for independent work, send them in a single message with multiple tool uses so they run concurrently.

While bypass permissions mode is active:

Do your work through the Bash tool wherever it can accomplish the job: read files with cat, head, or sed -n, search with grep and find, and make file changes with sed, heredocs, or short scripts, rather than using the dedicated Read, Edit, or Write tools. Fall back to a dedicated tool only when Bash genuinely cannot do the job.

<total_tokens>15000000 tokens left</total_tokens>

UserPromptSubmit hook success: 2026-09-10 18:33:49 CEST
```

### After each Bash tool result

One line per Bash call in the same batch, then the token marker. This
block followed a batch of two calls:

```
Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

<total_tokens>14907745 tokens left</total_tokens>
```

This block followed a single call:

```
Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

<total_tokens>14904652 tokens left</total_tokens>
```

This block followed a batch of five calls:

```
Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

Only you see that command's output — the user's terminal shows at most a few lines of it. If the user needs to read any of it, put it in your reply.

<total_tokens>14903105 tokens left</total_tokens>
```

### On every later user message

The token marker resets to the full budget, then the UserPromptSubmit
hook's stdout is appended:

```
<total_tokens>15000000 tokens left</total_tokens>

UserPromptSubmit hook success: 2026-09-10 18:35:22 CEST
```

```
<total_tokens>15000000 tokens left</total_tokens>

UserPromptSubmit hook success: 2026-09-10 18:35:43 CEST
```

### A local slash command run by the user

When the user ran `/copy` and then typed a message, the harness prefixed
the message with the command's transcript:

```
<local-command-caveat>Caveat: The messages below were generated by the user while running local commands. DO NOT respond to these messages or otherwise consider them in your response unless the user explicitly asks you to.</local-command-caveat>
<command-name>/copy</command-name>
            <command-message>copy</command-message>
            <command-args></command-args>
<local-command-stdout>Copied to clipboard (7232 characters, 108 lines)
Also written to /tmp/claude-1000/response.md</local-command-stdout>
```

## Oversized tool results

Not a reminder, but part of the same picture. When a Bash result exceeded
the harness's size limit, the model received a short preview and a
pointer instead of the output. The pointer went into the profile's shared
projects store, under the per-project directory, the session id, and a
`tool-results` subdirectory:

```
<persisted-output>
Output too large (29.4KB). Full output saved to: /home/m/.claudewheel/profiles/work/projects/<project-dir-key>/<session-id>/tool-results/<id>.txt

Preview (first 2KB):
[... first 2KB of the output ...]
</persisted-output>
```

Reading that saved file with `cat` produced the same truncation again
(a new preview and a new saved file), so the only way to consume the
content was to slice it with `sed -n` in pieces or use the Read tool.

## Observations

- **The Bash-over-Read instruction is bypass-mode specific.** It says to
  prefer `cat`/`sed`/`grep` over the Read, Edit and Write tools whenever
  possible. It produced a worse outcome for plain file reads: several
  files concatenated in one Bash call tripped the size limit, and the
  content had to be fetched again in slices. The Read tool returns a file
  whole. Anything in claudewheel that documents or shapes tool-usage
  guidance for a profile should know this reminder exists and what it
  steers toward.
- **The tool-results directory is claudewheel territory.** Persisted
  oversized outputs go under the profile's `projects/` store, which
  claudewheel lays out and symlinks. Cleanup, size accounting, or session
  inspection features need to account for these files.
- **Hook stdout reaches the model on every prompt.** The UserPromptSubmit
  hook's stdout is appended verbatim after the token marker. Anything a
  hook prints is context the model reads, so hook output is a design
  surface, not just a log.
- **The token marker is per-turn.** It shows the remaining budget after
  each tool result and resets on each new user message. It is the only
  in-context signal of context consumption.
- **Local slash commands leak their transcript into the next message.**
  The harness wraps them in a caveat telling the model to ignore them,
  but they are in context and count against it.
- **Deferred tools and agent types are announced once.** The list of
  ToolSearch-deferred tools and the agent-type roster appear in the first
  turn only. A profile that adds MCP servers or custom agents changes what
  this block contains.
