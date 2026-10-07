#!/usr/bin/env bash
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
