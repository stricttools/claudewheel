"""Registry of hook script templates for deploy-hooks command.

Each entry maps a script name to its content as a string constant.
Scripts are deployed to SCRIPTS_DIR (~/.claudewheel/scripts/).
"""

from __future__ import annotations

from pathlib import Path

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
    # Build the JSON with jq so the reason string is escaped correctly.
    # Hand-interpolating $1 into JSON breaks when the message contains quotes,
    # producing unparseable output that Claude Code silently discards.
    jq -cn --arg reason "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
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

# rm matcher: anchored rm after a separator/start, plus rm reached indirectly
# via sudo/env/xargs (allowing flag tokens) or find's -exec/-ok family.
if printf '%s' "$command" | grep -qE '(^|[;&|]|&&|\\|\\|)\\s*rm(\\s|$)|(^|\\s)(sudo|env|xargs)\\s+(-\\S+\\s+)*rm(\\s|$)|(^|\\s)-(exec|execdir|ok|okdir)\\s+rm(\\s|$)'; then
    deny "Use 'saferm delete --description \\\"why\\\" file1 file2' instead of 'rm'"
fi

exit 0
""",
}


def deploy_scripts(
    names: list[str], scripts_dir: Path, force_overwrite: bool = False
) -> list[tuple[str, str]]:
    """Write the named hook scripts into *scripts_dir*, chmod 0755.

    Returns a list of (name, action) pairs where action is one of
    "created", "overwritten", or "exists" (skipped because it already
    existed and *force_overwrite* was False). Unknown names in *names*
    raise KeyError -- callers validate against HOOK_SCRIPTS first.
    """
    scripts_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for name in names:
        dest = scripts_dir / name
        if dest.exists() and not force_overwrite:
            results.append((name, "exists"))
            continue
        action = "overwritten" if dest.exists() else "created"
        dest.write_text(HOOK_SCRIPTS[name])
        dest.chmod(0o755)
        results.append((name, action))
    return results
