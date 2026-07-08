"""Registry of hook script templates for deploy-hooks, with blocker/advise scripts generated from the guardrail model.

Each entry maps a script name to its content as a string constant.
Scripts are deployed to SCRIPTS_DIR (~/.claudewheel/scripts/).
"""

from __future__ import annotations

from pathlib import Path

from claudewheel import guardrail

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
    # Generated from the canonical guardrail model. See claudewheel/guardrail.py.
    "hook-block-unsafe-commands": guardrail.generate_blocker_script(),
    "hook-advise-commands": guardrail.generate_advise_script(),
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
