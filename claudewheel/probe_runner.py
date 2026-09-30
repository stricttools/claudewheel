"""The probe runner: the one process that hosts every probe (claudewheel-probe-runner.service).

It follows the user journal for systemd's "The kernel OOM killer killed some
processes in this unit" entries and, for each one:

* attributes the unit to a Claude Code session -- a claudewheel session scope
  through the lifecycle store, a ``heavy`` scope through the session scope
  heavy named in the scope's description, which the journal keeps after the
  scope is gone -- or records why it cannot;
* queues a report to the session whose command it was, with no probe and no
  deadline, and one to every subscription of every live probe that watches
  that session or all sessions;
* records the kill in ``kills.jsonl`` with the reports it produced (none: an
  unrouted kill, kept and listed rather than dropped), then saves the journal
  cursor, so a restarted runner resumes after the last entry with no repeat
  and no gap.

Between entries it keeps the store moving: it ends probes whose deadline
passed, whose file appeared, or whose watched session ended; it confirms each
report handed to a session by finding its id in the session's transcript, and
puts one back in the queue when the session went away without it or moved on
without it; and it expires the undelivered reports of an ended probe once
their session has ended too. A report no probe produced never expires.

It is started by systemd (``python -m claudewheel.probe_runner``) and stops
gracefully on SIGTERM: ``systemctl --user stop claudewheel-probe-runner.service``.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import effects, lifecycle, probe
from .probe import ProbeState, ProbeStore, Report
from .workspace import Workspace

SERVICE_NAME = probe.SERVICE_NAME

# systemd's catalog ids: a unit's process was OOM-killed; a job finished (the
# "Started <unit> - <description>" line).
OOM_KILL_MESSAGE_ID = "fe6faa94e7774663a0da52717891d8ef"
JOB_DONE_MESSAGE_ID = "39f53479d3a045ac8e11786248231fbf"

# How often the store is kept moving while the journal is quiet, in seconds.
TICK_SECONDS = 2.0

# A handed report whose session's transcript has moved on this long after the
# hand-off without it was lost on the way, and is queued again.
LOST_AFTER_SECONDS = 60.0


def _digest_id(*parts: str) -> str:
    """A 16-hex-digit id derived from *parts*, so reprocessing an entry repeats its ids."""
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    scope: str  # "session" | "heavy" | "other"
    session: str | None
    command: str | None
    unattributed: str | None


def heavy_description(unit: str) -> str | None:
    """The description the journal kept for a heavy scope, from its "Started" line."""
    proc = effects.run(
        [
            "journalctl",
            "--user",
            "-o",
            "json",
            "--no-pager",
            f"MESSAGE_ID={JOB_DONE_MESSAGE_ID}",
            f"USER_UNIT={unit}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        read=True,
    )
    description = None
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line).get("MESSAGE")
        except json.JSONDecodeError:
            continue
        if not isinstance(message, str):
            continue
        prefix = f"Started {unit} - "
        if message.startswith(prefix):
            description = message[len(prefix) :].removesuffix(".")
    return description


def attribute(
    unit: str,
    *,
    killed_at_ms: int,
    lifecycle_dir: Path,
    describe: Callable[[str], str | None],
) -> Attribution:
    """Which session's command the kill in *unit* hit, or why none can be named."""
    if probe.SESSION_SCOPE_RE.match(unit):
        session, why = probe.session_for_scope(lifecycle_dir, unit, at_ms=killed_at_ms)
        return Attribution("session", session, None, why or None)
    if probe.HEAVY_SCOPE_RE.match(unit):
        description = describe(unit)
        if description is None:
            return Attribution(
                "heavy",
                None,
                None,
                f"the journal holds no description for {unit}",
            )
        match = probe.HEAVY_DESCRIPTION_RE.match(description)
        if match is not None:
            session, why = probe.session_for_scope(
                lifecycle_dir, match.group(1), at_ms=killed_at_ms
            )
            return Attribution("heavy", session, match.group(2), why or None)
        match = probe.HEAVY_OUTSIDE_DESCRIPTION_RE.match(description)
        if match is not None:
            return Attribution(
                "heavy",
                None,
                match.group(1),
                "heavy ran outside any claudewheel session",
            )
        return Attribution(
            "heavy",
            None,
            None,
            f"{unit}'s description names no claudewheel session: the heavy that "
            "started it does not record one",
        )
    return Attribution(
        "other",
        None,
        None,
        f"{unit} is neither a claudewheel session scope nor a heavy scope",
    )


# ---------------------------------------------------------------------------
# One journal entry
# ---------------------------------------------------------------------------


def _report_exists(store: ProbeStore, session: str, report_id: str) -> bool:
    for state in probe.REPORT_STATES:
        directory = store.report_dir(state, session)
        if directory.is_dir() and any(directory.glob(f"{report_id}.*.json")):
            return True
    return False


def _watches(state: ProbeState, session: str | None, killed_at_ms: int) -> bool:
    if not state.active or state.kind != "oom-kill":
        return False
    if lifecycle.parse_timestamp_ms(state.deadline) < killed_at_ms:
        return False
    if lifecycle.parse_timestamp_ms(state.created_at) > killed_at_ms:
        return False
    return state.watch_session is None or state.watch_session == session


def process_entry(
    ws: Workspace,
    entry: Mapping[str, Any],
    *,
    describe: Callable[[str], str | None] = heavy_description,
) -> dict[str, Any] | None:
    """Turn one journal entry into its reports and its kill record.

    Returns the kill record, or None when the entry is not an OOM kill of a
    unit or was already recorded (a restarted runner may read it twice).
    """
    if entry.get("MESSAGE_ID") != OOM_KILL_MESSAGE_ID:
        return None
    unit = entry.get("USER_UNIT")
    cursor = entry.get("__CURSOR")
    realtime = entry.get("__REALTIME_TIMESTAMP")
    if not isinstance(unit, str) or not isinstance(cursor, str) or not unit:
        return None
    store = ws.probes
    kill_id = _digest_id("kill", cursor)
    if any(k["id"] == kill_id for k in probe.read_kills(store)):
        return None
    killed_at_us = int(str(realtime))
    killed_at_ms = killed_at_us // 1000
    who = attribute(
        unit,
        killed_at_ms=killed_at_ms,
        lifecycle_dir=ws.shared.lifecycle_dir,
        describe=describe,
    )
    kill: dict[str, Any] = {
        "format_version": probe.FORMAT_VERSION,
        "id": kill_id,
        "at": lifecycle.now_timestamp(),
        "killed_at_us": killed_at_us,
        "unit": unit,
        "cursor": cursor,
        "scope": who.scope,
        "session": who.session,
        "command": who.command,
        "unattributed": who.unattributed,
        "reports": [],
        "probes": [],
    }
    kill["label"] = probe.kill_label(kill)
    woken: set[str] = set()

    def queue(
        session: str,
        recipient: str,
        *,
        agent: str | None,
        task: str | None,
        probe_id: str | None,
        subscription: str | None,
        text_for: Callable[[str], str],
    ) -> None:
        report_id = _digest_id("report", cursor, session, recipient)
        kill["reports"].append(report_id)
        if _report_exists(store, session, report_id):
            return
        probe.write_report(
            store,
            Report(
                id=report_id,
                at=lifecycle.now_timestamp(),
                session=session,
                agent=agent,
                task=task,
                probe=probe_id,
                subscription=subscription,
                kill=kill_id,
                text=text_for(report_id),
            ),
            recipient,
        )
        woken.add(session)

    if who.session is not None:
        queue(
            who.session,
            "main",
            agent=None,
            task=None,
            probe_id=None,
            subscription=None,
            text_for=lambda rid: probe.own_kill_text(rid, kill),
        )

    for state in probe.load_probes(store).values():
        if not _watches(state, who.session, killed_at_ms):
            continue
        kill["probes"].append(state.id)
        for sub in state.subscriptions.values():
            if not sub.active:
                continue
            if sub.session == who.session and sub.agent is None:
                continue  # the session's own report already tells its main conversation
            agents = probe.read_session_agents(store, sub.session)
            if sub.agent == probe.UNBOUND:
                recipient = probe.recipient_of(None, sub.id, bound=False)
                agent, task = None, None
            elif sub.agent is None:
                recipient, agent, task = "main", None, None
            else:
                info = agents.get(sub.agent)
                agent, task = sub.agent, (info.task if info else None)
                finished = info is not None and info.finished
                recipient = "main" if finished else probe.recipient_of(sub.agent)
            probe_id = state.id
            queue(
                sub.session,
                recipient,
                agent=agent,
                task=task,
                probe_id=probe_id,
                subscription=sub.id,
                # queue calls this before the loop moves on, so probe_id is current.
                text_for=lambda rid: probe.probe_kill_text(rid, probe_id, kill),
            )

    probe.append_kill(store, kill)
    for session in sorted(woken):
        probe.wake_waiter(store, session)
    return kill


# ---------------------------------------------------------------------------
# Keeping the store moving
# ---------------------------------------------------------------------------


def _session_state(
    lifecycles: Mapping[str, lifecycle.SessionLifecycle], session: str
) -> tuple[bool, str | None]:
    """Whether *session*'s client runs now, and its transcript path."""
    life = lifecycles.get(session)
    if life is None or life.started is None:
        return False, None
    started = life.started
    live = (
        life.ended is None
        and started.pid is not None
        and Path(f"/proc/{started.pid}").exists()
    )
    return live, started.transcript


def _transcripts(transcript: str | None) -> list[Path]:
    """The session's transcript and its subagents' transcripts."""
    if transcript is None:
        return []
    main = Path(transcript)
    out = [main]
    subagents = main.with_suffix("") / "subagents"
    if subagents.is_dir():
        out.extend(sorted(subagents.glob("*.jsonl")))
    return out


def _mentions(paths: list[Path], needle: bytes) -> bool:
    for path in paths:
        try:
            if needle in path.read_bytes():
                return True
        except FileNotFoundError:
            continue
    return False


def _latest_mtime(paths: list[Path]) -> float:
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except FileNotFoundError:
            continue
    return latest


def end_probes(ws: Workspace, *, now_ms: int) -> list[tuple[str, str]]:
    """End every live probe whose deadline, count, file, or watched session says so."""
    store = ws.probes
    ended: list[tuple[str, str]] = []
    live = [state for state in probe.load_probes(store).values() if state.active]
    if not live:
        return ended
    # Read only what a live probe's stops need: this runs every tick.
    lifecycles = (
        lifecycle.load_all(ws.shared.lifecycle_dir)
        if any(state.until_watched_ends for state in live)
        else {}
    )
    kills = (
        probe.read_kills(store)
        if any(state.until_count is not None for state in live)
        else []
    )
    for state in live:
        reason: str | None = None
        if lifecycle.parse_timestamp_ms(state.deadline) <= now_ms:
            reason = "deadline"
        elif state.until_count is not None and (
            sum(1 for k in kills if state.id in k["probes"]) >= state.until_count
        ):
            reason = "count"
        elif state.until_file is not None and Path(state.until_file).exists():
            reason = "file"
        elif state.until_watched_ends and state.watch_session is not None:
            life = lifecycles.get(state.watch_session)
            if life is not None and life.ended is not None:
                reason = "watched-ended"
        if reason is not None:
            probe.append_probe_event(store, state.id, "ended", reason=reason)
            ended.append((state.id, reason))
    return ended


def settle_reports(
    ws: Workspace, *, now: float, states: tuple[str, ...] = ("pending", "handed")
) -> dict[str, int]:
    """Confirm and requeue handed reports and expire pending ones; return how many of each."""
    store = ws.probes
    counts = {"delivered": 0, "requeued": 0, "expired": 0}
    reports = probe.list_reports(store, states)
    if not reports:
        return counts
    lifecycles = lifecycle.load_all(ws.shared.lifecycle_dir)
    probes = probe.load_probes(store)
    for item in reports:
        report = item.report
        live, transcript = _session_state(lifecycles, report.session)
        paths = _transcripts(transcript)
        if item.state == "handed":
            needle = f"claudewheel probe report {report.id}".encode()
            if _mentions(paths, needle):
                probe.move_report(store, item, "delivered")
                counts["delivered"] += 1
                continue
            handed_at = item.path.stat().st_mtime
            moved_on = _latest_mtime(paths) > handed_at + LOST_AFTER_SECONDS
            if not live or moved_on:
                probe.move_report(store, item, "pending")
                probe.wake_waiter(store, report.session)
                counts["requeued"] += 1
            continue
        # pending
        if report.probe is None or live:
            continue
        state = probes.get(report.probe)
        if state is not None and not state.active:
            probe.move_report(store, item, "expired")
            counts["expired"] += 1
    return counts


# How often pending reports are checked for expiry, in seconds. A handed report
# is settled every tick, since a session is waiting on it; a pending one can
# only expire, and one kept for an ended session may wait for months.
EXPIRY_EVERY_SECONDS = 60.0


def tick(ws: Workspace, *, expire: bool) -> None:
    """One pass of keeping the store moving; *expire* also checks pending reports."""
    end_probes(ws, now_ms=probe.now_ms())
    settle_reports(
        ws, now=time.time(), states=("pending", "handed") if expire else ("handed",)
    )


# ---------------------------------------------------------------------------
# Following the journal
# ---------------------------------------------------------------------------


def journal_argv(cursor: str | None) -> list[str]:
    """journalctl following the OOM-kill entries, after *cursor* or from now on."""
    argv = [
        "journalctl",
        "--user",
        "--follow",
        "-o",
        "json",
        "--no-pager",
        f"MESSAGE_ID={OOM_KILL_MESSAGE_ID}",
    ]
    if cursor:
        argv.append(f"--after-cursor={cursor}")
    else:
        argv.extend(["--lines", "0"])
    return argv


def _lines(stream: Any, stop: Callable[[], bool]) -> Iterator[bytes | None]:
    """Complete lines from *stream*, and None every tick with nothing to read."""
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    buffer = b""
    while not stop():
        if not selector.select(TICK_SECONDS):
            yield None
            continue
        chunk = os.read(stream.fileno(), 65536)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line


def run(ws: Workspace) -> int:
    """Follow the journal until SIGTERM; return the exit status."""
    stopping = False

    def on_term(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    store = ws.probes
    effects.mkdir(store.root, parents=True, exist_ok=True)
    cursor = (
        store.cursor_file.read_text().strip() if store.cursor_file.exists() else None
    )
    follower = effects.follow(journal_argv(cursor))
    print(
        f"{SERVICE_NAME}: following OOM kills "
        + (f"after cursor {cursor}" if cursor else "from now on"),
        file=sys.stderr,
        flush=True,
    )
    last_expiry = 0.0

    def keep_moving() -> None:
        nonlocal last_expiry
        expire = time.monotonic() - last_expiry >= EXPIRY_EVERY_SECONDS
        tick(ws, expire=expire)
        if expire:
            last_expiry = time.monotonic()

    try:
        keep_moving()
        for line in _lines(follower.stdout, lambda: stopping):
            if line is None:
                keep_moving()
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"{SERVICE_NAME}: unreadable journal line: {line[:200]!r}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            kill = process_entry(ws, entry)
            if kill is not None:
                print(
                    f"{SERVICE_NAME}: {kill['unit']}: session {kill['session']}, "
                    f"{len(kill['reports'])} report(s)",
                    file=sys.stderr,
                    flush=True,
                )
            if isinstance(entry.get("__CURSOR"), str):
                effects.write_text_atomic(store.cursor_file, entry["__CURSOR"] + "\n")
            keep_moving()
        if not stopping:
            print(f"{SERVICE_NAME}: journalctl ended", file=sys.stderr, flush=True)
            return 1
        return 0
    finally:
        follower.terminate()
        try:
            follower.wait(timeout=10)
        except subprocess.TimeoutExpired:
            follower.kill()


def main() -> None:
    sys.exit(run(Workspace.default()))


if __name__ == "__main__":
    main()
