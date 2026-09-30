"""Probes: watch Claude Code sessions for an event and report it to the sessions that asked.

A probe watches for one kind of event -- the only kind is ``oom-kill``,
systemd's result term for a unit whose process the kernel's OOM killer killed
-- in one Claude Code session or in all of them, until its deadline or an
earlier stop, and reports each event to the sessions subscribed to it.
Independently of any probe, every OOM kill of a command a session started (in
its own session scope, in the tool scope of one of its Bash commands, or in a
``heavy`` scope it launched) is reported to that session; that report has no
probe and no deadline.

``claudewheel-probe-runner.service`` (:mod:`claudewheel.probe_runner`) reads
the kills from the user journal and writes the reports; two hook scripts hand
them to the sessions. This module owns the store they share, under
``~/.claudewheel/shared/probes/``:

==========================================  ===================================
``probes/<probe-id>.jsonl``                 one probe's log (probe-event schema)
``kills.jsonl``                             every kill read (oom-kill-event schema)
``sessions/<session>.jsonl``                a session's Bash calls and subagents
                                            (probe-session-event schema)
``reports/<state>/<session>/<file>``        one report (probe-report schema)
``waiters/<session>.lock``, ``.fifo``       the one waiter per session
``journal-cursor``                          where the runner resumes
==========================================  ===================================

A report's state is the directory it is in -- ``pending``, ``handed`` (given
to the session, not yet seen in its transcript), ``delivered`` or ``expired``
-- and its file name ``<report-id>.<recipient>.json`` names the conversation it
is for: ``main``, ``agent-<agent id>``, or ``unbound-<subscription id>`` while
the subscription waits to learn which conversation created it. Every move is a
rename, so a reader never sees half a report.

The line and document shapes are the four ``.strictspec/*.schema.toml`` files';
this module keeps what strictspec cannot see: the state a reader derives from a
probe's whole log, which session a scope belongs to, and the report texts.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from . import effects, lifecycle

# The kinds a probe can watch for. A probe that would run an arbitrary command
# is refused: a new kind comes when a user needs it.
PROBE_KINDS: tuple[str, ...] = ("oom-kill",)

# Every line and document claudewheel writes here carries this, and it is the
# only one read.
FORMAT_VERSION = 1

# Probe, subscription, report and kill ids: 16 lowercase hex digits.
ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Claude Code's agent_id, as the schemas accept it.
AGENT_RE = re.compile(r"^[0-9a-zA-Z_-]+$")

# The scope claudewheel starts each session in (claudewheel.launch.do_launch):
# the Claude Code process id, then the launch time in whole seconds.
SESSION_SCOPE_RE = re.compile(r"^claudewheel-session-(\d+)-(\d+)\.scope$")

# The scope each Bash command of a session runs in (the claudewheel-tool-scope
# shell prefix): the session scope's Claude Code process id and launch time,
# then the wrapper's own process id. It sits in the session's tools slice.
TOOL_SCOPE_RE = re.compile(r"^claudewheel-tool-(\d+)-(\d+)-(\d+)\.scope$")

# The scope heavy runs each job in.
HEAVY_SCOPE_RE = re.compile(r"^heavy-(\d+)-(\d+)\.scope$")

# heavy's scope description: the session scope heavy ran in, read from its own
# cgroup, then the command. The journal keeps it in the scope's "Started" line
# after the scope is gone.
HEAVY_DESCRIPTION_RE = re.compile(
    r"^heavy job of (claudewheel-session-\d+-\d+\.scope): (.*)$", re.DOTALL
)
HEAVY_OUTSIDE_DESCRIPTION_RE = re.compile(
    r"^heavy job outside any claudewheel session: (.*)$", re.DOTALL
)

# What to do about a command killed for its memory. heavy's kill message and
# every OOM report and label are built from this one text.
OOM_KILL_FIX = (
    "a command that outgrows its cap is a defect to fix at the source, so stop "
    "this line of work at a clean committed point, find where the memory goes "
    "(a heap profile, what is held at once, what is loaded that need not be), "
    "and cut it"
)

# How the label of an OOM-killed tool call begins.
OOM_KILL_LABEL = (
    "this command was OOM-killed: fix the memory at its source, do not rerun"
)


# The line probe create and probe subscribe print for the hook that binds the
# subscription to the conversation whose tool call ran them. The hook finds
# the two ids in the call's output with the same pattern.
BIND_LINE = "subscription {subscription} of probe {probe} waits to be bound to the conversation that ran this command"
BIND_LINE_RE = re.compile(
    r"subscription ([0-9a-f]{16}) of probe ([0-9a-f]{16}) waits to be bound"
)

# How long the hook that labels a tool call ended with status 137 waits
# for the runner to record the kill, which it reads from the journal a
# moment after it happens, in seconds.
HOOK_WAIT_SECONDS = 3

REPORT_STATES: tuple[str, ...] = ("pending", "handed", "delivered", "expired")

# The Claude Code versions the delivery mechanics (asyncRewake, rewakeMessage,
# rewakeSummary, additionalContext on tool events) are verified against: the
# integration test runs a real session of each one. rewakeMessage and
# rewakeSummary are internal to Claude Code's settings schema, so a version
# that drops them is found by that test, not by users.
VERIFIED_CLIENT_VERSIONS: tuple[str, ...] = ("2.1.281",)


# The user service that hosts every probe (claudewheel.probe_runner).
SERVICE_NAME = "claudewheel-probe-runner.service"


class ProbeError(Exception):
    """The probe store, or a request against it, cannot be honored."""


def new_id() -> str:
    """A fresh 16-hex-digit id."""
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeStore:
    """Path owner for ``~/.claudewheel/shared/probes``. Computes paths only."""

    root: Path

    @property
    def probes_dir(self) -> Path:
        return self.root / "probes"

    @property
    def kills_file(self) -> Path:
        return self.root / "kills.jsonl"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def waiters_dir(self) -> Path:
        return self.root / "waiters"

    @property
    def cursor_file(self) -> Path:
        return self.root / "journal-cursor"

    def probe_file(self, probe: str) -> Path:
        if not ID_RE.match(probe):
            raise ProbeError(f"not a probe id: {probe!r} (16 lowercase hex digits)")
        return self.probes_dir / f"{probe}.jsonl"

    def session_file(self, session: str) -> Path:
        _require_session(session)
        return self.sessions_dir / f"{session}.jsonl"

    def report_dir(self, state: str, session: str) -> Path:
        if state not in REPORT_STATES:
            raise ValueError(f"unknown report state {state!r}")
        _require_session(session)
        return self.reports_dir / state / session

    def fifo(self, session: str) -> Path:
        _require_session(session)
        return self.waiters_dir / f"{session}.fifo"


def _require_session(session: str) -> None:
    if not lifecycle.SESSION_UUID_RE.match(session):
        raise ValueError(f"not a Claude Code session uuid: {session!r}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(validator: Any, raw: str, syntax: str, where: str) -> None:
    import strictspec

    data = raw.encode("utf-8")
    marker = strictspec.version_gate(validator._program, data, syntax)
    if not marker.ok:
        raise ProbeError(
            f"{where}: " + "; ".join(d.message for d in marker.diagnostics)
        )
    _root, diags = validator.validate_bytes(data, syntax)
    if diags:
        raise ProbeError(f"{where}: " + "; ".join(d.message for d in diags))


def _probe_validator() -> Any:
    from .strictspec_gen import probe_event_validator

    return probe_event_validator


def _report_validator() -> Any:
    from .strictspec_gen import probe_report_validator

    return probe_report_validator


def _kill_validator() -> Any:
    from .strictspec_gen import oom_kill_event_validator

    return oom_kill_event_validator


def _session_validator() -> Any:
    from .strictspec_gen import probe_session_event_validator

    return probe_session_event_validator


def _read_jsonl(path: Path, validator: Any) -> list[dict[str, Any]]:
    """Every line of *path*, validated. A final line cut off mid-write is dropped."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = text.split("\n")
    complete = text.endswith("\n")
    if complete:
        lines.pop()
    out: list[dict[str, Any]] = []
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            if lineno == len(lines) and not complete:
                break
            raise ProbeError(f"{path}:{lineno}: malformed JSON: {exc}") from exc
        _validate(validator, line, "jsonl", f"{path}:{lineno}")
        out.append(data)
    return out


def _append_jsonl(path: Path, data: Mapping[str, Any], validator: Any) -> None:
    line = json.dumps(data, separators=(",", ":"))
    _validate(validator, line, "jsonl", str(path))
    effects.mkdir(path.parent, parents=True, exist_ok=True)
    lead = ""
    try:
        with open(path, "rb") as handle:  # a read: the chokepoint polices writes
            if handle.seek(0, os.SEEK_END) > 0:
                handle.seek(-1, os.SEEK_END)
                lead = "" if handle.read(1) == b"\n" else "\n"
    except FileNotFoundError:
        pass
    with effects.open_write(path, "a", encoding="utf-8") as out:
        out.write(f"{lead}{line}\n")


# ---------------------------------------------------------------------------
# Probe logs
# ---------------------------------------------------------------------------

# The agent value of a subscription no hook has bound yet.
UNBOUND = "unbound"


@dataclass
class Subscription:
    """One subscription to a probe, as its log reads."""

    id: str
    session: str
    # None: the main conversation; UNBOUND: not bound yet; else the agent id.
    agent: str | None
    active: bool = True


@dataclass
class ProbeState:
    """One probe, read from its whole log."""

    id: str
    session: str
    kind: str
    watch_session: str | None
    deadline: str
    until_count: int | None
    until_watched_ends: bool
    until_file: str | None
    created_at: str
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    ended: str | None = None
    ended_at: str | None = None

    @property
    def active(self) -> bool:
        return self.ended is None


def _event(probe: str, kind: str, **values: Any) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "id": lifecycle.new_event_id(),
        "at": lifecycle.now_timestamp(),
        "probe": probe,
        "kind": kind,
        **values,
    }


def append_probe_event(
    store: ProbeStore, probe: str, kind: str, **values: Any
) -> dict[str, Any]:
    """Validate and append one line to *probe*'s log; return it."""
    event = _event(probe, kind, **values)
    _append_jsonl(store.probe_file(probe), event, _probe_validator())
    return event


def read_probe(store: ProbeStore, probe: str) -> ProbeState:
    """Read *probe*'s whole log into its state; an absent probe is refused."""
    path = store.probe_file(probe)
    events = _read_jsonl(path, _probe_validator())
    if not events:
        raise ProbeError(f"no probe {probe} (no {path})")
    first = events[0]
    if first["kind"] != "created":
        raise ProbeError(f"{path}:1: a probe log starts with its created line")
    state = ProbeState(
        id=probe,
        session=first["session"],
        kind=first["probe_kind"],
        watch_session=first["watch_session"],
        deadline=first["deadline"],
        until_count=first["until_count"],
        until_watched_ends=first["until_watched_ends"],
        until_file=first["until_file"],
        created_at=first["at"],
    )
    for lineno, event in enumerate(events, start=1):
        if event["probe"] != probe:
            raise ProbeError(f"{path}:{lineno}: line names probe {event['probe']}")
        kind = event["kind"]
        if kind == "subscribed":
            state.subscriptions[event["subscription"]] = Subscription(
                event["subscription"], event["session"], UNBOUND
            )
        elif kind == "bound":
            sub = state.subscriptions.get(event["subscription"])
            if sub is not None and sub.agent == UNBOUND:
                sub.agent = event["agent"]
        elif kind == "unsubscribed":
            sub = state.subscriptions.get(event["subscription"])
            if sub is not None:
                sub.active = False
        elif kind == "ended" and state.ended is None:
            state.ended = event["reason"]
            state.ended_at = event["at"]
    return state


def load_probes(store: ProbeStore) -> dict[str, ProbeState]:
    """Every probe in the store, keyed by id."""
    if not store.probes_dir.is_dir():
        return {}
    out: dict[str, ProbeState] = {}
    for path in sorted(store.probes_dir.glob("*.jsonl")):
        if not ID_RE.match(path.stem):
            raise ProbeError(f"{path}: file name is not a probe id")
        out[path.stem] = read_probe(store, path.stem)
    return out


def find_subscription(
    probes: Mapping[str, ProbeState], subscription: str
) -> tuple[ProbeState, Subscription]:
    """The probe holding *subscription*; refused when no probe does."""
    for state in probes.values():
        sub = state.subscriptions.get(subscription)
        if sub is not None:
            return state, sub
    raise ProbeError(f"no subscription {subscription} in any probe")


# ---------------------------------------------------------------------------
# Kills
# ---------------------------------------------------------------------------


def append_kill(store: ProbeStore, kill: Mapping[str, Any]) -> None:
    _append_jsonl(store.kills_file, kill, _kill_validator())


def read_kills(store: ProbeStore) -> list[dict[str, Any]]:
    return _read_jsonl(store.kills_file, _kill_validator())


# ---------------------------------------------------------------------------
# Session calls and agents
# ---------------------------------------------------------------------------


@dataclass
class AgentInfo:
    agent: str
    task: str | None
    finished: bool


def read_session_agents(store: ProbeStore, session: str) -> dict[str, AgentInfo]:
    """The subagents a session launched, from the hook's record."""
    out: dict[str, AgentInfo] = {}
    for event in _read_jsonl(store.session_file(session), _session_validator()):
        if event["kind"] == "agent-launched":
            out[event["agent"]] = AgentInfo(event["agent"], event["task"], False)
        elif event["kind"] == "agent-finished":
            info = out.setdefault(
                event["agent"], AgentInfo(event["agent"], None, False)
            )
            info.finished = True
    return out


def read_session_events(store: ProbeStore, session: str) -> list[dict[str, Any]]:
    return _read_jsonl(store.session_file(session), _session_validator())


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    """One report, as its document reads."""

    id: str
    at: str
    session: str
    agent: str | None
    task: str | None
    probe: str | None
    subscription: str | None
    kill: str
    text: str

    def to_json(self) -> str:
        data: dict[str, Any] = {"format_version": FORMAT_VERSION}
        for f in fields(self):
            data[f.name] = getattr(self, f.name)
        return json.dumps(data, indent=2) + "\n"


@dataclass(frozen=True)
class ReportFile:
    """A report where it lies: its state, its recipient, and its path."""

    state: str
    recipient: str
    path: Path
    report: Report


def recipient_of(
    agent: str | None, subscription: str | None = None, *, bound: bool = True
) -> str:
    """The recipient part of a report's file name."""
    if not bound:
        assert subscription is not None
        return f"unbound-{subscription}"
    return "main" if agent is None else f"agent-{agent}"


def write_report(store: ProbeStore, report: Report, recipient: str) -> Path:
    """Validate *report* and write it as pending for *recipient*."""
    text = report.to_json()
    _validate(_report_validator(), text, "json", f"report {report.id}")
    directory = store.report_dir("pending", report.session)
    effects.mkdir(directory, parents=True, exist_ok=True)
    path = directory / f"{report.id}.{recipient}.json"
    effects.write_text_atomic(path, text)
    return path


def read_report(path: Path) -> Report:
    text = path.read_text(encoding="utf-8")
    _validate(_report_validator(), text, "json", str(path))
    data = json.loads(text)
    data.pop("format_version")
    return Report(**data)


_REPORT_NAME_RE = re.compile(
    r"^([0-9a-f]{16})\.((?:main)|(?:agent-[0-9a-zA-Z_-]+)|(?:unbound-[0-9a-f]{16}))\.json$"
)


def list_reports(
    store: ProbeStore, states: Iterable[str] = REPORT_STATES
) -> list[ReportFile]:
    """Every report in the given states, oldest first by id order within a session."""
    out: list[ReportFile] = []
    for state in states:
        base = store.reports_dir / state
        if not base.is_dir():
            continue
        for session_dir in sorted(base.iterdir()):
            if not session_dir.is_dir():
                continue
            for path in sorted(session_dir.glob("*.json")):
                match = _REPORT_NAME_RE.match(path.name)
                if match is None:
                    raise ProbeError(
                        f"{path}: not a report file name (<report-id>.<recipient>.json)"
                    )
                out.append(ReportFile(state, match.group(2), path, read_report(path)))
    return out


def move_report(store: ProbeStore, item: ReportFile, state: str) -> Path:
    """Rename *item* into *state*, keeping its file name."""
    target_dir = store.report_dir(state, item.report.session)
    effects.mkdir(target_dir, parents=True, exist_ok=True)
    target = target_dir / item.path.name
    effects.rename(item.path, target)
    return target


def wake_waiter(store: ProbeStore, session: str) -> None:
    """Tell *session*'s waiter, if one is waiting, that its reports changed.

    The waiter holds its FIFO open for reading and writing, so a write never
    blocks; with no waiter there is no FIFO, or no reader, and nothing to wake.
    """
    fifo = store.fifo(session)
    if not fifo.exists():
        return
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return  # no reader: no waiter to wake
    try:
        os.write(fd, b"x")
    except BlockingIOError:
        pass  # the pipe is full of wake-ups already
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Which session a scope belongs to
# ---------------------------------------------------------------------------


def session_scope_of_unit(unit: str) -> str | None:
    """The claudewheel session scope *unit* belongs to, or None.

    A session scope is its own; a Bash command's tool scope names the session
    scope's process id and launch time.
    """
    if SESSION_SCOPE_RE.match(unit):
        return unit
    match = TOOL_SCOPE_RE.match(unit)
    if match is not None:
        return f"claudewheel-session-{match.group(1)}-{match.group(2)}.scope"
    return None


def session_scope_of_cgroup(cgroup_text: str) -> str | None:
    """The claudewheel session scope a /proc/<pid>/cgroup file places a process in.

    The unified hierarchy's line (``0::<path>``) is read; its last element is
    the session scope itself (Claude Code and its hooks) or one of the
    session's tool scopes (its Bash commands). None for anything else.
    """
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            return session_scope_of_unit(line[3:].rstrip("/").rsplit("/", 1)[-1])
    return None


def session_for_scope(
    lifecycle_dir: Path, scope: str, *, at_ms: int
) -> tuple[str | None, str]:
    """The Claude Code session that ran in *scope* at *at_ms*, from the lifecycle store.

    The scope names the Claude Code process id and the second it was launched
    in; the session is the one whose newest ``started`` line records that pid
    no earlier than the launch and no later than *at_ms*. A process that
    resumed another session, or cleared, writes a new ``started`` line, so the
    newest one is the session it ran then. Returns the session, or None with
    the reason.
    """
    match = SESSION_SCOPE_RE.match(scope)
    if match is None:
        return None, f"{scope} is not a claudewheel session scope"
    pid, launched = int(match.group(1)), int(match.group(2))
    best: tuple[int, str] | None = None
    if lifecycle_dir.is_dir():
        for path in sorted(lifecycle_dir.glob("*.jsonl")):
            for event in lifecycle.read_session(path):
                if not isinstance(event, lifecycle.StartedEvent) or event.pid != pid:
                    continue
                started_ms = lifecycle.parse_timestamp_ms(event.at)
                if started_ms < launched * 1000 or started_ms > at_ms:
                    continue
                if best is None or started_ms >= best[0]:
                    best = (started_ms, event.session)
    if best is None:
        return None, (
            f"the lifecycle store records no session started by Claude Code "
            f"process {pid} of {scope}"
        )
    return best[1], ""


def own_cgroup_text() -> str:
    """This process's /proc/self/cgroup."""
    return Path("/proc/self/cgroup").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Report texts
# ---------------------------------------------------------------------------


def local_time(at_us: int) -> str:
    """A journal timestamp as local wall-clock time, as hook-timestamp prints it."""
    return (
        datetime.fromtimestamp(at_us / 1_000_000)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )


def what_was_killed(kill: Mapping[str, Any]) -> str:
    """One phrase naming what was killed, where, and when."""
    when = local_time(kill["killed_at_us"])
    if kill["scope"] == "heavy":
        command = kill["command"] or "(command not recorded)"
        return f"the heavy job `{command}` in {kill['unit']} was killed at its memory cap at {when}"
    if kill["scope"] == "tool":
        return f"a process of the Bash command in {kill['unit']} was killed at {when}"
    if kill["scope"] == "session":
        return f"a process in the session scope {kill['unit']} was killed at {when}"
    return f"a process in {kill['unit']} was killed at {when}"


def own_kill_text(report_id: str, kill: Mapping[str, Any]) -> str:
    """The report a session gets of its own command's kill."""
    return (
        f"[claudewheel probe report {report_id}] A command this session started "
        f"was OOM-killed: {what_was_killed(kill)}. Fix the memory at its source, "
        f"do not rerun it: {OOM_KILL_FIX}."
    )


def probe_kill_text(report_id: str, probe: str, kill: Mapping[str, Any]) -> str:
    """The report a subscriber gets from a probe."""
    whose = (
        f"in claudewheel session {kill['session']}"
        if kill["session"] is not None
        else f"outside any claudewheel session ({kill['unattributed']})"
    )
    return (
        f"[claudewheel probe report {report_id}] Probe {probe} saw an OOM kill "
        f"{whose}: {what_was_killed(kill)}."
    )


def kill_label(kill: Mapping[str, Any]) -> str:
    """What a tool call that ended OOM-killed during *kill* is told."""
    return f"{OOM_KILL_LABEL} ({what_was_killed(kill)}; {OOM_KILL_FIX})."


# Added to a call's label when other Bash calls of the session were running at
# the kill; the hook fills in {calls}. Every overlapping call that ends
# OOM-killed gets its own label with this sentence, so both are told.
OVERLAP_SENTENCE = (
    " Other Bash calls of this session were running at the kill ({calls}), so "
    "the killed process may have been theirs; each of them that ended "
    "OOM-killed is told the same."
)


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^([1-9][0-9]*)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """A whole number of seconds, minutes, hours, or days (90s, 30m, 2h, 7d) in seconds."""
    match = _DURATION_RE.match(text)
    if match is None:
        raise ProbeError(
            f"a duration is a whole number with an s, m, h, or d suffix (90s, 30m, 2h, 7d), not {text!r}"
        )
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


# ---------------------------------------------------------------------------
# The probe commands' operations
# ---------------------------------------------------------------------------


def resolve_session(lifecycle_dir: Path, cgroup_text: str, *, at_ms: int) -> str:
    """The Claude Code session the calling process belongs to, from its own cgroup.

    Refused outside a claudewheel session, naming the fix: a probe command is
    run from a Bash tool call of a session claudewheel launched, whose Bash
    commands run in tool scopes that name the session's scope.
    """
    scope = session_scope_of_cgroup(cgroup_text)
    if scope is None:
        where = next(
            (line[3:] for line in cgroup_text.splitlines() if line.startswith("0::")),
            "unknown",
        )
        raise ProbeError(
            "a probe command learns its session from its own cgroup, and this "
            f"process runs outside any claudewheel session scope (its cgroup is "
            f"{where}); run it from a Bash tool call of a Claude Code session "
            "claudewheel launched, whose commands run in scopes named "
            "claudewheel-tool-<pid>-<time>-<n>.scope after the session's "
            "claudewheel-session-<pid>-<time>.scope, and not through heavy, "
            "which runs its command in a scope of its own"
        )
    session, why = session_for_scope(lifecycle_dir, scope, at_ms=at_ms)
    if session is None:
        raise ProbeError(
            f"{why}; the hook-session-start hook records each session as it "
            "starts, so this session started before that hook was wired, or "
            "its record failed: relaunch the session with claudewheel"
        )
    return session


def create_probe(
    store: ProbeStore,
    lifecycle_dir: Path,
    *,
    session: str,
    kind: str,
    watch_session: str | None,
    deadline_seconds: int,
    until_count: int | None,
    until_watched_ends: bool,
    until_file: str | None,
    now: int,
) -> tuple[str, str]:
    """Create a probe owned by *session* and subscribe *session* to it.

    Returns the probe id and the subscription id. Every refusal happens before
    anything is written.
    """
    if kind not in PROBE_KINDS:
        raise ProbeError(
            f"no probe kind {kind!r}; the kinds that exist are: {', '.join(PROBE_KINDS)}"
        )
    if watch_session is not None:
        _require_session_arg(watch_session)
        if watch_session not in lifecycle.load_all(lifecycle_dir):
            raise ProbeError(
                f"the lifecycle store records no session {watch_session}; name a "
                "session claudewheel has recorded (claudewheel's sessions overview "
                "lists them)"
            )
    elif until_watched_ends:
        raise ProbeError(
            "--until-watched-ends needs a watched session, and a probe of all "
            "sessions has none"
        )
    if until_count is not None and until_count < 1:
        raise ProbeError(
            f"--count takes a whole number of at least 1, not {until_count}"
        )
    if until_file is not None and not os.path.isabs(until_file):
        raise ProbeError(f"--until-file takes an absolute path, not {until_file!r}")
    probe_id = new_id()
    subscription = new_id()
    append_probe_event(
        store,
        probe_id,
        "created",
        session=session,
        probe_kind=kind,
        watch_session=watch_session,
        deadline=lifecycle.now_timestamp(now + deadline_seconds * 1000),
        until_count=until_count,
        until_watched_ends=until_watched_ends,
        until_file=until_file,
    )
    append_probe_event(
        store, probe_id, "subscribed", subscription=subscription, session=session
    )
    return probe_id, subscription


def _require_session_arg(session: str) -> None:
    if not lifecycle.SESSION_UUID_RE.match(session):
        raise ProbeError(
            f"not a Claude Code session uuid: {session!r} (lowercase 8-4-4-4-12 hex)"
        )


def _require_id(kind: str, value: str) -> None:
    if not ID_RE.match(value):
        raise ProbeError(f"not a {kind} id: {value!r} (16 lowercase hex digits)")


def subscribe(store: ProbeStore, *, session: str, probe_id: str) -> str:
    """Subscribe *session* to a live probe; return the subscription id."""
    _require_id("probe", probe_id)
    state = read_probe(store, probe_id)
    if not state.active:
        raise ProbeError(
            f"probe {probe_id} ended ({state.ended}); it reports nothing more"
        )
    subscription = new_id()
    append_probe_event(
        store, probe_id, "subscribed", subscription=subscription, session=session
    )
    return subscription


def unsubscribe(store: ProbeStore, *, session: str, subscription: str) -> str:
    """Remove one of *session*'s subscriptions; return its probe id."""
    _require_id("subscription", subscription)
    state, sub = find_subscription(load_probes(store), subscription)
    if sub.session != session:
        raise ProbeError(
            f"subscription {subscription} belongs to session {sub.session}, not "
            f"to this one ({session}); a session removes only its own"
        )
    if not sub.active:
        raise ProbeError(f"subscription {subscription} was already removed")
    append_probe_event(store, state.id, "unsubscribed", subscription=subscription)
    return state.id


def stop_probe(store: ProbeStore, *, session: str, probe_id: str) -> None:
    """End a live probe *session* created."""
    _require_id("probe", probe_id)
    state = read_probe(store, probe_id)
    if state.session != session:
        raise ProbeError(
            f"probe {probe_id} was created by session {state.session}, not by "
            f"this one ({session}); only the session that created a probe stops it"
        )
    if not state.active:
        raise ProbeError(f"probe {probe_id} already ended ({state.ended})")
    append_probe_event(store, probe_id, "ended", reason="stopped")


def _when(at: str) -> str:
    return local_time(lifecycle.parse_timestamp_ms(at) * 1000)


def describe_probe(state: ProbeState) -> list[str]:
    """The lines ``probe list`` shows for one probe."""
    watch = f"session {state.watch_session}" if state.watch_session else "all sessions"
    stops = [f"deadline {_when(state.deadline)}"]
    if state.until_count is not None:
        stops.append(f"after {state.until_count} kill(s)")
    if state.until_watched_ends:
        stops.append("when the watched session ends")
    if state.until_file is not None:
        stops.append(f"when {state.until_file} exists")
    status = (
        "live"
        if state.active
        else f"ended ({state.ended}, {_when(state.ended_at or state.created_at)})"
    )
    lines = [
        f"probe {state.id} [{status}]: {state.kind} in {watch}, created by session "
        f"{state.session}; stops at {', '.join(stops)}"
    ]
    for sub in state.subscriptions.values():
        if sub.agent == UNBOUND:
            to = "not bound to a conversation yet"
        elif sub.agent is None:
            to = "main conversation"
        else:
            to = f"subagent {sub.agent}"
        removed = "" if sub.active else " (unsubscribed)"
        lines.append(f"  subscription {sub.id}: session {sub.session}, {to}{removed}")
    return lines


def undelivered_counts(store: ProbeStore) -> dict[str, int]:
    """How many reports each session has not been confirmed to receive."""
    counts: dict[str, int] = {}
    for item in list_reports(store, ["pending", "handed"]):
        counts[item.report.session] = counts.get(item.report.session, 0) + 1
    return counts


def render_list(store: ProbeStore) -> str:
    """Everything the store holds that someone may need to act on, as text."""
    out: list[str] = []
    probes = load_probes(store)
    out.append("Probes:")
    if not probes:
        out.append("  none")
    for state in probes.values():
        out.extend("  " + line for line in describe_probe(state))

    out.append("Undelivered reports:")
    undelivered = list_reports(store, ["pending", "handed"])
    if not undelivered:
        out.append("  none")
    for item in undelivered:
        why = (
            "handed, not yet in the transcript" if item.state == "handed" else "pending"
        )
        if item.recipient.startswith("unbound-"):
            why = "waiting for its subscription to be bound"
        origin = f"probe {item.report.probe}" if item.report.probe else "own command"
        out.append(
            f"  report {item.report.id} to session {item.report.session} "
            f"({item.recipient}), {origin}: {why}"
        )

    out.append("Expired reports:")
    expired = list_reports(store, ["expired"])
    if not expired:
        out.append("  none")
    for item in expired:
        out.append(
            f"  report {item.report.id} to session {item.report.session} "
            f"({item.recipient}), probe {item.report.probe}"
        )

    out.append("Kills no session took (unrouted):")
    unrouted = [k for k in read_kills(store) if not k["reports"]]
    if not unrouted:
        out.append("  none")
    for kill in unrouted:
        out.append(
            f"  {local_time(kill['killed_at_us'])} {kill['unit']}: "
            f"{kill['unattributed'] or 'attributed, but no conversation took it'}"
        )
    return "\n".join(out) + "\n"
