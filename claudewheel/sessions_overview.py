"""Every Claude Code session registered under a profile, on one scrolling screen.

The overview is the read side of the same list the deletion checklist ticks:
one block per registry record, the focused one expanded, the whole column
scrolled by :func:`claudewheel.vertical_viewport.compute_viewport`.  It differs
from the checklist in what a key does -- nothing here signals a process -- and
in what it lists: **every parseable record**, live or not, because a record
whose process is gone is exactly what pruning is for.

Snapshot, never a poll
----------------------

The registry is read once when the screen opens and again only when the user
presses the refresh key.  Nothing re-reads it on a keystroke, on a timer or on
a resize, so the rows, the memory figures and the clock that uptimes are
measured against all belong to one moment the user chose.  A screen that
re-read itself under the cursor would renumber rows while someone was moving
through them, and an uptime that ticked would make an unchanged screen look
like it was tracking something it is not.

The cost of that choice is that the screen ages, and it says so: what it draws
is what was true at the last refresh.  Anything acting on a row therefore
re-probes rather than trusting the snapshot -- see
:func:`claudewheel.session_registry.prune`, whose whole safety argument is that
it asks the kernel again at the moment it deletes.

Focus survives a refresh
------------------------

A refresh re-identifies the focused row by PID rather than keeping the index:
records come and go between snapshots, and an index would silently land the
focus on a different session.  When the focused PID is gone the index is
clamped into the new list, which is the ordinary "the row you were on no longer
exists" answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import processes
from . import session_registry
from .session_list import ListRow, build_frame, move_focus, render_frame
from .session_registry import SessionRecord
from .session_rows import SessionIdentity
from .terminal import Terminal
from .theme import ThemeColors
from .ui import screen_session

#: Re-read the registry.  The only thing that does -- there is no auto-refresh.
REFRESH_KEYS = frozenset({"r", "R"})

#: Leave the screen.
CLOSE_KEYS = frozenset({"ESC", "CTRL_C", "q", "Q"})

_HINT = "up/down: move   r: refresh   q/esc: close"

_EMPTY = "No sessions registered under this profile."


@dataclass(frozen=True)
class Snapshot:
    """One reading of the registry, and the clock it was read at.

    *now_ms* is stored rather than re-read per frame: every uptime on the
    screen is measured against the moment the rows were gathered, so an
    un-refreshed screen is internally consistent instead of half-live.
    """

    rows: tuple[ListRow, ...]
    now_ms: int


@dataclass(frozen=True)
class OverviewOutcome:
    """What the screen was showing when it closed.

    *focused* is the record under the cursor at that moment (None when the list
    was empty), and *refreshes* how many times the user re-read the registry.
    """

    focused: SessionRecord | None = None
    refreshes: int = 0


def take_snapshot(config_dir: Path, *, clock: Callable[[], int]) -> Snapshot:
    """Read the registry under *config_dir* into rows, once.

    Every parseable record is a row -- a dead one reads ``stale`` in its header
    and is what the prune key acts on.  Resident memory is measured in a single
    ``ps`` call, and only for the records that were live at this reading: asking
    about a dead PID could only measure whatever now wears its number.
    """
    records = session_registry.read_records(config_dir)
    memory = processes.resident_memory([r.pid for r in records if r.live])
    rows = tuple(
        ListRow(record=record, rss_kib=memory.get(record.pid)) for record in records
    )
    return Snapshot(rows=rows, now_ms=clock())


def refocus(previous: Sequence[ListRow], focus: int, rows: Sequence[ListRow]) -> int:
    """Where the focus belongs in *rows*, given it was on *previous*[*focus*].

    By PID, so a record that arrived or left between the two readings does not
    drag the focus onto a different session.  A focused PID that is no longer
    listed falls back to the clamped index, and an empty list has no focus.
    """
    if not rows:
        return -1
    if 0 <= focus < len(previous):
        pid = previous[focus].record.pid
        for index, row in enumerate(rows):
            if row.record.pid == pid:
                return index
    return max(0, min(len(rows) - 1, focus))


def run_overview(
    config_dir: Path,
    *,
    profile_name: str,
    theme: ThemeColors,
    terminal: Terminal,
    clock: Callable[[], int],
    identity: SessionIdentity | None = None,
) -> OverviewOutcome:
    """Show the sessions registered under *config_dir* until the user leaves.

    Up and down move the focus (clamped, never wrapping), the refresh key takes
    a new snapshot, and escape, ``q`` or Ctrl-C close the screen.  Every other
    key is ignored rather than doing something adjacent.

    The frame is rebuilt at the terminal's current size on every draw, so a
    window too short for the list -- or for the chrome around it -- draws the
    prefix that fits instead of raising or writing past its last row.
    """
    snapshot = take_snapshot(config_dir, clock=clock)
    focus = 0 if snapshot.rows else -1
    refreshes = 0
    title = f"Sessions under '{profile_name}'"

    def render() -> None:
        rows, cols = terminal.get_size()
        frame = build_frame(
            snapshot.rows,
            focus=focus,
            now_ms=snapshot.now_ms,
            title=title,
            hint=_HINT,
            height=rows,
            width=max(1, cols - 2),
            identity=identity,
            empty_text=_EMPTY,
        )
        render_frame(terminal, theme, frame)

    with screen_session(terminal, True, render):
        render()
        while True:
            try:
                key = terminal.read_key()
            except KeyboardInterrupt:
                break
            if key in CLOSE_KEYS:
                break
            if key == "DOWN":
                focus = move_focus(focus, len(snapshot.rows), 1)
            elif key == "UP":
                focus = move_focus(focus, len(snapshot.rows), -1)
            elif key in REFRESH_KEYS:
                fresh = take_snapshot(config_dir, clock=clock)
                focus = refocus(snapshot.rows, focus, fresh.rows)
                snapshot = fresh
                refreshes += 1
            render()

    focused = snapshot.rows[focus].record if 0 <= focus < len(snapshot.rows) else None
    return OverviewOutcome(focused=focused, refreshes=refreshes)
