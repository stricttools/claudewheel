# Disable the fork subagent type entirely

## Context

Claude Code's Agent tool supports `subagent_type: "fork"`, which clones the
parent session's entire conversation context into the child agent. The owner
has banned its use fleet-wide (see ~/Projects/CLAUDE.md, "Never use fork
subagents"): subagents exist to provide clean, empty context windows primed
with exactly what a task needs, and a fork is the structural opposite.

## Problem

An instruction-level ban depends on every session reading and honoring it.
The mechanical enforcement belongs in claudewheel's profile settings: the
shared settings layer already carries a disallowedTools list inherited by
every profile.

## Solution

Add the fork form of the Agent/Task tool to disallowedTools in
~/.claudewheel/shared-settings.json so the harness refuses it in every
profile regardless of session discipline. First verify the exact matcher
syntax Claude Code supports for tool rules (whether a subagent-type-scoped
matcher like "Task(fork)" exists, or whether hooks are the right lever if
disallowedTools cannot see the subagent_type parameter) and use the
narrowest rule that blocks only forks, never the Agent tool generally. If
only a hook can inspect the parameter, add a PreToolUse hook that rejects
Agent calls whose subagent_type is "fork".

## Affected files

- ~/.claudewheel/shared-settings.json (disallowedTools or hooks)
- claudewheel's settings-reconciliation tests, if the shared list is pinned

## Effort

Small — a settings or hook change plus verification of the matcher syntax.
