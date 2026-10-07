#!/usr/bin/env bash
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
[[ "$session" =~ @SESSION_UUID_RE@ ]] ||
    fail "payload carries no Claude Code session uuid: '$session'"

# Where the store is: claudewheel says so outright when it launched the session;
# otherwise this mirrors Workspace.default() -- the CLAUDEWHEEL_CONFIG_DIR root,
# or ~/.claudewheel. An environment with none of the three (a stripped service
# env with no HOME) is the hook's own diagnostic, not bash's unbound-variable
# abort. Nothing is created here: the store is touched only once every check
# below has passed.
lifecycle_dir="${CLAUDEWHEEL_LIFECYCLE_DIR:-}"
if [[ -z "$lifecycle_dir" ]]; then
    cw_root="${CLAUDEWHEEL_CONFIG_DIR:-${HOME:+${HOME}/.claudewheel}}"
    [[ -n "$cw_root" ]] ||
        fail 'cannot resolve the lifecycle directory: set CLAUDEWHEEL_LIFECYCLE_DIR, CLAUDEWHEEL_CONFIG_DIR or HOME'
    lifecycle_dir="$cw_root/shared/lifecycle"
fi
file="$lifecycle_dir/$session.jsonl"

config_dir="${CLAUDE_CONFIG_DIR:-${HOME:+${HOME}/.claude}}"
[[ -n "$config_dir" ]] ||
    fail 'cannot resolve the config directory: set CLAUDE_CONFIG_DIR or HOME'

# Claude Code's own per-session registry, which holds the display name, the pid
# and the client version while the process lives. No directory, or no record for
# this session, leaves every field null.
#
# Each value is carried as JSON TEXT (`"a\tb"`, `4242`, `null`) from the record
# straight into the final jq's --argjson, so a name holding a tab, a newline or a
# backslash is never decoded, re-encoded or split by the shell on its way to the
# line.
reg_name=null
reg_name_source=null
reg_pid=null
reg_version=null
if [[ -d "$config_dir/sessions" ]]; then
    for record in "$config_dir"/sessions/*.json; do
        [[ -f "$record" ]] || continue
        match=$(jq -r --arg s "$session" \
            'if .sessionId == $s then "yes" else "no" end' "$record" 2>/dev/null)
        [[ "$match" == "yes" ]] || continue
        reg_name=$(jq -c '.name // null' "$record" 2>/dev/null)
        reg_name_source=$(jq -c '.nameSource // null' "$record" 2>/dev/null)
        # A pid reaches the line as a JSON number or not at all.
        reg_pid=$(jq -c 'if (.pid|type) == "number" then .pid else null end' \
            "$record" 2>/dev/null)
        reg_version=$(jq -c '.version // null' "$record" 2>/dev/null)
        break
    done
fi
# A jq that failed mid-read leaves its variable empty, which is not JSON.
[[ -n "$reg_name" ]] || reg_name=null
[[ -n "$reg_name_source" ]] || reg_name_source=null
[[ -n "$reg_pid" ]] || reg_pid=null
[[ -n "$reg_version" ]] || reg_version=null

# The Claude Code process's own pid, which Claude Code exports to its hooks as
# CLAUDE_PID, beats the registry's: the registry record may not be written yet
# when SessionStart runs, and the probe runner finds a session scope's session
# (claudewheel-session-<pid>-<time>.scope) through this line's pid.
if [[ -n "${CLAUDE_PID:-}" ]]; then
    [[ "$CLAUDE_PID" =~ ^[1-9][0-9]*$ ]] ||
        fail "CLAUDE_PID is not a process id: '$CLAUDE_PID'"
    reg_pid=$CLAUDE_PID
fi

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
    # No name, or an empty one, is no `named` line. Both tests are against the
    # JSON text, which is why '""' is spelled out.
    [[ "$reg_name" != null && "$reg_name" != '""' ]] || return 0
    mint
    line=$(jq -cn \
        --arg id "$event_id" \
        --arg at "$event_at" \
        --arg session "$session" \
        --argjson name "$reg_name" \
        --argjson name_source "$reg_name_source" \
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
# claudewheel did not launch this session. The registry version stays JSON text
# so it is never decoded, which is why jq picks between the two rather than bash.
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
    --arg launch_version "${CLAUDEWHEEL_LAUNCH_VERSION:-}" \
    --argjson reg_version "$reg_version" \
    --arg model "$model" \
    --arg permissions "${CLAUDEWHEEL_LAUNCH_PERMISSIONS:-}" \
    --arg entry "$entry" \
    --arg transcript "$transcript" \
    --argjson pid "$reg_pid" \
    '{format_version: 1, id: $id, at: $at, session: $session,
      source: "hook", kind: "started", cwd: $cwd, config_dir: $config_dir,
      profile: (if $profile == "" then null else $profile end),
      claude_version: (if $launch_version != "" then $launch_version
                       elif $reg_version == "" then null
                       else $reg_version end),
      model: (if $model == "" then null else $model end),
      permissions: (if $permissions == "" then null else $permissions end),
      entry: $entry,
      transcript: (if $transcript == "" then null else $transcript end),
      pid: $pid}' 2>/dev/null)

# The first thing that touches the disk, after every check above has passed.
mkdir -p "$lifecycle_dir" 2>/dev/null || fail "cannot create $lifecycle_dir"
append "$started" started

append_named

exit 0
