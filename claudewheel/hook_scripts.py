"""Registry of the scripts deploy-hooks deploys: the hook scripts, with blocker/advise generated from the guardrail model, the heavy wrapper, and the claudewheel-tool-scope shell prefix.

Each entry maps a script name to its content as a string constant.
Scripts are deployed to SCRIPTS_DIR (~/.claudewheel/scripts/). The scripts
named in ``PATH_COMMANDS`` are commands rather than hooks, and are also linked
into the user's ``~/.local/bin`` so they are on PATH.
"""

from __future__ import annotations

import os
from pathlib import Path

from claudewheel import guardrail, lifecycle, probe

from . import effects

# The two lifecycle hooks are held in raw strings: their bash contains
# backslash escapes of its own (``tr -d ' \n'``, ``printf '%s\n'``) that a
# regular Python string would eat before bash ever saw them.
#
# The parts the two scripts share (the fail helper, the store and registry
# lookups, mint, append) are repeated in each, on purpose: a deployed hook is
# ONE file that Claude Code runs directly, so neither may depend on a shared
# include sitting next to it.

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
# When the cap does not fit while no heavy command is running, no heavy command
# can end to make room, so heavy exits at once (exit 75) instead of waiting. It
# names the largest cap that fits now, to be used only when a measured peak of
# the command fits under it; otherwise the command needs less memory, made so
# at the source.
#
# Until the command fits, heavy checks again every second and prints, every 30
# seconds, how long it has waited, the memory figures, and every running heavy
# command; it gives up (exit 75) after TIME, naming them, and names the scope
# that ends each one, to be stopped only when it is stuck, never to make room.
# TIME is a whole number of seconds, minutes, or hours (90s, 60m, 2h); the
# default is 60m.
#
# The command runs in its own systemd user scope with MemoryMax=SIZE and no
# swap, so a runaway is killed alone instead of the whole terminal session, and
# with CPUWeight=20 (the default is 100), so interactive work stays responsive.
# heavy prints the cap on every run, and the peak memory use of the command's
# scope when the command returns. When the command is killed for going over
# its cap, heavy says so, with the scope's peak before the kill, and says not
# to rerun it with a bigger cap: a command that outgrows its cap is a defect to
# fix at the source, by stopping that line of work at a clean committed point,
# finding where the memory goes, and cutting it. Only a cap that was a guess
# rather than a measurement may be measured, once, with the largest cap that
# fits now, which heavy names, and then set from the peak that run reports.
# When the command returns, heavy stops its scope, so whatever it left running
# stops too. The scope's description names the claudewheel session scope heavy
# was started from, so an OOM kill of the command is reported to that session.
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

# A peak memory use in bytes, rounded up, so a cap set from it never falls
# short: to a tenth of a gigabyte from a gigabyte on, to a whole megabyte below.
peak_size() {
    awk -v b="$1" 'BEGIN {
        g = 1073741824; m = 1048576
        if (b >= g) { t = int(b * 10 / g); if (t * g < b * 10) t++; printf "%.1fG", t / 10 }
        else { n = int(b / m); if (n * m < b) n++; printf "%dM", n }
    }'
}

# KiB as a cap heavy accepts, rounded down so it still fits: whole gigabytes
# from a gigabyte on, whole megabytes below, nothing below a megabyte.
cap_size() {
    if (($1 >= 1048576)); then
        echo "$(($1 / 1048576))G"
    elif (($1 >= 1024)); then
        echo "$(($1 / 1024))M"
    fi
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

# The claudewheel session scope heavy runs in, read from its own cgroup, goes
# into the job scope's description. The journal keeps a scope's description in
# its "Started" line after the scope is gone, which is how the probe runner
# learns which session's job an OOM kill hit. A heavy started by a Bash command
# runs in the command's tool scope, which names the session scope's pid and
# launch time.
session_scope=""
while IFS= read -r cgroup_line; do
    case "$cgroup_line" in 0::*) session_scope=${cgroup_line##*/} ;; esac
done <"/proc/$$/cgroup"
if [[ "$session_scope" =~ ^claudewheel-tool-([0-9]+)-([0-9]+)-[0-9]+\.scope$ ]]; then
    session_scope="claudewheel-session-${BASH_REMATCH[1]}-${BASH_REMATCH[2]}.scope"
fi
if [[ "$session_scope" =~ ^claudewheel-session-[0-9]+-[0-9]+\.scope$ ]]; then
    description="heavy job of $session_scope: $command_line"
else
    description="heavy job outside any claudewheel session: $command_line"
fi
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

# One admission attempt, made while this heavy holds the admission lock; with
# the argument "survey", only the count of what is reserved, claiming nothing.
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
# and stop_hints say why not; unknown says whether reserved_kib is all of it.
try_admit() {
    local i file pid start cap note_mem note_unit desc rest scope load active
    local free=""
    local noted_units=()
    unknown=0
    hopeless=0
    reserved_kib=0
    running_jobs=()
    stop_hints=()
    for ((i = 0; i < slot_count; i++)); do
        file="$slots/$i"
        exec 7>>"$file"
        if flock -n 7; then
            if [[ -z "$free" && "${1:-}" != survey ]]; then
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
            stop_hints+=("'systemctl --user stop $note_unit' for pid $pid")
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
    [[ "${1:-}" != survey ]] || return 1
    if ((unknown)); then
        why="a heavy slot or scope heavy cannot account for is counted as reserving all the memory"
    elif [[ -z "$free" ]]; then
        why="all $slot_count heavy slots are in use"
    elif ((reserved_kib + cap_kib > avail_kib - margin_kib)); then
        why="$(gib "$avail_kib") available less the 2G margin leaves $(gib $((avail_kib - margin_kib))), and running heavy jobs still reserve $(gib "$reserved_kib") of it"
        ((${#running_jobs[@]} > 0)) || hopeless=1
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
# a command runs, so it is always about to be free. It is open only during an
# attempt, so a waiting heavy, and the sleep it waits in, never show among the
# processes 'fuser -v' lists for it.
waited=0
next_report=0
while :; do
    attempted=0
    stop_hints=()
    exec 9>>"$lock"
    if flock -n 9; then
        attempted=1
        if try_admit; then
            break
        fi
        if ((hopeless)); then
            fits=$(cap_size $((avail_kib - margin_kib - reserved_kib)))
            how="otherwise this command needs more memory than is free, so make it need less at the source (find where the memory goes and cut it), or wait for the memory outside heavy to be given back; do not stop other programs to make room"
            if [[ -n "$fits" ]]; then
                how="the largest cap that fits now is $fits: use it only if a measured peak of this command fits under it, never as a guess; $how"
            else
                how="less than 1M fits now; ${how#otherwise }"
            fi
            echo "heavy: cannot start a $mem job: $why; no heavy job is running, so none can end to make room; $how" >&2
            exit 75
        fi
    else
        why="the admission lock $lock is held (find its holder with 'fuser -v $lock')"
    fi
    exec 9>&-
    if ((waited >= next_report)); then
        echo "heavy: waiting to start a $mem job (${waited}s so far, gives up after $max_wait): $why$(running)" >&2
        next_report=$((next_report + report_every))
    fi
    if ((waited >= max_wait_s)); then
        how="rerun with a longer --max-wait to wait for the running heavy jobs to end; lower --mem only to a measured peak of this command, never as a guess"
        if ((${#stop_hints[@]} > 0)); then
            # Killing a heavy leaves its command running in its scope, holding
            # its cap: stopping the scope is what ends a job.
            how+="; stop a job only when it is stuck, never to make room: 'ps -o pid,etime,args -p PID' shows it, and stopping its scope ends it and all it started ($(printf '%s, ' "${stop_hints[@]}" | sed 's/, $//'))"
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

# Runs the command inside its scope and, once it returns, reports the peak
# memory use of the scope, read there because systemd drops a scope that ended
# cleanly, figures and all, as soon as its last process exits.
run_in_scope() {
    local cap=$1 status=0 cg peak
    shift
    "$@" || status=$?
    cg=$(sed -n 's/^0:://p' /proc/self/cgroup)
    peak=$(cat "/sys/fs/cgroup$cg/memory.peak" 2>/dev/null || true)
    if [[ "$peak" =~ ^[0-9]+$ ]]; then
        echo "heavy: the command's scope peaked at $(peak_size "$peak") of its $cap cap (page cache included)" >&2
    fi
    return "$status"
}

# 8>&- : the command must not inherit the slot, or a process it leaves running
# would hold the slot after heavy exits. --expand-environment=no: systemd-run
# would otherwise expand $VAR and $$ in the command's own arguments.
status=0
systemd-run --user --scope --quiet --expand-environment=no --unit="$unit" \
    --description="$description" \
    -p MemoryMax="$mem" -p MemorySwapMax=0 -p CPUWeight=20 \
    -- bash -c "$(declare -f peak_size run_in_scope)"'; run_in_scope "$@"' heavy "$mem" "$@" 8>&- \
    || status=$?

# The scope is not collected on exit, so its result can be read here; a scope
# that failed stays loaded, with the peak memory use systemd recorded for it,
# until reset-failed clears it.
result=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    state=$(systemctl --user show "$unit" -p ActiveState -p Result -p MemoryPeak 2>/dev/null || true)
    result=$(sed -n 's/^Result=//p' <<<"$state")
    case "$(sed -n 's/^ActiveState=//p' <<<"$state")" in
        active|activating|deactivating) [[ $status -ne 0 ]] || break ;;
        *) break ;;
    esac
done
# Whatever the command left running in its scope is stopped with it, so no
# leftover process keeps holding memory, or budget, after heavy exits. The
# cgroup says whether any process is left; the unit's state can still read
# active for a moment after the last one has exited.
cg=$(systemctl --user show -P ControlGroup "$unit" 2>/dev/null || true)
if [[ "$cg" == /* ]] && grep -qx 'populated 1' "/sys/fs/cgroup$cg/cgroup.events" 2>/dev/null; then
    systemctl --user stop "$unit" >/dev/null 2>&1 || true
    echo "heavy: stopped what the command left running in $unit" >&2
fi
systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
exec 8>&-

# A command killed at its cap is a defect to fix at the source, never a reason
# for a bigger cap. The one larger run it may get is a measurement, when its
# cap was a guess: at the largest cap that fits now, counted the way an
# admission counts it.
if [[ "$result" == oom-kill ]]; then
    killed="heavy: killed at the $mem memory cap"
    peak=$(sed -n 's/^MemoryPeak=//p' <<<"$state")
    if [[ "$peak" =~ ^[0-9]+$ ]]; then
        killed+=" (its scope peaked at $(peak_size "$peak") before the kill, page cache included)"
    fi
    fix="do not rerun it with a bigger --mem: @OOM_KILL_FIX@"
    guess="if $mem was a guess rather than a measurement"
    exec 9>>"$lock"
    if ! flock -w 10 9; then
        measure="only $guess: measure once with the largest cap that fits now, which heavy cannot tell while the admission lock $lock is held (find its holder with 'fuser -v $lock')"
    else
        try_admit survey || true
        fits=$(cap_size $((avail_kib - margin_kib - reserved_kib)))
        if ((unknown)); then
            measure="only $guess: measure once with the largest cap that fits now, which heavy cannot tell while a heavy slot or scope it cannot account for holds memory"
        elif [[ -n "$fits" ]] && (($(to_kib "$fits") > cap_kib)); then
            measure="only $guess: measure once with --mem $fits, the largest cap that fits now, and set --mem from the peak heavy prints when the command returns"
        else
            largest="${fits:+$fits is the largest}"
            measure="even $guess, no cap larger than $mem fits now to measure with (${largest:-less than 1M fits}), so cut the memory at the source first"
        fi
    fi
    exec 9>&-
    echo "$killed; $fix; $measure" >&2
fi
exit "$status"
"""

# The CLAUDE_CODE_SHELL_PREFIX claudewheel.launch.do_launch sets for every
# session it launches.
TOOL_SCOPE_SCRIPT = "claudewheel-tool-scope"

_TOOL_SCOPE_SCRIPT = r"""#!/usr/bin/env bash
# claudewheel-tool-scope: the CLAUDE_CODE_SHELL_PREFIX of every Claude Code
# session claudewheel launches.
#
# Claude Code runs every shell command it spawns through this prefix, as
# `claudewheel-tool-scope '<command line>'`: Bash tool calls, hooks, the status
# line, and stdio MCP servers. The command line arrives as one argument and is
# re-evaluated with bash.
#
# A Bash tool call runs in a systemd user scope of its own,
# claudewheel-tool-<pid>-<time>-<n>.scope (the session scope's Claude Code pid
# and launch time, then this wrapper's pid), inside the session's tools slice,
# CLAUDEWHEEL_TOOL_SLICE. The slice caps the session's Bash commands and
# everything they start, together, at CLAUDEWHEEL_TOOL_MEMORY_MAX of memory and
# CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX of swap. Claude Code itself runs outside the
# slice, uncapped, so a runaway command is killed while the session goes on.
# The session's first Bash call creates the slice with its cap; when the slice
# cannot carry the cap, the command is refused rather than run uncapped.
#
# When the kernel's OOM killer kills a process of the command, the wrapper says
# so on stderr once the command returns, naming the cap; the exit status is the
# command's own. Nothing is retried.
#
# Everything else Claude Code runs through the prefix (hooks, the status line,
# MCP servers) runs directly, outside the cap: a guardrail hook killed at the
# cap would let its tool call through unchecked.

set -u

# The size a K, M, G, or T suffix (or a bare 0) names, in bytes.
bytes() {
    local n=${1%[KMGT]}
    [[ "$1" == 0 ]] && { echo 0; return 0; }
    [[ "$n" =~ ^[1-9][0-9]*$ && "$1" != "$n" ]] || return 1
    case "${1: -1}" in
        K) echo $((n << 10)) ;;
        M) echo $((n << 20)) ;;
        G) echo $((n << 30)) ;;
        T) echo $((n << 40)) ;;
    esac
}

# A byte count as heavy prints a peak: tenths of a gigabyte from a gigabyte on,
# whole megabytes below, both rounded up.
peak_size() {
    awk -v b="$1" 'BEGIN {
        g = 1073741824; m = 1048576
        if (b >= g) { t = int(b * 10 / g); if (t * g < b * 10) t++; printf "%.1fG", t / 10 }
        else { n = int(b / m); if (n * m < b) n++; printf "%dM", n }
    }'
}

# The value of KEY in a memory.events file, 0 when it cannot be read.
event() {
    local key value
    if [[ -r "$2" ]]; then
        while read -r key value; do
            if [[ "$key" == "$1" ]]; then
                echo "$value"
                return 0
            fi
        done <"$2"
    fi
    echo 0
}

# Inside the scope: run the command, then report an OOM kill of any of its
# processes. The scope's cgroup lives until this process exits, so its
# counters are read here; the tools slice's own count of the times it hit its
# limit tells a kill at the cap from one when the machine ran out of memory.
if [[ $# -eq 2 && "$1" == --claudewheel-in-tool-scope ]]; then
    cg=""
    while IFS= read -r line; do
        case "$line" in 0::*) cg=${line#0::} ;; esac
    done <"/proc/$$/cgroup"
    tools=${cg%/*}
    at_limit_before=$(event oom "/sys/fs/cgroup$tools/memory.events")
    # bash reports a child killed by a signal with a "line N: PID Killed" line
    # on its own stderr, which is /dev/null while the command runs; the
    # command keeps the real stderr through fd 3.
    status=0
    { bash -c "$2" 2>&3 3>&-; } 3>&2 2>/dev/null || status=$?
    killed=$(event oom_kill "/sys/fs/cgroup$cg/memory.events")
    if [[ "$killed" =~ ^[1-9][0-9]*$ ]]; then
        at_limit_after=$(event oom "/sys/fs/cgroup$tools/memory.events")
        cap="the ${CLAUDEWHEEL_TOOL_MEMORY_MAX:-} memory cap they share (${tools##*/}, MemoryMax=${CLAUDEWHEEL_TOOL_MEMORY_MAX:-}, MemorySwapMax=${CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX:-})"
        if ((at_limit_after > at_limit_before)); then
            when="when this session's Bash commands reached $cap"
        else
            when="when this machine ran out of memory, with this session's Bash commands under $cap"
        fi
        where="The command ran in ${cg##*/}"
        peak=$(cat "/sys/fs/cgroup$cg/memory.peak" 2>/dev/null || true)
        if [[ "$peak" =~ ^[0-9]+$ ]]; then
            where+=", which peaked at $(peak_size "$peak") (page cache included)"
        fi
        echo "claudewheel: this command was OOM-killed: the kernel's OOM killer killed $killed of its processes $when. $where. Claude Code itself is not capped and keeps running. Fix the memory at its source, do not rerun it: @OOM_KILL_FIX@." >&2
    fi
    exit "$status"
fi

command_line=${1-}
slice=${CLAUDEWHEEL_TOOL_SLICE:-}

# Claude Code assembles each Bash tool call as one chain that ends by writing
# the shell's working directory to a file (`&& pwd -P >| <file>`); nothing
# else it runs through the prefix does.
if [[ $# -ne 1 || "$command_line" != *" && pwd -P >| "* ]]; then
    exec bash -c "$command_line"
fi

refuse() {
    echo "claudewheel: refused to run this command: $1. claudewheel runs a session's Bash commands only in the session's tools slice, under the memory cap they share" >&2
    exit 1
}

[[ -n "$slice" ]] ||
    refuse "CLAUDEWHEEL_TOOL_SLICE is empty, and claudewheel launches every session with it set beside this prefix"

session_scope=${CLAUDEWHEEL_SESSION_SCOPE:-}
max=${CLAUDEWHEEL_TOOL_MEMORY_MAX:-}
swap=${CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX:-}
[[ "$session_scope" =~ ^claudewheel-session-([0-9]+)-([0-9]+)\.scope$ ]] ||
    refuse "CLAUDEWHEEL_SESSION_SCOPE is not a claudewheel session scope: '$session_scope'"
unit="claudewheel-tool-${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-$$.scope"
max_bytes=$(bytes "$max") || refuse "CLAUDEWHEEL_TOOL_MEMORY_MAX is not a size: '$max'"
swap_bytes=$(bytes "$swap") || refuse "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX is not a size: '$swap'"

# Reads the slice's state and caps as systemd reports them.
read_slice() {
    local out
    out=$(systemctl --user show -p ActiveState -p MemoryMax -p MemorySwapMax "$slice" 2>/dev/null || true)
    state=$(sed -n 's/^ActiveState=//p' <<<"$out")
    slice_max=$(sed -n 's/^MemoryMax=//p' <<<"$out")
    slice_swap=$(sed -n 's/^MemorySwapMax=//p' <<<"$out")
}
capped() {
    [[ "$state" == active && "$slice_max" == "$max_bytes" && "$slice_swap" == "$swap_bytes" ]]
}

read_slice
if ! capped; then
    created=""
    if [[ "$state" != active ]]; then
        # A transient slice holds its caps from the moment it exists. Two first
        # commands may race to create it: the second creation fails, and the
        # slice the first made is read again below.
        created=$(busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
            org.freedesktop.systemd1.Manager StartTransientUnit 'ssa(sv)a(sa(sv))' \
            "$slice" fail 3 MemoryMax t "$max_bytes" MemorySwapMax t "$swap_bytes" \
            Description s "claudewheel Bash tools of $session_scope, capped together" \
            0 2>&1 >/dev/null) || created="could not create it: ${created:-busctl failed}"
        read_slice
    fi
    if ! capped; then
        if [[ -n "$created" ]]; then
            refuse "$slice, capped at $max of memory and $swap of swap, $created"
        fi
        refuse "$slice has no $max memory cap (ActiveState=${state:-unknown}, MemoryMax=${slice_max:-unknown}, MemorySwapMax=${slice_swap:-unknown})"
    fi
fi

# --expand-environment=no: systemd-run would otherwise expand $VAR and $$ in
# the command line. OOMPolicy=continue: a kill of one process leaves the rest
# of the command, and this wrapper, to finish and report it.
exec systemd-run --user --scope --quiet --collect --expand-environment=no \
    --slice="$slice" --unit="$unit" --description="Bash tool command of $session_scope" \
    -p OOMPolicy=continue \
    -- "${BASH_SOURCE[0]}" --claudewheel-in-tool-scope "$command_line"
"""

# The waiter: hands the reports queued for a session's main conversation
# over by exiting 2 from an asyncRewake hook. The one hook allowed to exit 2
# (EXIT_2_HOOKS).
_WAIT_FOR_PROBE_REPORTS_SCRIPT = r"""#!/usr/bin/env bash
# hook-wait-for-probe-reports: the one waiter of a Claude Code session. Wired
# on SessionStart and Stop as an asyncRewake hook: it runs in the background,
# waits for the probe reports queued for the session's main conversation, and
# hands them over by printing them and exiting 2, which wakes the session with
# them (rewakeMessage and rewakeSummary label the reminder).
#
# The exit 2 is the one written exception to the rule that a claudewheel hook
# never blocks a session (every other hook exits 1 on failure, never 2): on an
# asyncRewake hook, exit 2 does not block anything -- it is how a background
# hook wakes an idle session, and nothing else can. This hook exits 2 only
# after it has handed reports over; every failure exits 1, and every other
# outcome exits 0. hook_scripts.EXIT_2_HOOKS names it, and a test holds every
# other hook to exit 1 or 0.
#
# It exits 0 at once when the session is not interactive: under `claude -p`
# an asyncRewake hook holds the run open for its whole timeout. It exits 0
# when its client process is gone (its pid, or the pid's start time, changed),
# so a killed client leaves no waiter behind. One waiter per session, held by
# a lock: a waiter armed while another serves the same client exits 0 at once.
#
# The reports are files the probe runner queues under
# shared/probes/reports/pending/<session>/, each <report-id>.main.json. The
# waiter moves each to handed/ before printing it; the runner confirms it in
# the session's transcript, or queues it again. It sleeps on a FIFO the runner
# writes a byte to, and looks again every few seconds regardless, without
# starting a process while it waits.

set -uo pipefail

[[ "${CLAUDE_CODE_SESSION_ATTENDED:-}" == 1 && "${CLAUDE_CODE_ENTRYPOINT:-}" == cli ]] || exit 0

fail() {
    printf 'hook-wait-for-probe-reports: %s\n' "$1" >&2
    exit 1
}

command -v jq >/dev/null 2>&1 || fail 'jq not found'
command -v flock >/dev/null 2>&1 || fail 'flock not found'

input=$(cat 2>/dev/null || true)
session=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)
[[ "$session" =~ @SESSION_UUID_RE@ ]] ||
    fail "payload carries no Claude Code session uuid: '$session'"

client="${CLAUDE_PID:-}"
[[ "$client" =~ ^[1-9][0-9]*$ ]] || fail "CLAUDE_PID is not a process id: '$client'"

# The kernel start time of a process (field 22 of /proc/PID/stat) in REPLY,
# read without starting a process; fails when the process is gone.
proc_start() {
    local stat rest
    local -a fields
    REPLY=""
    { read -r stat <"/proc/$1/stat"; } 2>/dev/null || return 1
    rest=${stat##*) }
    read -r -a fields <<<"$rest"
    REPLY=${fields[19]:-}
    [[ -n "$REPLY" ]]
}

proc_start "$client" || fail "client process $client is gone"
client_start=$REPLY

root="${CLAUDEWHEEL_CONFIG_DIR:-${HOME:+${HOME}/.claudewheel}}"
[[ -n "$root" ]] || fail 'cannot resolve the store: set CLAUDEWHEEL_CONFIG_DIR or HOME'
probes="$root/shared/probes"
waiters="$probes/waiters"
pending="$probes/reports/pending/$session"
handed="$probes/reports/handed/$session"
mkdir -p "$waiters" || fail "cannot create $waiters"

exec 9>>"$waiters/$session.lock" || fail "cannot open $waiters/$session.lock"
if ! flock -n 9; then
    holder=""
    { read -r holder <"$waiters/$session.lock"; } 2>/dev/null || true
    # A waiter already serves this client: it is the one.
    [[ "$holder" == "$client $client_start" ]] && exit 0
    # A waiter of an earlier client of this session exits on its next look.
    flock -w 30 9 || exit 0
fi
printf '%s %s\n' "$client" "$client_start" >"$waiters/$session.lock"

fifo="$waiters/$session.fifo"
[[ -p "$fifo" ]] || mkfifo "$fifo" 2>/dev/null || [[ -p "$fifo" ]] || fail "cannot create $fifo"
exec 3<>"$fifo" || fail "cannot open $fifo"

has_pending() {
    local f
    for f in "$pending"/*.main.json; do
        [[ -e "$f" ]] && return 0
    done
    return 1
}

while :; do
    proc_start "$client" && [[ "$REPLY" == "$client_start" ]] || exit 0
    if has_pending; then
        # Reports arriving together go in one delivery.
        while read -r -t 0.3 -n 1 -u 3 _; do :; done
        mkdir -p "$handed" || fail "cannot create $handed"
        files=()
        for f in "$pending"/*.main.json; do
            [[ -e "$f" ]] || continue
            name=${f##*/}
            mv "$f" "$handed/$name" 2>/dev/null || continue
            touch "$handed/$name"
            files+=("$handed/$name")
        done
        if ((${#files[@]} > 0)); then
            text=$(jq -rs '[.[] | (if .agent != null then "For subagent \(.agent)" + (if .task != null then " (task: \(.task))" else "" end) + ", which has finished: " else "" end) + .text] | join("\n\n")' "${files[@]}") ||
                fail "cannot read the reports ${files[*]}"
            printf '%s\n' "$text" >&2
            exit 2
        fi
    fi
    read -r -t 5 -n 1 -u 3 _ || true
done
"""

# The tool-event side of report delivery: subagents, bindings, 137 labels.
_DELIVER_PROBE_REPORTS_SCRIPT = r"""#!/usr/bin/env bash
# hook-deliver-probe-reports: the probe reports' way into tool calls, and the
# record the reports need of a session's tool calls and subagents. Wired on
# PreToolUse for Bash, and on PostToolUse, PostToolUseFailure, and
# SubagentStop for every tool.
#
# - PreToolUse (Bash): records that the call started.
# - PostToolUse / PostToolUseFailure (Bash): records that it ended. After a
#   call that ran `claudewheel probe create` or `probe subscribe`, binds the
#   subscription the command printed to the conversation that made the call:
#   the payload's agent_id, or the main conversation when it has none.
# - PostToolUseFailure (Bash, exit 137): when the probe runner recorded an OOM
#   kill in this session during the call, labels the failure for the calling
#   conversation, and says so when other Bash calls were running at the kill.
# - PostToolUse (Agent): records the subagent the call launched and its task.
# - PostToolUse / PostToolUseFailure of a subagent's call: hands the
#   subagent the reports queued for it, as additionalContext.
# - SubagentStop: records that the subagent finished and sends its queued
#   reports to the main conversation, which gets them labeled with the
#   subagent and its task.
#
# The hook never blocks a session. Every failure prints one line to stderr and
# exits 1 -- never 2, which Claude Code reads as "block this event".

set -uo pipefail

fail() {
    printf 'hook-deliver-probe-reports: %s\n' "$1" >&2
    exit 1
}

command -v jq >/dev/null 2>&1 || fail 'jq not found'

input=$(cat 2>/dev/null || true)
# One jq for the five fields, joined by the unit separator: a tab is IFS
# whitespace, so an empty field between two tabs would vanish.
fields=$(printf '%s' "$input" | jq -r '[.hook_event_name // "", .session_id // "", .agent_id // "", .tool_name // "", .tool_use_id // ""] | map(tostring) | join("\u001f")' 2>/dev/null) ||
    fail 'payload is not JSON'
IFS=$'\x1f' read -r event session agent tool call <<<"$fields"
[[ "$session" =~ @SESSION_UUID_RE@ ]] ||
    fail "payload carries no Claude Code session uuid: '$session'"
[[ -z "$agent" || "$agent" =~ ^[0-9a-zA-Z_-]+$ ]] || fail "unexpected agent_id: '$agent'"

root="${CLAUDEWHEEL_CONFIG_DIR:-${HOME:+${HOME}/.claudewheel}}"
[[ -n "$root" ]] || fail 'cannot resolve the store: set CLAUDEWHEEL_CONFIG_DIR or HOME'
probes="$root/shared/probes"
record="$probes/sessions/$session.jsonl"
pending="$probes/reports/pending/$session"
handed="$probes/reports/handed/$session"

now_ms() {
    local us=${EPOCHREALTIME/[.,]/}
    REPLY=$((10#$us / 1000))
}

agent_json() {
    if [[ -n "$agent" ]]; then REPLY="\"$agent\""; else REPLY=null; fi
}

append() {
    mkdir -p "${record%/*}" 2>/dev/null || fail "cannot create ${record%/*}"
    printf '%s\n' "$1" >>"$record" || fail "cannot append to $record"
}

record_call() {
    # $1: call-started or call-ended.
    [[ "$call" =~ ^[0-9A-Za-z_-]+$ ]] || return 0
    now_ms
    local at=$REPLY
    agent_json
    if [[ "$1" == call-started ]]; then
        local timeout
        timeout=$(printf '%s' "$input" | jq -r '(.tool_input.timeout // 120000) | floor' 2>/dev/null)
        [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=120000
        append "{\"format_version\":1,\"at_ms\":$at,\"kind\":\"call-started\",\"call\":\"$call\",\"agent\":$REPLY,\"timeout_ms\":$timeout}"
    else
        append "{\"format_version\":1,\"at_ms\":$at,\"kind\":\"call-ended\",\"call\":\"$call\",\"agent\":$REPLY}"
    fi
}

wake_waiter() {
    local fifo="$probes/waiters/$session.fifo"
    [[ -p "$fifo" ]] || return 0
    # Opened for reading and writing, so it never blocks for a reader.
    { exec 4<>"$fifo" && printf x >&4 && exec 4>&-; } 2>/dev/null || true
}

mint() {
    local rand
    rand=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
    [[ ${#rand} -eq 32 ]] || fail 'cannot read 16 random bytes from /dev/urandom'
    event_id=$(printf '%016x%s' "$(date +%s%N)" "$rand")
    event_at=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
}

# Bind a subscription this session created in this call to this conversation.
bind() {
    local sub=$1 probe_id=$2 file="$probes/probes/$2.jsonl" state target f moved task
    [[ -f "$file" ]] || return 0
    state=$(jq -rs --arg sub "$sub" --arg s "$session" '
        (map(select(.kind == "subscribed" and .subscription == $sub and .session == $s)) | length) as $mine
        | (map(select(.kind == "bound" and .subscription == $sub)) | length) as $bound
        | if $mine == 1 and $bound == 0 then "unbound" else "no" end' "$file" 2>/dev/null)
    [[ "$state" == unbound ]] || return 0
    mint
    agent_json
    printf '%s\n' "{\"format_version\":1,\"id\":\"$event_id\",\"at\":\"$event_at\",\"probe\":\"$probe_id\",\"kind\":\"bound\",\"subscription\":\"$sub\",\"agent\":$REPLY}" >>"$file" ||
        fail "cannot append to $file"
    # Reports queued before the binding go to the conversation now known.
    if [[ -n "$agent" ]]; then target="agent-$agent"; else target=main; fi
    task=null
    if [[ -n "$agent" && -f "$record" ]]; then
        task=$(jq -cs --arg a "$agent" 'map(select(.kind == "agent-launched" and .agent == $a)) | (last | .task) // null' "$record" 2>/dev/null)
        [[ -n "$task" ]] || task=null
    fi
    agent_json
    for f in "$pending"/*".unbound-$sub.json"; do
        [[ -e "$f" ]] || continue
        # The content gains the conversation in place, then the name does.
        jq --argjson a "$REPLY" --argjson t "$task" '.agent = $a | .task = $t' "$f" >"$f.new" &&
            mv "$f.new" "$f" &&
            mv "$f" "${f%.unbound-$sub.json}.$target.json"
    done
    wake_waiter
}

# The reports queued for this subagent, moved to handed/, as text in REPLY.
take_agent_reports() {
    local f name
    local -a files=()
    REPLY=""
    [[ -n "$agent" ]] || return 0
    for f in "$pending"/*".agent-$agent.json"; do
        [[ -e "$f" ]] || continue
        mkdir -p "$handed" || fail "cannot create $handed"
        name=${f##*/}
        mv "$f" "$handed/$name" 2>/dev/null || continue
        touch "$handed/$name"
        files+=("$handed/$name")
    done
    ((${#files[@]} > 0)) || return 0
    REPLY=$(jq -rs '[.[] | .text] | join("\n\n")' "${files[@]}") || fail "cannot read ${files[*]}"
}

overlap_sentence='@OVERLAP_SENTENCE@'

# The label of a Bash call that ended with status 137 while the runner recorded
# an OOM kill in this session, in REPLY.
label_oom_kill() {
    local duration now start_ms end_ms kills="$probes/kills.jsonl" labels calls tries
    REPLY=""
    duration=$(printf '%s' "$input" | jq -r '(.duration_ms // 0) | floor' 2>/dev/null)
    [[ "$duration" =~ ^[0-9]+$ ]] || duration=0
    now_ms
    now=$REPLY
    REPLY=""
    start_ms=$((now - duration - 1000))
    # The runner records a kill from the journal a moment after it happens,
    # so a kill during the call may be recorded after the call returned.
    end_ms=$((now + @HOOK_WAIT_SECONDS@ * 1000))
    labels=""
    for ((tries = 0; tries < @HOOK_WAIT_SECONDS@ * 5; tries++)); do
        if [[ -f "$kills" ]]; then
            labels=$(jq -rs --arg s "$session" --argjson a "$((start_ms * 1000))" --argjson b "$((end_ms * 1000))" '
                map(select(.session == $s and .killed_at_us >= $a and .killed_at_us <= $b)) | map(.label) | join(" ")' "$kills" 2>/dev/null)
        fi
        [[ -z "$labels" ]] || break
        sleep 0.2
    done
    [[ -n "$labels" ]] || return 0
    calls=""
    if [[ -f "$record" && -f "$kills" ]]; then
        calls=$(jq -rn --arg s "$session" --arg me "$call" --argjson a "$((start_ms * 1000))" --argjson b "$((end_ms * 1000))" \
            --slurpfile k "$kills" --slurpfile r "$record" '
            [$k[] | select(.session == $s and .killed_at_us >= $a and .killed_at_us <= $b) | .killed_at_us / 1000] as $times
            | ($r | map(select(.kind == "call-ended")) | map({key: .call, value: .at_ms}) | from_entries) as $ends
            | [$r[] | select(.kind == "call-started" and .call != $me)
                | . as $c
                | select(any($times[]; . >= $c.at_ms and . <= ($ends[$c.call] // ($c.at_ms + $c.timeout_ms))))
                | "\(.call) (" + (if .agent == null then "main conversation" else "subagent \(.agent)" end) + ")"]
            | unique | join(", ")' 2>/dev/null)
    fi
    REPLY=$labels
    if [[ -n "$calls" ]]; then
        REPLY+=${overlap_sentence//\{calls\}/$calls}
    fi
}

emit() {
    # $1: the additionalContext text; nothing is printed when it is empty.
    [[ -n "$1" ]] || return 0
    jq -cn --arg e "$event" --arg c "$1" '{hookSpecificOutput: {hookEventName: $e, additionalContext: $c}}'
}

bind_re='@BIND_RE@'

case "$event" in
    PreToolUse)
        [[ "$tool" == Bash ]] && record_call call-started
        exit 0
        ;;
    PostToolUse | PostToolUseFailure)
        context=""
        if [[ "$tool" == Bash ]]; then
            record_call call-ended
            if [[ "$event" == PostToolUse ]]; then
                stdout=$(printf '%s' "$input" | jq -r '.tool_response.stdout // ""' 2>/dev/null)
                while [[ "$stdout" =~ $bind_re ]]; do
                    bind "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
                    stdout=${stdout#*"${BASH_REMATCH[0]}"}
                done
            else
                error=$(printf '%s' "$input" | jq -r '.error // ""' 2>/dev/null)
                if [[ "$error" == "Exit code 137"* ]]; then
                    label_oom_kill
                    context=$REPLY
                fi
            fi
        elif [[ "$tool" == Agent && "$event" == PostToolUse ]]; then
            launched_agent=$(printf '%s' "$input" | jq -r '.tool_response.agentId // ""' 2>/dev/null)
            launched_task=$(printf '%s' "$input" | jq -r '.tool_input.description // ""' 2>/dev/null)
            if [[ "$launched_agent" =~ ^[0-9a-zA-Z_-]+$ ]]; then
                now_ms
                line=$(jq -cn --argjson at "$REPLY" --arg a "$launched_agent" --arg t "$launched_task" \
                    '{format_version: 1, at_ms: $at, kind: "agent-launched", agent: $a, task: (if $t == "" then null else $t end)}')
                append "$line"
            fi
        fi
        take_agent_reports
        if [[ -n "$REPLY" ]]; then
            context+=${context:+$'\n\n'}$REPLY
        fi
        emit "$context"
        exit 0
        ;;
    SubagentStop)
        [[ -n "$agent" ]] || exit 0
        now_ms
        append "{\"format_version\":1,\"at_ms\":$REPLY,\"kind\":\"agent-finished\",\"agent\":\"$agent\"}"
        moved=0
        for f in "$pending"/*".agent-$agent.json"; do
            [[ -e "$f" ]] || continue
            mv "$f" "${f%.agent-$agent.json}.main.json" && moved=1
        done
        ((moved)) && wake_waiter
        exit 0
        ;;
esac
exit 0
"""

# The hooks that may exit 2, each with why: the written exception to the
# rule that a claudewheel hook never blocks a session (it exits 1 on
# failure, never 2). tests/test_probe_hooks.py holds every other hook to it.
EXIT_2_HOOKS: dict[str, str] = {
    "hook-wait-for-probe-reports": (
        "an asyncRewake hook exiting 2 blocks nothing: it is how a background "
        "hook wakes an idle session with its output, and nothing else can"
    ),
}


def _with_session_uuid_re(script: str) -> str:
    """*script* with its session-uuid test spelled as lifecycle's one pattern."""
    return script.replace("@SESSION_UUID_RE@", lifecycle.SESSION_UUID_RE.pattern)


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
    "hook-session-start": _with_session_uuid_re(_SESSION_START_SCRIPT),
    "hook-session-end": _with_session_uuid_re(_SESSION_END_SCRIPT),
    # Generated from the canonical guardrail model. See claudewheel/guardrail.py.
    "hook-block-unsafe-commands": guardrail.generate_blocker_script(),
    "hook-advise-commands": guardrail.generate_advise_script(),
    # The kill message's fix is the one text every OOM report shares.
    "hook-wait-for-probe-reports": _with_session_uuid_re(
        _WAIT_FOR_PROBE_REPORTS_SCRIPT
    ),
    "hook-deliver-probe-reports": _with_session_uuid_re(_DELIVER_PROBE_REPORTS_SCRIPT)
    .replace("@OVERLAP_SENTENCE@", probe.OVERLAP_SENTENCE.replace("'", "'\\''"))
    .replace("@HOOK_WAIT_SECONDS@", str(probe.HOOK_WAIT_SECONDS))
    .replace("@BIND_RE@", probe.BIND_LINE_RE.pattern),
    "heavy": _HEAVY_SCRIPT.replace(
        "@OOM_KILL_FIX@", guardrail.bash_dquote_body(probe.OOM_KILL_FIX)
    ),
    # Not a hook: the shell prefix every launched session runs its commands
    # through. Its kill message's fix is the shared OOM text too.
    TOOL_SCOPE_SCRIPT: _TOOL_SCOPE_SCRIPT.replace(
        "@OOM_KILL_FIX@", guardrail.bash_dquote_body(probe.OOM_KILL_FIX)
    ),
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


# ---------------------------------------------------------------------------
# The probe runner's user service
# ---------------------------------------------------------------------------


def service_unit(python: str, root: Path) -> str:
    """The unit file of claudewheel-probe-runner.service.

    It runs the probe runner under *python*, the interpreter claudewheel was
    deployed from, over the workspace at *root*, restarts it when it fails,
    and starts it with the user's systemd manager, so it runs again after a
    reboot. ``systemctl --user stop claudewheel-probe-runner.service`` stops it
    gracefully: the runner saves its journal cursor and exits on SIGTERM.
    """
    return (
        "# Deployed by claudewheel ('claudewheel deploy-hooks "
        f"{probe.SERVICE_NAME}'); edits are overwritten.\n"
        "[Unit]\n"
        "Description=claudewheel probe runner: reports OOM kills to the Claude "
        "Code sessions they concern\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="CLAUDEWHEEL_CONFIG_DIR={root}"\n'
        f'ExecStart="{python}" -m claudewheel.probe_runner\n'
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def deploy_service(
    unit_dir: Path, python: str, root: Path, force_overwrite: bool
) -> tuple[Path, str]:
    """Write the probe runner's unit into *unit_dir*, enable it, and (re)start it.

    Returns the unit path and "created", "overwritten", or "exists" (left alone
    because it was there and *force_overwrite* was False). A written unit is
    loaded and the service restarted, so it runs the code deployed now; one
    left alone is enabled and started when it is not running.
    """
    path = unit_dir / probe.SERVICE_NAME
    systemctl = ["systemctl", "--user"]
    if path.exists() and not force_overwrite:
        action = "exists"
        effects.run([*systemctl, "enable", "--now", probe.SERVICE_NAME], check=True)
        return path, action
    action = "overwritten" if path.exists() else "created"
    effects.mkdir(unit_dir, parents=True, exist_ok=True)
    effects.write_text_atomic(path, service_unit(python, root))
    effects.run([*systemctl, "daemon-reload"], check=True)
    effects.run([*systemctl, "enable", probe.SERVICE_NAME], check=True)
    effects.run([*systemctl, "restart", probe.SERVICE_NAME], check=True)
    return path, action
