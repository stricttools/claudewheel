"""The per-session lifecycle store: what happened to one Claude Code session.

Claude Code writes a per-session registry entry only while its process lives,
and says nothing at all once the process is gone: a session that exited, or
died, leaves no record of having existed.  This module owns the record that
remains -- an append-only JSONL file per session, under
``~/.claudewheel/shared/lifecycle/<session-uuid>.jsonl``
(:data:`LIFECYCLE_DIRNAME`, resolved by
:attr:`claudewheel.shared_store.SharedStore.lifecycle_dir`)::

    <lifecycle_dir>/<session-uuid>.jsonl   one line per event, append-only

Four kinds of line, discriminated by ``kind``: ``started`` (a session began),
``ended`` (it stopped, by exiting or by dying), ``named`` (it carried a display
name) and ``mark`` (the user marked it, or cleared the mark).  Events are never
edited or deleted -- a mark is removed by appending one whose ``state`` is
``null``, and a session that starts again after exiting simply gets a second
``started``.

The line shape is NOT this module's to define
-------------------------------------------------

``.strictspec/lifecycle-event.schema.toml`` is the authority for the document
shape -- the per-line ``format_version`` marker, the ``kind`` arm set, every
field's type, the enums, which fields are required, and the rejection of an
unknown key -- and the generated validator
(:mod:`claudewheel.strictspec_gen.lifecycle_event_validator`) enforces it.  What
this module keeps is what strictspec cannot see:

* ordering events by ``at``, which is lexical because every timestamp is
  fixed-width RFC 3339 UTC (:func:`now_timestamp`);
* the latest-wins reading of a file (:func:`summarize`), including the
  cross-event rules: an ``ended`` older than the newest ``started`` belongs to a
  previous run, and a ``mark`` with a null ``state`` clears the one before it;
* the state a reader derives from a lifecycle plus what it can observe about the
  process right now (:func:`derive_state`).

What is tolerated, and what is not
----------------------------------

Exactly one kind of damage is tolerated, and only in one position: a FINAL line
that does not end in a newline and does not parse as JSON is an interrupted
write, and :func:`read_session` drops it without a word.  Everything else --
a missing or wrong ``format_version``, an unknown ``kind``, an unknown key, a
damaged line anywhere but the end -- raises :class:`LifecycleError` naming the
file and the 1-based line number.  An absent file and an empty file both yield
no events, which is the normal state of a session nothing has recorded yet.

Every write goes through :mod:`claudewheel.effects`, so a ``--dry-run`` records
it instead of performing it.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from . import effects
from .shared_store import LIFECYCLE_DIRNAME

__all__ = [
    "FORMAT_VERSION",
    "HIDDEN_BY_DEFAULT_STATES",
    "LIFECYCLE_DIRNAME",
    "LIVE_STATES",
    "LOOSE_END_STATES",
    "SESSION_UUID_RE",
    "STATES",
    "SWEEP_GRACE_MS",
    "EndedEvent",
    "Event",
    "LifecycleError",
    "MarkEvent",
    "NamedEvent",
    "SessionLifecycle",
    "StartedEvent",
    "append_event",
    "capture_name",
    "derive_state",
    "event_to_json",
    "load_all",
    "new_event_id",
    "now_timestamp",
    "parse_event",
    "parse_timestamp_ms",
    "read_session",
    "session_file",
    "summarize",
    "sweep_crashed",
]

# The per-line format_version every line this module writes carries, and the
# only one it reads. The schema declares the same number; a line carrying
# anything else is a hard error, never a line to skip or to upgrade in place.
FORMAT_VERSION = 1

# Claude Code session uuids are lowercase 8-4-4-4-12 hex. A session string is
# also a FILE NAME here, so it is matched before any path is built from it.
SESSION_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# How long after a `started` the observation pass leaves a session alone. A
# session writes its `started` line from a SessionStart hook, before Claude Code
# has registered its process, so a sweep that ran immediately would declare a
# launching session crashed.
SWEEP_GRACE_MS = 60_000

# Every state derive_state can return. The three sets below partition it: a
# reader shows LIVE_STATES as running sessions, LOOSE_END_STATES as things
# wanting attention, and hides HIDDEN_BY_DEFAULT_STATES until asked.
STATES: tuple[str, ...] = (
    "working",
    "shell",
    "idle",
    "waiting",
    "running",
    "unverified",
    "starting",
    "on-hold",
    "blocked",
    "done",
    "crashed",
    "exited",
)
LIVE_STATES = frozenset(
    {"working", "shell", "idle", "waiting", "running", "unverified"}
)
LOOSE_END_STATES = frozenset({"starting", "on-hold", "blocked", "crashed"})
HIDDEN_BY_DEFAULT_STATES = frozenset({"done", "exited"})

# The status values Claude Code's own registry writes, mapped to the state a
# live session is shown in. An unknown status falls to "running": the process is
# there, and only the label is unrecognized.
_LIVE_STATUS_STATES = {
    "busy": "working",
    "shell": "shell",
    "idle": "idle",
    "waiting": "waiting",
}

# The fields every arm carries, in schema order. `kind` follows them and is a
# class attribute rather than a field: it is the discriminator, fixed per arm.
_COMMON_FIELDS = ("id", "at", "session", "source")


class LifecycleError(Exception):
    """A lifecycle file, or a line in one, cannot be read or written."""


# ---------------------------------------------------------------------------
# The events
#
# `id` and `at` default to "" because a caller building an event does not know
# either: append_event stamps them and returns the stamped copy. Every other
# field is stated, including the nullable ones -- "unknown" is written as None,
# never by leaving a field out.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class StartedEvent:
    """A session began running, under a stated config directory and cwd."""

    KIND: ClassVar[str] = "started"

    id: str = ""
    at: str = ""
    session: str
    source: str
    cwd: str
    config_dir: str
    profile: str | None
    claude_version: str | None
    model: str | None
    permissions: str | None
    entry: str
    transcript: str | None
    pid: int | None


@dataclass(frozen=True, kw_only=True)
class EndedEvent:
    """A session stopped running, either by exiting or by dying without one."""

    KIND: ClassVar[str] = "ended"

    id: str = ""
    at: str = ""
    session: str
    source: str
    outcome: str
    reason: str | None
    detail: str | None


@dataclass(frozen=True, kw_only=True)
class NamedEvent:
    """A session carried a display name."""

    KIND: ClassVar[str] = "named"

    id: str = ""
    at: str = ""
    session: str
    source: str
    name: str
    name_source: str | None


@dataclass(frozen=True, kw_only=True)
class MarkEvent:
    """The user marked the session, or cleared a previous mark."""

    KIND: ClassVar[str] = "mark"

    id: str = ""
    at: str = ""
    session: str
    source: str
    state: str | None
    note: str | None


Event = StartedEvent | EndedEvent | NamedEvent | MarkEvent

# append_event returns the same arm it was handed, so a caller that appended an
# EndedEvent gets an EndedEvent back rather than the union.
EventT = TypeVar("EventT", bound=Event)

_ARMS: dict[str, type[Event]] = {
    cls.KIND: cls for cls in (StartedEvent, EndedEvent, NamedEvent, MarkEvent)
}


# ---------------------------------------------------------------------------
# Ids and timestamps
# ---------------------------------------------------------------------------


def new_event_id() -> str:
    """Generate a unique event id: ``<16 hex ns><32 hex uuid4>``.

    Timestamp-prefixed, so ids sort by creation order, and uuid4-suffixed, so
    two events minted in the same nanosecond still differ.
    """
    return format(time.time_ns(), "016x") + uuid.uuid4().hex


def now_timestamp(now_ms: int | None = None) -> str:
    """Render *now_ms* (default: now) as RFC 3339 UTC with milliseconds.

    One fixed-width spelling, always UTC and always three fractional digits, so
    lexical string order over these timestamps IS time order -- which is what
    :func:`summarize` sorts by.
    """
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    seconds, millis = divmod(ms, 1000)
    stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{stamp.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


def parse_timestamp_ms(at: str) -> int:
    """Read an ``at`` timestamp back into milliseconds since the epoch.

    The inverse of :func:`now_timestamp`, and the only way a lifecycle's
    timestamps are compared against a wall clock (the sweep's grace period, and
    ``derive_state``'s "starting" window). A ``+00:00`` offset is read as
    readily as ``Z``: both are legal under the schema's offset datetime.
    """
    try:
        stamp = datetime.fromisoformat(at)
    except ValueError as exc:
        raise LifecycleError(f"not an RFC 3339 timestamp: {at!r}") from exc
    if stamp.tzinfo is None:
        raise LifecycleError(f"timestamp carries no offset: {at!r}")
    delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def session_file(lifecycle_dir: Path, session: str) -> Path:
    """Return the lifecycle file of *session* under *lifecycle_dir*.

    The session string is matched against :data:`SESSION_UUID_RE` first: it
    becomes a file name, so a path must never be built out of junk -- a stray
    ``..`` or separator would address a file outside the store entirely.
    """
    if not SESSION_UUID_RE.match(session):
        raise ValueError(
            f"not a Claude Code session uuid: {session!r} "
            "(lowercase 8-4-4-4-12 hex expected)"
        )
    return lifecycle_dir / f"{session}.jsonl"


# ---------------------------------------------------------------------------
# One line: write, validate, read
# ---------------------------------------------------------------------------


def event_to_json(event: Event) -> str:
    """Serialize *event* to one compact JSON line, without a trailing newline.

    Keys come out in schema order -- ``format_version``, the common fields,
    ``kind``, then the arm's own fields -- and EVERY key is written, including
    the ones whose value is ``None``: the schema requires each of them present,
    so an omitted key is a second spelling of "unknown" that a reader could not
    tell from a writer's mistake.
    """
    data: dict[str, Any] = {"format_version": FORMAT_VERSION}
    for name in _COMMON_FIELDS:
        data[name] = getattr(event, name)
    data["kind"] = event.KIND
    for field in fields(event):
        if field.name not in _COMMON_FIELDS:
            data[field.name] = getattr(event, field.name)
    return json.dumps(data, separators=(",", ":"))


def _where(path: Path, lineno: int | None) -> str:
    """The ``file:line`` prefix every diagnostic carries."""
    return str(path) if lineno is None else f"{path}:{lineno}"


def _validate_line(line: str, *, path: Path, lineno: int | None) -> None:
    """Run the strictspec format_version marker and the full shape validation.

    Absence of ``format_version`` is an error like any other: this store was
    born with the marker, so there is no legacy line to accommodate and nothing
    that could turn the enforcement off.
    """
    import strictspec

    from .strictspec_gen import lifecycle_event_validator as validator

    raw = line.encode("utf-8")
    marker = strictspec.version_gate(validator._program, raw, "jsonl")
    if not marker.ok:
        joined = "; ".join(d.message for d in marker.diagnostics)
        raise LifecycleError(f"{_where(path, lineno)}: {joined}")
    _root, diags = validator.validate_bytes(raw, "jsonl")
    if diags:
        joined = "; ".join(d.message for d in diags)
        raise LifecycleError(f"{_where(path, lineno)}: {joined}")


def parse_event(line: str, *, path: Path, lineno: int) -> Event:
    """Parse one JSON line into the event its ``kind`` selects.

    *path* and *lineno* name the line in every diagnostic; they are keyword-only
    because they are the reader's context, not part of the line.  Validation
    runs before anything is bound, so the keyword expansion below sees a
    document the schema has already accepted in full.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"{_where(path, lineno)}: malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LifecycleError(f"{_where(path, lineno)}: line is not a JSON object")

    _validate_line(line, path=path, lineno=lineno)

    kind = data["kind"]
    arm = _ARMS[kind]
    bound = {k: v for k, v in data.items() if k not in ("format_version", "kind")}
    return arm(**bound)


# ---------------------------------------------------------------------------
# One file: read and append
# ---------------------------------------------------------------------------


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


def read_session(path: Path) -> list[Event]:
    """Read one session's lifecycle file, in file order.

    An absent or empty file yields no events.  The single tolerated damage is an
    interrupted write: a FINAL line with no terminating newline that does not
    parse as JSON is dropped silently, because that is what a process killed
    mid-append leaves behind.  Any other unreadable line raises
    :class:`LifecycleError` naming the file and the 1-based line number.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (IsADirectoryError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"{path}: cannot be read: {exc}") from exc
    if not text:
        return []

    lines = text.split("\n")
    complete_tail = text.endswith("\n")
    if complete_tail:
        lines.pop()  # the empty string after the final newline

    events: list[Event] = []
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        interrupted = (
            lineno == len(lines) and not complete_tail and not _is_json(stripped)
        )
        if interrupted:
            break
        events.append(parse_event(stripped, path=path, lineno=lineno))
    return events


def _needs_separator(path: Path) -> bool:
    """True when *path* ends mid-line, so an append must lead with a newline."""
    try:
        with open(path, "rb") as handle:  # a read: the chokepoint polices writes
            if handle.seek(0, os.SEEK_END) == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


def append_event(lifecycle_dir: Path, event: EventT) -> EventT:
    """Append *event* to its session's file and return the copy as written.

    The event is stamped with an ``id`` and an ``at`` when it carries neither,
    and validated BEFORE anything is written, so an invalid event leaves the
    file exactly as it was -- uncreated, if it did not exist.  The caller's own
    object is never modified; the stamped copy is the return value.

    Prior content is never read back and rewritten, so a concurrent writer
    cannot be clobbered.  The one thing read first is the file's final byte:
    when it is not a newline -- an interrupted write, a hand edit -- a
    separating newline leads the append so the new event starts its own line
    instead of being concatenated onto the damaged one.  The damaged line stays
    damaged; :func:`read_session` will name it.
    """
    stamped = replace(
        event,
        id=event.id or new_event_id(),
        at=event.at or now_timestamp(),
    )
    path = session_file(lifecycle_dir, stamped.session)
    line = event_to_json(stamped)
    _validate_line(line, path=path, lineno=None)

    effects.mkdir(path.parent, parents=True, exist_ok=True)
    lead = "\n" if _needs_separator(path) else ""
    with effects.open_write(path, "a", encoding="utf-8") as handle:
        handle.write(f"{lead}{line}\n")
    return stamped


# ---------------------------------------------------------------------------
# The derived picture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionLifecycle:
    """What one session's whole file says, read latest-wins.

    ``ended`` is the session's CURRENT end, not merely the newest ``ended``
    line: a session that exited and then started again has an ``ended`` older
    than its newest ``started``, which belongs to the previous run and is
    dropped here -- otherwise a resumed session would read as dead.  ``mark``
    is likewise the mark in force: the newest ``mark`` line governs, and a null
    ``state`` on it means the user cleared the mark.
    """

    session: str
    started: StartedEvent | None
    ended: EndedEvent | None
    name: NamedEvent | None
    mark: MarkEvent | None
    last_at: str


def summarize(events: Sequence[Event], *, session: str) -> SessionLifecycle:
    """Reduce one session's events to the picture a reader uses.

    Events are ordered by ``at`` -- lexically, which is time order for these
    fixed-width UTC timestamps -- and a tie is broken by position in the file,
    the later line winning.
    """
    started: StartedEvent | None = None
    started_key = ("", -1)
    ended: EndedEvent | None = None
    ended_key = ("", -1)
    name: NamedEvent | None = None
    name_key = ("", -1)
    marked: MarkEvent | None = None
    mark_key = ("", -1)
    last_at = ""

    for index, event in enumerate(events):
        key = (event.at, index)
        last_at = max(last_at, event.at)
        if isinstance(event, StartedEvent) and key > started_key:
            started, started_key = event, key
        elif isinstance(event, EndedEvent) and key > ended_key:
            ended, ended_key = event, key
        elif isinstance(event, NamedEvent) and key > name_key:
            name, name_key = event, key
        elif isinstance(event, MarkEvent) and key > mark_key:
            marked, mark_key = event, key

    if ended is not None and started is not None and ended_key < started_key:
        ended = None
    if marked is not None and marked.state is None:
        marked = None

    return SessionLifecycle(
        session=session,
        started=started,
        ended=ended,
        name=name,
        mark=marked,
        last_at=last_at,
    )


def load_all(lifecycle_dir: Path) -> dict[str, SessionLifecycle]:
    """Summarize every session file in *lifecycle_dir*, keyed by session uuid.

    An absent directory yields nothing.  Only ``*.jsonl`` files are read -- a
    ``notes.txt`` someone dropped in is not this store's business -- but a
    ``*.jsonl`` whose stem is not a session uuid IS: nothing but this module
    writes here, so such a file is either damage or a misunderstanding, and
    reading the store as if it were not there would hide it.
    """
    if not lifecycle_dir.is_dir():
        return {}
    loaded: dict[str, SessionLifecycle] = {}
    for path in sorted(lifecycle_dir.glob("*.jsonl")):
        session = path.stem
        if not SESSION_UUID_RE.match(session):
            raise LifecycleError(
                f"{path}: file name is not a session uuid; the lifecycle store "
                "holds one <session-uuid>.jsonl per session and nothing else"
            )
        loaded[session] = summarize(read_session(path), session=session)
    return loaded


def sweep_crashed(
    lifecycle_dir: Path,
    lifecycles: Mapping[str, SessionLifecycle],
    *,
    live_sessions: Set[str],
    now_ms: int,
) -> list[EndedEvent]:
    """Record an ``ended`` for every session that died without writing one.

    A session qualifies when it has a ``started``, has no current ``ended``, is
    not among *live_sessions*, and started longer than
    :data:`SWEEP_GRACE_MS` ago.  The grace period is the whole reason a sweep
    can be trusted: a session writes its ``started`` line from a SessionStart
    hook, before Claude Code has registered its process, so a session launched
    a moment ago looks exactly like one that crashed.

    Returns the events as written, in session order.  Idempotent: the next
    sweep reads the ``ended`` this one appended and passes the session by.
    """
    cutoff = now_ms - SWEEP_GRACE_MS
    written: list[EndedEvent] = []
    for session in sorted(lifecycles):
        state = lifecycles[session]
        if state.started is None or state.ended is not None:
            continue
        if session in live_sessions:
            continue
        if parse_timestamp_ms(state.started.at) >= cutoff:
            continue
        written.append(
            append_event(
                lifecycle_dir,
                EndedEvent(
                    session=session,
                    source="sweep",
                    outcome="crashed",
                    reason=None,
                    detail="no live process and no SessionEnd recorded",
                ),
            )
        )
    return written


def capture_name(
    lifecycle_dir: Path,
    lifecycle: SessionLifecycle | None,
    *,
    session: str,
    name: str | None,
    name_source: str | None,
) -> NamedEvent | None:
    """Record *name* for *session* when it is new or has changed.

    Claude Code holds a session's display name in its registry, which vanishes
    with the process; this copies it into the lifecycle store while it can still
    be read.  Returns the event written, or ``None`` when there was nothing to
    record -- no name at all, or the same name from the same source as the last
    time.
    """
    if not name:
        return None
    if (
        lifecycle is not None
        and lifecycle.name is not None
        and lifecycle.name.name == name
        and lifecycle.name.name_source == name_source
    ):
        return None
    return append_event(
        lifecycle_dir,
        NamedEvent(
            session=session,
            source="sweep",
            name=name,
            name_source=name_source,
        ),
    )


def derive_state(
    lifecycle: SessionLifecycle | None,
    *,
    live: bool,
    verified: bool,
    status: str | None,
    registry_present: bool,
    now_ms: int,
) -> str:
    """Decide the one state a session is shown in.  First rule that matches wins.

    The two inputs are deliberately separate: *live*, *verified*, *status* and
    *registry_present* are what can be observed about the process RIGHT NOW
    (from Claude Code's registry, checked against the kernel), and *lifecycle*
    is what the store recorded.  Observation beats the record, because a live
    process is a fact no recorded line can contradict -- including a ``done``
    mark on a session that turns out to still be running.

    1. live but unverified -- the process exists, but its kernel start token
       could not be checked, so its identity is unproven.
    2. live -- the registry's own status, or ``running`` when it says nothing
       this module recognizes.
    3. a mark in force -- the user's own word about a session nothing is
       running.
    4. a recorded end -- its outcome, ``exited`` or ``crashed``.
    5. a registry file with no process behind it -- it died without a
       SessionEnd, so ``crashed``.
    6. a ``started`` with no end -- ``starting`` inside the
       :data:`SWEEP_GRACE_MS` window, ``crashed`` after it.
    7. nothing says it is alive -- ``exited``.
    """
    if live and not verified:
        return "unverified"
    if live:
        return _LIVE_STATUS_STATES.get(status or "", "running")
    if lifecycle is not None and lifecycle.mark is not None:
        marked = lifecycle.mark.state
        # summarize() drops a mark whose state is null, so this is never None.
        assert marked is not None
        return marked
    if lifecycle is not None and lifecycle.ended is not None:
        return lifecycle.ended.outcome
    if registry_present:
        return "crashed"
    if lifecycle is not None and lifecycle.started is not None:
        started_ms = parse_timestamp_ms(lifecycle.started.at)
        if started_ms >= now_ms - SWEEP_GRACE_MS:
            return "starting"
        return "crashed"
    return "exited"
