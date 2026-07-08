---
title: Guardrails
description: "How claudewheel guardrails work: the four enforcement tiers, subagent-versus-main-agent handling, command-string caveats, and upgrading existing profiles."
nav_group: "Concepts"
order: 5
---

# Guardrails

claudewheel ships a canonical set of command guardrails that every profile
inherits. The guardrails discourage or block destructive shell commands (bulk
`git add`, `rm`, history rewrites, branch deletion) and steer agents toward
safe alternatives such as `safegit` and `saferm`. The rules live in one place
and drive both the deployed hook scripts and each profile's permission arrays.

## Enforcement tiers

Every rule belongs to exactly one of four tiers. The tier decides where the
rule is enforced (a `PreToolUse`/`PostToolUse` hook, the settings
`deny`/`ask` arrays, or both) and who it applies to. The hook is always the
authoritative enforcer when a rule has one; the settings arrays are
best-effort defense-in-depth for the plain command form.

- **HARD_DENY** -- denied for everyone, both the main agent and subagents, via
  a `PreToolUse` hook. A backing settings `deny` glob may exist as
  defense-in-depth, but it never reproduces the hook's full match surface
  (compound commands, `sudo`/`env`/`xargs`/`find -exec` wrappers, alternate
  remotes). Some HARD_DENY rules own no deny glob at all.
- **ESCALATE** -- denied only when a subagent attempts the command. The main
  agent falls through the hook silently so the settings `ask` rule prompts the
  user to approve it deliberately.
- **ADVISE** -- the command runs, then a `PostToolUse` hook nudges the agent
  with advice via `additionalContext`. There are no settings entries and
  nothing is blocked.
- **ASK** -- a pure settings `ask` rule with no hook involvement. The user is
  prompted before the command runs.

## Subagents versus the main agent

The blocker hook distinguishes a subagent from the main agent using the
`agent_id` field in the `PreToolUse` payload. Claude Code populates
`agent_id` only for subagent tool calls, so a non-empty `agent_id` marks a
subagent. HARD_DENY rules block regardless of `agent_id`, while ESCALATE rules
block only when `agent_id` is set and otherwise let the main agent through.

This is why an ESCALATE command like `git push` is refused outright for a
subagent (with a message telling it to report to its parent) but merely prompts
the user when the main agent runs it. The distinction keeps risky,
outward-facing actions in the hands of the human-supervised main agent.

## Command-string caveat

The hooks match against the raw command string with `grep -qE`, anchored to
the start of a shell segment. They do not parse the shell. A command that only
*mentions* a guarded token -- for example inside an `echo`, a `grep` pattern, a
comment, or a heredoc -- can still trip the matcher and be nudged or blocked
even though nothing dangerous would actually run.

This is a deliberate trade-off: false positives are safe (you rephrase or
split the command), whereas parsing the shell to eliminate them would be far
more fragile than a conservative string match. When a benign command is
blocked, move the guarded token out of the command line or run the pieces
separately.

## Upgrading existing profiles

The guardrail model evolves between releases. Existing profiles keep whatever
rules were current when they were created, so after upgrading claudewheel you
should re-apply the canonical model to bring older profiles up to date:

- Run `claudewheel reconcile-permissions --apply` to rewrite each profile's
  `deny`/`ask`/`allow` permission arrays to match the current model.
- Run `claudewheel patch-profiles` to sync the deployed hook scripts and
  `disallowedTools` defaults into every profile and `shared-settings.json`.

Both commands support `--dry-run` so you can preview the changes before writing
anything to disk.

## Rule reference

The table below is generated directly from the canonical rule set, so it always
reflects the guardrails shipped in this version. "Settings coverage" reports how
completely a rule's `deny`/`ask` glob(s) track its hook surface (FULL, PARTIAL,
or NONE), or `n/a` for tiers with no settings backstop.

:-: table-guardrails
