"""Registry of hook script templates for deploy-hooks, with blocker/advise scripts generated from the guardrail model.

Each entry maps a script name to its content as a string constant.
Scripts are deployed to SCRIPTS_DIR (~/.claudewheel/scripts/).
"""

from __future__ import annotations

from pathlib import Path

from claudewheel import guardrail

from . import effects

# The two lifecycle hooks are held in raw strings: their bash contains
# backslash escapes of its own (``IFS=$'\t'``, ``tr -d ' \n'``) that a regular
# Python string would eat before bash ever saw them.
#
# Everything from `set -uo pipefail` down to `exit 0` in the first script is
# repeated verbatim in the second, on purpose: a deployed hook is ONE file that
# Claude Code runs directly, so neither may depend on a shared include sitting
# next to it.

_SESSION_START_SCRIPT = r"""#!/usr/bin/env bash
# SessionStart hook: record a `started` line in claudewheel's per-session
# lifecycle store, plus a `named` line when Claude Code's session registry
# already carries a display name.
#
# Claude Code's registry exists only while a session's process lives, so once a
# session is gone nothing says it ever ran. This hook writes that record from
# the one moment every launch fact is readable at once: claudewheel states what
# it chose in the environment (CLAUDEWHEEL_LAUNCH_*), Claude Code hands the
# session's own facts in on stdin, and the registry still has the pid and name.
#
# The hook never blocks a session. Every failure prints one line to stderr and
# exits 1 -- never 2, which Claude Code reads as "block this event" -- and a
# line that cannot be built is never half-written.

set -uo pipefail

fail() {
    printf 'hook-session-start: %s\n' "$1" >&2
    exit 1
}

command -v jq >/dev/null 2>&1 || fail 'jq not found'

input=$(cat 2>/dev/null || true)

session=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[[ "$session" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
    fail "payload carries no Claude Code session uuid: '$session'"

# Where the store is: claudewheel says so outright when it launched the session;
# otherwise this mirrors Workspace.default() -- the CLAUDEWHEEL_CONFIG_DIR root,
# or ~/.claudewheel.
lifecycle_dir="${CLAUDEWHEEL_LIFECYCLE_DIR:-${CLAUDEWHEEL_CONFIG_DIR:-$HOME/.claudewheel}/shared/lifecycle}"
mkdir -p "$lifecycle_dir" 2>/dev/null || fail "cannot create $lifecycle_dir"
file="$lifecycle_dir/$session.jsonl"

config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Claude Code's own per-session registry, which holds the display name, the pid
# and the client version while the process lives. No directory, or no record for
# this session, leaves every field empty, and the line then says null.
reg_name=""
reg_name_source=""
reg_pid=""
reg_version=""
if [[ -d "$config_dir/sessions" ]]; then
    for record in "$config_dir"/sessions/*.json; do
        [[ -f "$record" ]] || continue
        found=$(jq -r --arg s "$session" \
            'select(.sessionId == $s)
             | [(.name // ""), (.nameSource // ""), ((.pid // "") | tostring), (.version // "")]
             | @tsv' "$record" 2>/dev/null)
        if [[ -n "$found" ]]; then
            IFS=$'\t' read -r reg_name reg_name_source reg_pid reg_version <<<"$found"
            break
        fi
    done
fi
# A pid reaches the line as a JSON number or not at all.
[[ "$reg_pid" =~ ^[0-9]+$ ]] || reg_pid=""

event_id=""
event_at=""

# One id and one timestamp per event. The id is 16 hex digits of nanosecond
# clock (so ids sort by creation order) then 32 hex digits of randomness (so two
# ids minted in the same nanosecond still differ); the timestamp is fixed-width
# RFC 3339 UTC, so lexical order over timestamps IS time order. Both match
# lifecycle.new_event_id() / lifecycle.now_timestamp().
mint() {
    local rand
    rand=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
    [[ ${#rand} -eq 32 ]] || fail 'cannot read 16 random bytes from /dev/urandom'
    event_id=$(printf '%016x%s' "$(date +%s%N)" "$rand")
    event_at=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    [[ -n "$event_at" ]] || fail 'cannot read the clock'
}

append() {
    # $1 is one whole JSON line; $2 names it in a diagnostic. One printf per
    # line, so a concurrent writer cannot interleave inside a line.
    [[ -n "$1" ]] || fail "cannot build the $2 line"
    printf '%s\n' "$1" >> "$file" || fail "cannot append to $file"
}

append_named() {
    local line
    [[ -n "$reg_name" ]] || return 0
    mint
    line=$(jq -cn \
        --arg id "$event_id" \
        --arg at "$event_at" \
        --arg session "$session" \
        --arg name "$reg_name" \
        --arg name_source "$reg_name_source" \
        '{format_version: 1, id: $id, at: $at, session: $session,
          source: "hook", kind: "named", name: $name,
          name_source: (if $name_source == "" then null else $name_source end)}' \
        2>/dev/null)
    append "$line" named
}

entry=$(printf '%s' "$input" | jq -r '.source // empty' 2>/dev/null)
# A compaction is not a process start: the same session kept running, so there
# is nothing to record.
[[ "$entry" == "compact" ]] && exit 0
case "$entry" in
    startup | resume | clear | fork) ;;
    *) fail "unrecognized SessionStart source: '$entry'" ;;
esac

cwd=$(printf '%s' "$input" | jq -r '.cwd // empty' 2>/dev/null)
[[ -n "$cwd" ]] || fail 'payload carries no cwd'

transcript=$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)

# What claudewheel chose beats what the session reports, because claudewheel
# chose it. The version and the model have a second source (the registry, and
# the payload); a profile and a permissions mode do not -- their absence means
# claudewheel did not launch this session.
claude_version="${CLAUDEWHEEL_LAUNCH_VERSION:-}"
[[ -n "$claude_version" ]] || claude_version="$reg_version"
model="${CLAUDEWHEEL_LAUNCH_MODEL:-}"
[[ -n "$model" ]] || model=$(printf '%s' "$input" | jq -r '.model // empty' 2>/dev/null)

mint
started=$(jq -cn \
    --arg id "$event_id" \
    --arg at "$event_at" \
    --arg session "$session" \
    --arg cwd "$cwd" \
    --arg config_dir "$config_dir" \
    --arg profile "${CLAUDEWHEEL_LAUNCH_PROFILE:-}" \
    --arg claude_version "$claude_version" \
    --arg model "$model" \
    --arg permissions "${CLAUDEWHEEL_LAUNCH_PERMISSIONS:-}" \
    --arg entry "$entry" \
    --arg transcript "$transcript" \
    --argjson pid "${reg_pid:-null}" \
    '{format_version: 1, id: $id, at: $at, session: $session,
      source: "hook", kind: "started", cwd: $cwd, config_dir: $config_dir,
      profile: (if $profile == "" then null else $profile end),
      claude_version: (if $claude_version == "" then null else $claude_version end),
      model: (if $model == "" then null else $model end),
      permissions: (if $permissions == "" then null else $permissions end),
      entry: $entry,
      transcript: (if $transcript == "" then null else $transcript end),
      pid: $pid}' 2>/dev/null)
append "$started" started

append_named

exit 0
"""

_SESSION_END_SCRIPT = r"""#!/usr/bin/env bash
# SessionEnd hook: record an `ended` line in claudewheel's per-session lifecycle
# store, preceded by a `named` line when Claude Code's session registry still
# carries a display name.
#
# The name is recorded here too because the registry file is unlinked as the
# process exits: this is the last moment the session's own name is readable, and
# a session that is never seen again would otherwise be nameless forever.
#
# The hook never blocks a session. Every failure prints one line to stderr and
# exits 1 -- never 2, which Claude Code reads as "block this event" -- and a
# line that cannot be built is never half-written.

set -uo pipefail

fail() {
    printf 'hook-session-end: %s\n' "$1" >&2
    exit 1
}

command -v jq >/dev/null 2>&1 || fail 'jq not found'

input=$(cat 2>/dev/null || true)

session=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[[ "$session" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] ||
    fail "payload carries no Claude Code session uuid: '$session'"

# Where the store is: claudewheel says so outright when it launched the session;
# otherwise this mirrors Workspace.default() -- the CLAUDEWHEEL_CONFIG_DIR root,
# or ~/.claudewheel.
lifecycle_dir="${CLAUDEWHEEL_LIFECYCLE_DIR:-${CLAUDEWHEEL_CONFIG_DIR:-$HOME/.claudewheel}/shared/lifecycle}"
mkdir -p "$lifecycle_dir" 2>/dev/null || fail "cannot create $lifecycle_dir"
file="$lifecycle_dir/$session.jsonl"

config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Claude Code's own per-session registry, which holds the display name, the pid
# and the client version while the process lives. No directory, or no record for
# this session, leaves every field empty, and the line then says null.
reg_name=""
reg_name_source=""
reg_pid=""
reg_version=""
if [[ -d "$config_dir/sessions" ]]; then
    for record in "$config_dir"/sessions/*.json; do
        [[ -f "$record" ]] || continue
        found=$(jq -r --arg s "$session" \
            'select(.sessionId == $s)
             | [(.name // ""), (.nameSource // ""), ((.pid // "") | tostring), (.version // "")]
             | @tsv' "$record" 2>/dev/null)
        if [[ -n "$found" ]]; then
            IFS=$'\t' read -r reg_name reg_name_source reg_pid reg_version <<<"$found"
            break
        fi
    done
fi
# A pid reaches the line as a JSON number or not at all.
[[ "$reg_pid" =~ ^[0-9]+$ ]] || reg_pid=""

event_id=""
event_at=""

# One id and one timestamp per event. The id is 16 hex digits of nanosecond
# clock (so ids sort by creation order) then 32 hex digits of randomness (so two
# ids minted in the same nanosecond still differ); the timestamp is fixed-width
# RFC 3339 UTC, so lexical order over timestamps IS time order. Both match
# lifecycle.new_event_id() / lifecycle.now_timestamp().
mint() {
    local rand
    rand=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
    [[ ${#rand} -eq 32 ]] || fail 'cannot read 16 random bytes from /dev/urandom'
    event_id=$(printf '%016x%s' "$(date +%s%N)" "$rand")
    event_at=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    [[ -n "$event_at" ]] || fail 'cannot read the clock'
}

append() {
    # $1 is one whole JSON line; $2 names it in a diagnostic. One printf per
    # line, so a concurrent writer cannot interleave inside a line.
    [[ -n "$1" ]] || fail "cannot build the $2 line"
    printf '%s\n' "$1" >> "$file" || fail "cannot append to $file"
}

append_named() {
    local line
    [[ -n "$reg_name" ]] || return 0
    mint
    line=$(jq -cn \
        --arg id "$event_id" \
        --arg at "$event_at" \
        --arg session "$session" \
        --arg name "$reg_name" \
        --arg name_source "$reg_name_source" \
        '{format_version: 1, id: $id, at: $at, session: $session,
          source: "hook", kind: "named", name: $name,
          name_source: (if $name_source == "" then null else $name_source end)}' \
        2>/dev/null)
    append "$line" named
}

# Only Claude Code's own SessionEnd vocabulary is recorded; anything else is
# recorded as unknown rather than passed through as if it meant something.
reason=$(printf '%s' "$input" | jq -r '.reason // empty' 2>/dev/null)
case "$reason" in
    clear | resume | logout | prompt_input_exit | other) ;;
    *) reason="" ;;
esac

append_named

mint
ended=$(jq -cn \
    --arg id "$event_id" \
    --arg at "$event_at" \
    --arg session "$session" \
    --arg reason "$reason" \
    '{format_version: 1, id: $id, at: $at, session: $session,
      source: "hook", kind: "ended", outcome: "exited",
      reason: (if $reason == "" then null else $reason end), detail: null}' \
    2>/dev/null)
append "$ended" ended

exit 0
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

# Block the worktree-isolated Agent call.
# Build the JSON with jq so the reason string is escaped correctly.
# Hand-interpolating the reason into JSON breaks when the message contains
# quotes, producing unparseable output that Claude Code silently discards.
reason="Worktree isolation is blocked by policy."
jq -cn --arg reason "$reason" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
exit 0
""",
    "hook-session-start": _SESSION_START_SCRIPT,
    "hook-session-end": _SESSION_END_SCRIPT,
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
    effects.mkdir(scripts_dir, parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for name in names:
        dest = scripts_dir / name
        if dest.exists() and not force_overwrite:
            results.append((name, "exists"))
            continue
        action = "overwritten" if dest.exists() else "created"
        effects.write_text(dest, HOOK_SCRIPTS[name])
        effects.chmod(dest, 0o755)
        results.append((name, action))
    return results
