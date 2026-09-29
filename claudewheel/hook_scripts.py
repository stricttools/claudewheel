"""Registry of the scripts deploy-hooks deploys: the hook scripts, with blocker/advise generated from the guardrail model, and the heavy wrapper.

Each entry maps a script name to its content as a string constant.
Scripts are deployed to SCRIPTS_DIR (~/.claudewheel/scripts/). The scripts
named in ``PATH_COMMANDS`` are commands rather than hooks, and are also linked
into the user's ``~/.local/bin`` so they are on PATH.
"""

from __future__ import annotations

import os
from pathlib import Path

from claudewheel import guardrail

from . import effects

# The two lifecycle hooks are held in raw strings: their bash contains
# backslash escapes of its own (``tr -d ' \n'``, ``printf '%s\n'``) that a
# regular Python string would eat before bash ever saw them.
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

# Only Claude Code's own SessionEnd vocabulary is recorded; anything else is
# recorded as unknown rather than passed through as if it meant something.
reason=$(printf '%s' "$input" | jq -r '.reason // empty' 2>/dev/null)
case "$reason" in
    clear | resume | logout | prompt_input_exit | other) ;;
    *) reason="" ;;
esac

# The first thing that touches the disk, after every check above has passed.
mkdir -p "$lifecycle_dir" 2>/dev/null || fail "cannot create $lifecycle_dir"

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

# The heavy wrapper: the command the heavy-unwrapped guardrail rule tells an
# agent to run instead of a bare test suite or large build. Help prints the
# leading comment block, so the note on where the wrapper comes from sits below
# it.
_HEAVY_SCRIPT = r"""#!/usr/bin/env bash
# heavy: run a memory-heavy command (a test suite, a large build, a long
# verification) under a memory cap, once this machine has the memory for it.
#
#   heavy [--mem SIZE] [--max-wait TIME] [--] command [args...]
#
# Every heavy command declares its memory cap, SIZE: a whole number with a K,
# M, G, or T suffix (512M, 8G); the default is 5G, and a smaller cap starts
# sooner when others are running. The command starts only when its cap fits in
# the memory budget: this machine's MemAvailable (from /proc/meminfo) less a 2G
# margin, less the part of every running heavy command's cap that the command
# has not used yet. Running heavy commands are counted by the heavy scopes
# still running, so a command whose heavy was killed keeps its cap reserved
# until its scope ends. At most 8 heavy commands run at once. A cap larger
# than the machine's whole memory less the margin is a usage error.
#
# Until the command fits, heavy checks again every second and prints, every 30
# seconds, how long it has waited, the memory figures, and every running heavy
# command; it gives up (exit 75) after TIME, naming them and how to stop them.
# TIME is a whole number of seconds, minutes, or hours (90s, 60m, 2h); the
# default is 60m.
#
# The command runs in its own systemd user scope with MemoryMax=SIZE and no
# swap, so a runaway is killed alone instead of the whole terminal session, and
# with CPUWeight=20 (the default is 100), so interactive work stays responsive.
# heavy prints the cap on every run; when the command is killed for going over
# it, heavy says so and suggests a cap twice as large. When the command
# returns, heavy stops its scope, so whatever it left running stops too.
#
# GOFLAGS gets -p=2 (at most two Go packages built or tested at once) unless it
# already sets -p; heavy prints a line when it adds it. heavy exits with the
# command's exit status. HEAVY_REPORT_EVERY sets the waiting report interval in
# seconds.
set -euo pipefail

# Shipped by claudewheel (claudewheel/hook_scripts.py) and deployed by
# 'claudewheel deploy-hooks heavy'; edits to a deployed copy are overwritten.

usage() {
    echo "heavy: $1; usage: heavy [--mem SIZE] [--max-wait TIME] [--] command [args...]" >&2
    exit 2
}

mem=5G
max_wait=60m
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mem)
            [[ $# -ge 2 && "$2" != --* ]] || usage "--mem needs a size, such as 8G"
            mem="$2"; shift 2 ;;
        --mem=*) mem="${1#--mem=}"; shift ;;
        --max-wait)
            [[ $# -ge 2 && "$2" != --* ]] || usage "--max-wait needs a time, such as 90m"
            max_wait="$2"; shift 2 ;;
        --max-wait=*) max_wait="${1#--max-wait=}"; shift ;;
        --) shift; break ;;
        -h|--help) awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
        *) break ;;
    esac
done
[[ "$mem" =~ ^[1-9][0-9]*[KMGT]$ ]] \
    || usage "--mem takes a whole number with a K, M, G, or T suffix (512M, 8G), not '$mem'"
[[ "$max_wait" =~ ^[0-9]+[smh]$ ]] \
    || usage "--max-wait takes a whole number of seconds, minutes, or hours (90s, 60m, 2h), not '$max_wait'"
if [[ $# -eq 0 ]]; then
    usage "no command given"
fi

case "$max_wait" in
    *s) max_wait_s=$((10#${max_wait%s})) ;;
    *m) max_wait_s=$((10#${max_wait%m} * 60)) ;;
    *h) max_wait_s=$((10#${max_wait%h} * 3600)) ;;
esac
report_every="${HEAVY_REPORT_EVERY:-30}"
[[ "$report_every" =~ ^[1-9][0-9]*$ ]] || report_every=30

# The budget's constants. The margin is memory no heavy command may count on:
# the desktop and the Claude sessions grow while a command runs, and swap is
# never counted, because the zram swap it goes to first lives in RAM too.
margin_kib=$((2 * 1024 * 1024))
slot_count=8

# A size with a K, M, G, or T suffix, in KiB (1024-based, as systemd reads it).
to_kib() {
    local n=${1%?}
    case "${1: -1}" in
        K) echo "$n" ;;
        M) echo $((n * 1024)) ;;
        G) echo $((n * 1024 * 1024)) ;;
        T) echo $((n * 1024 * 1024 * 1024)) ;;
    esac
}

# KiB as gigabytes with one decimal (5242880 -> 5.0G).
gib() {
    awk -v k="$1" 'BEGIN { printf "%.1fG", k / 1048576 }'
}

# A /proc/meminfo figure in KiB, such as MemAvailable.
meminfo() {
    awk -v key="$1:" '$1 == key { print $2 }' /proc/meminfo
}

cap_kib=$(to_kib "$mem")
total_kib=$(meminfo MemTotal)
if ((cap_kib > total_kib - margin_kib)); then
    usage "--mem $mem is more than this machine can ever give a heavy command ($(gib "$total_kib") of memory less the 2G margin is $(gib $((total_kib - margin_kib))))"
fi

dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
lock="$dir/heavy.lock"
slots="$dir/heavy.slots"
unit="heavy-$$-$(date +%s).scope"
# One line, as the slot note keeps it.
command_line="$*"
command_line=${command_line//$'\n'/ }
mkdir -p "$slots"

# The kernel start time of a process (field 22 of /proc/PID/stat), which tells
# a live process from a reused PID.
start_time() {
    local stat rest fields
    stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    rest=${stat##*) }
    read -r -a fields <<<"$rest"
    printf '%s' "${fields[19]:-}"
}

# Reads a heavy scope's cgroup: scope_used_kib is what the scope holds now, in
# KiB, less its page cache, which the kernel reclaims before it would kill the
# command (0 while the scope has not started or cannot be read); scope_cap_kib
# is its MemoryMax in KiB, empty when it has none or it cannot be read.
read_scope() {
    local cg cur file max
    scope_used_kib=0
    scope_cap_kib=""
    cg=$(systemctl --user show -P ControlGroup "$1" 2>/dev/null || true)
    [[ "$cg" == /* && -r "/sys/fs/cgroup$cg/memory.current" ]] || return 0
    cur=$(cat "/sys/fs/cgroup$cg/memory.current" 2>/dev/null || echo 0)
    file=$(awk '$1 == "file" { print $2 }' "/sys/fs/cgroup$cg/memory.stat" 2>/dev/null || true)
    [[ "$cur" =~ ^[0-9]+$ ]] || cur=0
    [[ "${file:-0}" =~ ^[0-9]+$ ]] || file=0
    scope_used_kib=$(((cur - ${file:-0}) / 1024))
    ((scope_used_kib > 0)) || scope_used_kib=0
    max=$(cat "/sys/fs/cgroup$cg/memory.max" 2>/dev/null || true)
    if [[ "$max" =~ ^[0-9]+$ ]]; then
        scope_cap_kib=$((max / 1024))
    fi
}

# One admission attempt, made while this heavy holds the admission lock.
#
# Each slot is a file that a heavy holds a flock on for as long as it runs, and
# a slot's note (its content) says who holds it: "PID START_TIME", then
# "CAP_KIB SIZE UNIT", then a description. A slot is taken while, and only
# while, a live process holds its flock, so a heavy that dies in any way,
# SIGKILL included, frees its slot and its share of the budget at once. A note
# is believed only while its PID is alive with its start time; a taken slot
# whose note is not believed counts as reserving all the memory there is.
#
# On success the claimed slot stays locked on descriptor 8 and its note names
# this heavy. Otherwise nothing is held, and why, reserved_kib, running_jobs,
# and kill_pids say why not.
try_admit() {
    local i file pid start cap note_mem note_unit desc rest scope load active
    local unknown=0 free=""
    local noted_units=()
    reserved_kib=0
    running_jobs=()
    for ((i = 0; i < slot_count; i++)); do
        file="$slots/$i"
        exec 7>>"$file"
        if flock -n 7; then
            if [[ -z "$free" ]]; then
                free=$file
                exec 8>&7
            fi
            exec 7>&-
            continue
        fi
        exec 7>&-
        pid="" start="" cap="" note_mem="" note_unit="" desc=""
        { read -r pid start && read -r cap note_mem note_unit && read -r desc; } <"$file" 2>/dev/null || true
        if [[ "$pid" =~ ^[0-9]+$ && "$cap" =~ ^[0-9]+$ && -n "$start" && -n "$note_unit" && -n "$desc" ]] \
            && [[ "$(start_time "$pid" || true)" == "$start" ]]; then
            read_scope "$note_unit"
            rest=$((cap - scope_used_kib))
            ((rest > 0)) || rest=0
            reserved_kib=$((reserved_kib + rest))
            running_jobs+=("$desc (cap $note_mem, using $(gib "$scope_used_kib"))")
            kill_pids+=("$pid")
            noted_units+=("$note_unit")
        else
            unknown=1
            running_jobs+=("unknown, holding $file (find it with 'fuser -v $file')")
        fi
    done
    # Every other heavy scope still running that no slot names, as when its
    # heavy was killed while its command ran: the command runs on in its
    # capped scope, and the part of its cap it has not used stays reserved
    # until the scope ends.
    while read -r scope load active _; do
        [[ "$scope" == heavy-*.scope && "$scope" != "$unit" ]] || continue
        case "$active" in active|activating|deactivating) ;; *) continue ;; esac
        [[ " ${noted_units[*]} " != *" $scope "* ]] || continue
        read_scope "$scope"
        if [[ -z "$scope_cap_kib" ]]; then
            unknown=1
            running_jobs+=("scope $scope with no memory cap, running without a heavy slot (stop it with 'systemctl --user stop $scope')")
            continue
        fi
        rest=$((scope_cap_kib - scope_used_kib))
        ((rest > 0)) || rest=0
        reserved_kib=$((reserved_kib + rest))
        running_jobs+=("scope $scope, running without a heavy slot, as when its heavy was killed (cap $(gib "$scope_cap_kib"), using $(gib "$scope_used_kib"); stop it with 'systemctl --user stop $scope')")
    done < <(systemctl --user list-units --type=scope --plain --no-legend 'heavy-*.scope' 2>/dev/null || true)
    avail_kib=$(meminfo MemAvailable)
    if ((unknown)); then
        why="a heavy slot or scope heavy cannot account for is counted as reserving all the memory"
    elif [[ -z "$free" ]]; then
        why="all $slot_count heavy slots are in use"
    elif ((reserved_kib + cap_kib > avail_kib - margin_kib)); then
        why="$(gib "$avail_kib") available less the 2G margin leaves $(gib $((avail_kib - margin_kib))), and running heavy jobs still reserve $(gib "$reserved_kib") of it"
    else
        printf '%s %s\n%s %s %s\npid %s since %s in %s, scope %s: %s\n' \
            "$$" "$(start_time $$)" "$cap_kib" "$mem" "$unit" \
            "$$" "$(date '+%H:%M:%S')" "$PWD" "$unit" "$command_line" >"$free"
        return 0
    fi
    [[ -z "$free" ]] || exec 8>&-
    return 1
}

# The running heavy jobs, for a report line; nothing when no attempt was made.
running() {
    local joined
    ((attempted)) || return 0
    if ((${#running_jobs[@]} == 0)); then
        printf '; running heavy jobs: none'
        return
    fi
    printf -v joined '%s; ' "${running_jobs[@]}"
    printf '; running heavy jobs: %s' "${joined%; }"
}

# The admission lock is held only for the moment of an admission, never while
# a command runs, so it is always about to be free.
exec 9>>"$lock"
waited=0
next_report=0
while :; do
    attempted=0
    kill_pids=()
    if flock -n 9; then
        attempted=1
        if try_admit; then
            break
        fi
        flock -u 9
    else
        why="the admission lock $lock is held (find its holder with 'fuser -v $lock')"
    fi
    if ((waited >= next_report)); then
        echo "heavy: waiting to start a $mem job (${waited}s so far, gives up after $max_wait): $why$(running)" >&2
        next_report=$((next_report + report_every))
    fi
    if ((waited >= max_wait_s)); then
        how="free memory, rerun with a smaller --mem, or rerun with a longer --max-wait"
        if ((${#kill_pids[@]} > 0)); then
            how="look at a job with 'ps -o pid,etime,args -p PID' and, if it is stuck, stop it ($(printf "'kill %s', " "${kill_pids[@]}" | sed 's/, $//')); or $how"
        fi
        echo "heavy: gave up after waiting $max_wait to start a $mem job: $why$(running); $how" >&2
        exit 75
    fi
    sleep 1
    waited=$((waited + 1))
done
exec 9>&-

case " ${GOFLAGS:-} " in
    *" -p="*|*" -p "*) ;;
    *)
        export GOFLAGS="${GOFLAGS:+$GOFLAGS }-p=2"
        echo "heavy: added -p=2 to GOFLAGS (at most two Go packages built or tested at once)" >&2
        ;;
esac

echo "heavy: capped at $mem" >&2

# 8>&- : the command must not inherit the slot, or a process it leaves running
# would hold the slot after heavy exits. --expand-environment=no: systemd-run
# would otherwise expand $VAR and $$ in the command's own arguments.
status=0
systemd-run --user --scope --quiet --expand-environment=no --unit="$unit" \
    -p MemoryMax="$mem" -p MemorySwapMax=0 -p CPUWeight=20 -- "$@" 8>&- || status=$?

# The scope is not collected on exit, so its result can be read here; a scope
# that failed stays loaded until reset-failed clears it.
result=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    state=$(systemctl --user show "$unit" -p ActiveState -p Result 2>/dev/null || true)
    result=$(sed -n 's/^Result=//p' <<<"$state")
    case "$(sed -n 's/^ActiveState=//p' <<<"$state")" in
        active|activating|deactivating) [[ $status -ne 0 ]] || break ;;
        *) break ;;
    esac
done
# Whatever the command left running in its scope is stopped with it, so no
# leftover process keeps holding memory, or budget, after heavy exits.
if [[ "$(systemctl --user show -P ActiveState "$unit" 2>/dev/null || true)" == active ]]; then
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
    echo "heavy: stopped what the command left running in $unit" >&2
fi
systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
exec 8>&-

if [[ "$result" == oom-kill ]]; then
    printf -v rerun ' %q' "$@"
    echo "heavy: killed at the $mem memory cap; rerun with 'heavy --mem $((${mem%?} * 2))${mem: -1} --${rerun}'" >&2
fi
exit "$status"
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
    "heavy": _HEAVY_SCRIPT,
}

# The deployed scripts that are commands an agent runs, not hooks Claude Code
# runs. deploy-hooks links each one into the bin directory (``~/.local/bin``)
# so the command on PATH is always the copy claudewheel deployed.
PATH_COMMANDS: tuple[str, ...] = ("heavy",)


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
        # A new file renamed over the old one, never a rewrite in place: bash
        # reads a script as it runs it, so a running copy (a heavy waiting on
        # its command, a hook mid-run) must keep the file it started from.
        effects.write_text_atomic(dest, HOOK_SCRIPTS[name])
        effects.chmod(dest, 0o755)
        results.append((name, action))
    return results


def link_path_commands(
    names: list[str], scripts_dir: Path, bin_dir: Path, force_overwrite: bool
) -> list[tuple[Path, Path, str]]:
    """Link each ``PATH_COMMANDS`` member of *names* from *bin_dir* to *scripts_dir*.

    Returns (link, target, action) triples, action being one of "linked" (the
    link was created), "exists" (it already points at the target), "relinked"
    (something else stood there and *force_overwrite* replaced it), or
    "foreign" (something else stands there and was left alone). Names outside
    ``PATH_COMMANDS`` are skipped. A replacement is a new symlink renamed over
    the old entry, so a running copy of the old command keeps its file and no
    reader ever finds the name missing.
    """
    results: list[tuple[Path, Path, str]] = []
    for name in names:
        if name not in PATH_COMMANDS:
            continue
        link = bin_dir / name
        target = scripts_dir / name
        if link.is_symlink() and os.readlink(link) == str(target):
            results.append((link, target, "exists"))
            continue
        if link.is_symlink() or link.exists():
            if not force_overwrite:
                results.append((link, target, "foreign"))
                continue
            staged = bin_dir / f".{name}.claudewheel-{os.getpid()}.tmp"
            effects.symlink(staged, target)
            effects.rename(staged, link)
            results.append((link, target, "relinked"))
            continue
        effects.mkdir(bin_dir, parents=True, exist_ok=True)
        effects.symlink(link, target)
        results.append((link, target, "linked"))
    return results
