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
    "hook-stamp-origin": """\
#!/usr/bin/env bash
# UserPromptSubmit hook that stamps user.origin-profile xattr on this session's
# artifacts based on which profile dir is active (resolved via CLAUDE_CONFIG_DIR).
#
# Idempotent: skips already-stamped artifacts via getfattr check, so re-firing
# on every prompt costs near-zero.
#
# Reads JSON from stdin (CC's hook payload). Required field: session_id.
# Reads CLAUDE_CONFIG_DIR from env to determine the active profile name.
#
# This hook does NOT print anything to stdout (would otherwise be injected
# into the assistant's context). Errors go to stderr.
#
# Sample install (in profile settings.json under hooks.UserPromptSubmit):
#   { "type": "command", "command": "~/.claudewheel/scripts/hook-stamp-origin" }

set -uo pipefail
shopt -s nullglob

# Read stdin
input=$(cat 2>/dev/null || true)
[[ -z "$input" ]] && exit 0

uuid=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[[ -z "$uuid" ]] && exit 0

# Resolve profile name from CLAUDE_CONFIG_DIR (e.g. /home/m/.claude-personal -> personal)
cfg="${CLAUDE_CONFIG_DIR:-}"
[[ -z "$cfg" ]] && exit 0
cfg_base=$(basename "$cfg")
if [[ "$cfg_base" == ".claude" ]]; then
  profile="default"
else
  profile="${cfg_base#.claude-}"
fi
[[ -z "$profile" ]] && exit 0

SHARED="$HOME/.claude-shared"
SENTINEL="$SHARED/.stamped-$uuid"

# Fast path: if this session was already stamped, skip all work
[[ -e "$SENTINEL" ]] && exit 0

ROOTS=()
[[ -d "$SHARED" ]] && ROOTS+=("$SHARED")
[[ -d "$cfg" ]]    && ROOTS+=("$cfg")

INDEX="$HOME/.claudewheel/profile-origins.jsonl"
TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

stamped_any=0

stamp_if_unstamped() {
  local path="$1"
  [[ -e "$path" ]] || return 0
  if getfattr --only-values -n user.origin-profile "$path" 2>/dev/null >/dev/null; then
    return 0
  fi
  if setfattr -n user.origin-profile -v "$profile" "$path" 2>/dev/null; then
    (
      flock -n 9 || flock 9
      jq -nc --arg path "$path" --arg profile "$profile" --arg ts "$TS" \\
        '{path:$path, profile:$profile, ts:$ts, phase:"hook"}' >> "$INDEX" 2>/dev/null
    ) 9>"${INDEX}.lock"
    stamped_any=1
  fi
}

for root in "${ROOTS[@]}"; do
  for jsonl in "$root/projects"/*"/$uuid.jsonl"; do
    stamp_if_unstamped "$jsonl"
  done
  for sub in "$root/projects"/*/"$uuid"; do
    [[ -d "$sub" ]] && stamp_if_unstamped "$sub"
  done
  for d in session-env file-history tasks; do
    [[ -e "$root/$d/$uuid" ]] && stamp_if_unstamped "$root/$d/$uuid"
  done
  for todo in "$root/todos/$uuid"-agent-*.json; do
    stamp_if_unstamped "$todo"
  done
done

# Create sentinel so subsequent prompts in this session skip all work
touch "$SENTINEL" 2>/dev/null

exit 0
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
}
