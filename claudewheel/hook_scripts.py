"""Registry of hook script templates for deploy-hooks command.

Each entry maps a script name to its content as a string constant.
Scripts are deployed to SCRIPTS_DIR (~/.claudewheel/scripts/).
"""

HOOK_SCRIPTS: dict[str, str] = {
    "hook-timestamp": """\
#!/usr/bin/env bash
# Injects current timestamp into Claude's context for temporal awareness.
echo "$(date '+%Y-%m-%d %H:%M:%S %Z')"
""",
    "hook-block-worktree": """\
#!/usr/bin/env bash
# PreToolUse hook that blocks Agent tool calls with isolation:"worktree".
#
# Reads JSON from stdin (CC's hook payload). If the tool is "Agent" and
# tool_input.isolation is "worktree", denies the call. Otherwise exits
# silently to allow normal processing.

set -uo pipefail

input=$(cat 2>/dev/null || true)
[[ -z "$input" ]] && exit 0

tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" != "Agent" ]] && exit 0

isolation=$(printf '%s' "$input" | jq -r '.tool_input.isolation // empty' 2>/dev/null)
[[ "$isolation" != "worktree" ]] && exit 0

# Block the worktree-isolated Agent call
printf '%s' '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Worktree isolation is blocked by policy."}}'
exit 0
""",
    "hook-block-unsafe-commands": """\
#!/usr/bin/env bash
# PreToolUse hook that blocks unsafe git/rm commands and suggests safe alternatives.
#
# Reads JSON from stdin (CC's hook payload). If the tool is "Bash",
# inspects the command string for forbidden patterns (git add, git stash,
# git restore, git checkout --, rm). Denies with an actionable message
# explaining what to use instead. Otherwise exits silently.

set -uo pipefail

input=$(cat 2>/dev/null || true)
[[ -z "$input" ]] && exit 0

tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" != "Bash" ]] && exit 0

command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -z "$command" ]] && exit 0

deny() {
    printf '%s' "{\\"hookSpecificOutput\\": {\\"hookEventName\\": \\"PreToolUse\\", \\"permissionDecision\\": \\"deny\\", \\"permissionDecisionReason\\": \\"$1\\"}}"
    exit 0
}

# Check for forbidden patterns
if printf '%s' "$command" | grep -qE '(^|[;&|]|&&|\\|\\|)\\s*git\\s+add\\s+(-[AuU]|--all|\\.)'; then
    deny "Use 'safegit commit -- file1 file2' instead of 'git add'"
fi

if printf '%s' "$command" | grep -qE '(^|[;&|]|&&|\\|\\|)\\s*git\\s+stash'; then
    deny "Use 'safegit commit' on a temporary branch instead of 'git stash'"
fi

if printf '%s' "$command" | grep -qE '(^|[;&|]|&&|\\|\\|)\\s*git\\s+restore'; then
    deny "Use the Edit tool to revert specific lines instead of 'git restore'"
fi

if printf '%s' "$command" | grep -qE '(^|[;&|]|&&|\\|\\|)\\s*git\\s+checkout\\s+--\\s'; then
    deny "Use the Edit tool to revert specific lines instead of 'git checkout -- file'"
fi

if printf '%s' "$command" | grep -qE '(^|[;&|]|&&|\\|\\|)\\s*rm\\s'; then
    deny "Use 'saferm delete --description \\\"why\\\" file1 file2' instead of 'rm'"
fi

exit 0
""",
}
