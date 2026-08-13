"""Render one session registry record as a block of lines, collapsed or expanded.

Two screens show the same list of processes holding a profile: the sessions
overview, and the checklist deletion presents of everything holding the profile
it is about to remove.  They differ in what they *do* with a row, not in what a
row looks like, so the formatting lives here once: a
:class:`~claudewheel.session_registry.SessionRecord` in, a tuple of plain text
lines out, with no colour, no terminal and no clock of its own.

Block heights are fixed per state, because they are what
:func:`claudewheel.vertical_viewport.compute_viewport` scrolls over:

===================  ==========================================================
Collapsed            two lines -- a header and one summary line
Collapsed + state    three lines -- the state line is the checklist's indicator
Highlighted          five lines -- header, directory, identity, resources, and
                     the state line (blank when the row carries no state)
===================  ==========================================================

Nothing a record may be missing changes those counts: an absent directory,
version or start time changes what a line says, never how many there are.

The current session
-------------------

Claude Code exports its own identity into every process it starts, and the
registry file records the same two values.  ``CLAUDE_CODE_SESSION_ID`` is the
session's UUID (the record's ``sessionId``) and ``CLAUDE_PID`` is the process
that owns it (the record's ``pid``).  A row is marked as *this* session only
when **both** match: a session id alone would also mark a sibling process of
the same session, and a pid alone would mark whatever the kernel handed that
recycled number to.  If either variable is missing or unusable, no identity is
resolved and no row is marked -- claiming the wrong row is worse than claiming
none.

The environment is a parameter here, never read from the process: a caller
passes ``os.environ`` (or a fixture) to :func:`current_identity`, and the
resulting identity is passed down to :func:`format_row`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .session_registry import SessionRecord

#: The environment variable carrying Claude Code's own session UUID.
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"

#: The environment variable carrying the pid of the Claude Code process.
PID_ENV = "CLAUDE_PID"

#: What marks the row of the session the user is sitting in.
CURRENT_MARK = "*"

#: Separates the clauses inside one line.
_SEP = " · "


@dataclass(frozen=True)
class SessionIdentity:
    """Who the reader is: the session UUID and the pid that owns it."""

    session_id: str
    pid: int


def current_identity(env: Mapping[str, str]) -> SessionIdentity | None:
    """The identity *env* describes, or None when it does not describe one.

    Both values must be present and usable.  A missing variable, an empty
    string, a non-numeric pid and a pid that no process can have all mean "no
    identity", so nothing is marked rather than the wrong thing being marked.
    """
    session_id = env.get(SESSION_ID_ENV, "").strip()
    raw_pid = env.get(PID_ENV, "").strip()
    if not session_id or not raw_pid.isdigit():
        return None
    pid = int(raw_pid)
    if pid <= 0:
        return None
    return SessionIdentity(session_id=session_id, pid=pid)


def is_current(record: SessionRecord, identity: SessionIdentity | None) -> bool:
    """True when *record* is the session *identity* describes.

    Both values are compared exactly.  A record carrying no session id can
    never match, and neither can a match on one value alone.
    """
    if identity is None or record.session_id is None:
        return False
    return record.session_id == identity.session_id and record.pid == identity.pid


def format_uptime(started_at: int | None, now_ms: int) -> str:
    """How long a session started at *started_at* has been up, at *now_ms*.

    Both are wall-clock milliseconds, as the registry records them.  A record
    with no start time reads ``unknown``; a start time in the future (a clock
    stepped between the two readings) reads as no uptime rather than negative.
    """
    if started_at is None:
        return "unknown"
    seconds = max(0, (now_ms - started_at) // 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m"
    return f"{hours // 24}d {hours % 24}h"


def format_memory(rss_kib: int) -> str:
    """Resident memory, given in KiB as ``ps -o rss=`` reports it on both platforms."""
    if rss_kib < 0:
        raise ValueError(f"resident memory must not be negative: {rss_kib}")
    if rss_kib < 1024:
        return f"{rss_kib} KiB"
    if rss_kib < 1024 * 1024:
        return f"{rss_kib / 1024:.1f} MiB"
    return f"{rss_kib / (1024 * 1024):.1f} GiB"


def _header(record: SessionRecord, *, prefix: str, current: bool) -> str:
    """The first line: the toggle, the current mark, and what the session is."""
    mark = CURRENT_MARK if current else " " * len(CURRENT_MARK)
    parts = [record.name or "(unnamed)", record.kind, record.status or "unknown"]
    if not record.live:
        parts.append("stale")
    return f"{prefix}{mark} " + "  ".join(parts)


def format_row(
    record: SessionRecord,
    *,
    highlighted: bool,
    now_ms: int,
    identity: SessionIdentity | None = None,
    rss_kib: int | None = None,
    state: str | None = None,
    selector: str | None = None,
) -> tuple[str, ...]:
    """The lines of one row block, two, three or five of them.

    *highlighted* expands the block; *state* adds (collapsed) or fills
    (highlighted) the per-row state line the deletion checklist writes its
    running/stopped indicator into; *selector* is that checklist's ``[x]`` /
    ``[ ]`` toggle, drawn at the left edge with the rest of the block indented
    under it.  *rss_kib* is the resident memory a caller measured for the pid;
    omitted, no memory clause is written at all.  *identity* decides the
    current-session mark and is compared per :func:`is_current`.
    """
    prefix = f"{selector} " if selector else ""
    indent = " " * (len(prefix) + len(CURRENT_MARK) + 1)
    memory = format_memory(rss_kib) if rss_kib is not None else None
    uptime = format_uptime(record.started_at, now_ms)
    header = _header(record, prefix=prefix, current=is_current(record, identity))
    cwd = record.cwd or "(no directory)"

    if not highlighted:
        summary = [cwd, f"up {uptime}"]
        if memory is not None:
            summary.append(memory)
        lines = [header, indent + _SEP.join(summary)]
        if state is not None:
            lines.append(indent + state)
        return tuple(lines)

    version = f"v{record.version}" if record.version else "version unknown"
    identity_line = _SEP.join(
        [
            f"pid {record.pid}",
            f"session {record.session_id or 'unknown'}",
            version,
        ]
    )
    resources = [f"up {uptime}"]
    if memory is not None:
        resources.append(memory)
    return (
        header,
        indent + f"cwd {cwd}",
        indent + identity_line,
        indent + _SEP.join(resources),
        indent + state if state is not None else "",
    )
