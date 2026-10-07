#!/usr/bin/env bash
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
