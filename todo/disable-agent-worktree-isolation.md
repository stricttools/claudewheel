# Disable Agent worktree isolation

## Problem

claudewheel disables `EnterWorktree` and `ExitWorktree` in `DISALLOWED_TOOLS`, which prevents the main session from entering a worktree directly. However, the `Agent` tool has a separate `isolation: "worktree"` parameter that creates a temporary git worktree for subagents. This may bypass the disallowedTools restriction since it's handled by the Agent infrastructure, not by calling EnterWorktree/ExitWorktree.

## Context

Multiple Claude Code sessions share the same worktree. Worktree isolation on subagents could create conflicts with other sessions' work, defeating the multi-session safety that claudewheel is designed to provide.

## Solution

Investigate whether `isolation: "worktree"` on the Agent tool is blocked when EnterWorktree is in disallowedTools. If not, find a way to disable it -- either via an additional disallowedTools entry if one exists, or by documenting it as a known gap.
