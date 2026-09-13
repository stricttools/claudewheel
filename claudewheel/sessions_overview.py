"""Every Claude Code session on this machine, on one framed scrolling table.

The screen joins the two things that know about a session and neither of which
knows all of it:

* Claude Code's own per-process registry (:mod:`claudewheel.session_registry`),
  which is exact while a process lives and says nothing once it is gone -- and
  is per profile, so reading one profile's registry shows one profile's work;
* claudewheel's lifecycle store (:mod:`claudewheel.lifecycle`), which is
  machine-wide and outlives every process, but knows only what was recorded.

So the table is gathered across EVERY profile the workspace discovers (the
vanilla ``default`` profile included) and every session the lifecycle store has
a file for, and each row's state is :func:`claudewheel.lifecycle.derive_state`
over both answers, with observation beating the record.

Gathering writes
----------------

Opening the screen is not a read-only act, and deliberately so. Two writes
happen while gathering, both idempotent and both through the lifecycle store's
own doors:

* :func:`claudewheel.lifecycle.sweep_crashed` records an ``ended`` for a session
  that has a ``started``, no end, no live process and is past the grace period.
  Nothing else would ever notice that session died.
* :func:`claudewheel.lifecycle.capture_name` copies a live session's display
  name out of the registry, which is the only place it exists, into the store
  that outlives it.

Snapshot, never a poll
----------------------

The gather happens when the screen opens and again only when the user asks --
the refresh key, or any key that changed the world (a prune, a mark). Uptimes
are measured against the clock of that gather, so an untouched screen is
internally consistent instead of half-live, and nothing renumbers rows under a
cursor someone is moving through.

Drawing
-------

:mod:`claudewheel.sessions_table` owns the whole layout and emits styled spans;
this module is the only place a style name becomes an escape sequence, and every
colour it uses comes from the theme's ``sessions`` section.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import lifecycle
from . import processes
from . import session_registry
from . import sessions_table
from .constants import BOLD, CLEAR_SCREEN, DIM, RESET, move_to
from .lifecycle import MarkEvent, SessionLifecycle
from .session_list import move_focus
from .session_registry import SessionRecord
from .session_rows import SessionIdentity, is_current
from .sessions_table import Frame, SessionRow
from .terminal import Terminal
from .theme import ThemeColors
from .ui import screen_session
from .workspace import Workspace

#: Re-gather. The only key that does -- there is no auto-refresh.
REFRESH_KEYS = frozenset({"r", "R"})

#: Delete the registry files of the listed records that are provably dead.
PRUNE_KEYS = frozenset({"p", "P"})

#: Show the states hidden by default (what is finished) as well.
SHOW_ALL_KEYS = frozenset({"a", "A"})

#: Enter mark mode, where one more key marks the focused session.
MARK_KEYS = frozenset({"m", "M"})

#: Leave the screen.
CLOSE_KEYS = frozenset({"ESC", "CTRL_C", "q", "Q"})

#: What a key means in mark mode. ``None`` clears the mark in force.
MARK_STATES: dict[str, str | None] = {
    "h": "on-hold",
    "b": "blocked",
    "d": "done",
    "c": None,
}

#: How far a left/right key scrolls the column strip.
HSCROLL_STEP = 8

#: The states drawn dim: a session deliberately parked behind something else,
#: and one that is simply over.
DIM_STATES = frozenset({"blocked", "exited"})

#: The one state drawn bold: a session sitting at a prompt for the user.
BOLD_STATES = frozenset({"waiting"})

_HINT_DEFAULT = (
    "↑↓ move  ←→ scroll  enter: details  m: mark  "
    "a: show all  p: prune crashed  r: refresh  q: close"
)
_HINT_SHOW_ALL = (
    "↑↓ move  ←→ scroll  enter: details  m: mark  "
    "a: loose ends  p: prune crashed  r: refresh  q: close"
)
_HINT_MARK = "mark: h on-hold  b blocked  d done  c clear  esc cancel"

#: What a row with no name anywhere reads as.
UNNAMED = "(unnamed)"


@dataclass(frozen=True)
class OverviewOutcome:
    """What the screen changed while it was open.

    *pruned* is every registry record whose file it deleted, *marked* how many
    mark events the user wrote, and *swept* how many ``ended`` events the
    gathering passes recorded for sessions that died without one.
    """

    pruned: tuple[SessionRecord, ...] = ()
    marked: int = 0
    swept: int = 0


def style_sequence(theme: ThemeColors, style: str) -> str:
    """The escape sequence *style* is drawn in, under *theme*.

    The only place a style name from :mod:`claudewheel.sessions_table` becomes
    colour. ``BOLD`` and ``DIM`` are the two attributes applied directly: they
    say "this one wants you" and "this one is over" on top of whatever hue the
    theme gave the state, which no single colour can do.
    """
    kind, _, state = style.partition(":")
    if kind in ("state", "state_focus"):
        sequence = theme.sessions_state_fg.get(state, "")
        if state in BOLD_STATES:
            sequence = BOLD + sequence
        elif state in DIM_STATES:
            sequence = DIM + sequence
        if kind == "state_focus":
            sequence = theme.sessions_focus_bg + sequence
        return sequence
    return {
        sessions_table.STYLE_FRAME: theme.sessions_frame_fg,
        sessions_table.STYLE_HEADER: BOLD + theme.sessions_header_fg,
        sessions_table.STYLE_ROW: theme.sessions_row_fg,
        sessions_table.STYLE_ROW_FOCUS: (
            theme.sessions_focus_bg + theme.sessions_focus_fg
        ),
        sessions_table.STYLE_DETAIL: theme.sessions_detail_fg,
        sessions_table.STYLE_EMPTY: theme.sessions_detail_fg,
    }.get(style, theme.sessions_row_fg)


def draw(
    terminal: Terminal,
    theme: ThemeColors,
    frame: Frame,
    *,
    footer: str,
    message: bool,
    rows: int,
    cols: int,
) -> None:
    """Draw *frame* over a cleared screen, with *footer* on the last row.

    The footer is clipped one column short of the terminal's width: a line that
    filled the last cell of the last row would leave the cursor in a pending
    wrap, and the next write would scroll the screen the frame was just drawn
    onto.
    """
    buf: list[str] = [CLEAR_SCREEN]
    for index, line in enumerate(frame.lines):
        buf.append(move_to(index + 1, 1))
        for span in line:
            buf.append(style_sequence(theme, span.style) + span.text)
        buf.append(RESET)
    if rows > 0 and cols > 1:
        colour = theme.sessions_message_fg if message else theme.sessions_hint_fg
        buf.append(move_to(rows, 1) + colour + footer[: cols - 1] + RESET)
    terminal.write("".join(buf))


def _verified(record: SessionRecord) -> bool:
    """Whether *record*'s process identity could actually be checked.

    Both halves of the phantom filter must have answered: the record carries a
    kernel start token, and the kernel still offers one for that pid. Where
    either is missing the process may be the recorded one or may be whatever
    took over its number, and the row says so rather than picking.
    """
    return (
        record.live
        and session_registry.recorded_token(record.proc_start) is not None
        and session_registry.process_start_token(record.pid) is not None
    )


def _recordable(session: str | None) -> bool:
    """True when *session* can be written to the lifecycle store.

    A lifecycle file is named after its session, so a registry record carrying
    something that is not a session uuid is read but never written about.
    """
    return session is not None and bool(lifecycle.SESSION_UUID_RE.match(session))


def _kind_label(kind: str) -> str:
    return sessions_table.KIND_LABELS.get(kind, kind)


def _row_for_record(
    record: SessionRecord,
    life: SessionLifecycle | None,
    *,
    profile: str,
    config_dir: Path,
    rss_kib: int | None,
    identity: SessionIdentity | None,
    now_ms: int,
) -> SessionRow:
    """One table row from a registry record, filled out from its lifecycle.

    The registry is the authority on everything it carries; the lifecycle
    supplies what a registry file has never held -- the model and the transcript
    path -- and stands in for a name the record lost.
    """
    started = life.started if life is not None else None
    named = life.name if life is not None else None
    state = lifecycle.derive_state(
        life,
        live=record.live,
        verified=_verified(record),
        status=record.status,
        registry_present=True,
        now_ms=now_ms,
    )
    started_ms = record.started_at
    if started_ms is None and started is not None:
        started_ms = lifecycle.parse_timestamp_ms(started.at)
    return SessionRow(
        session=record.session_id,
        name=record.name or (named.name if named is not None else None) or UNNAMED,
        name_source=named.name_source if named is not None else None,
        state=state,
        kind=_kind_label(record.kind),
        cwd=record.cwd or (started.cwd if started is not None else None),
        profile=profile,
        version=record.version
        or (started.claude_version if started is not None else None),
        model=started.model if started is not None else None,
        started_ms=started_ms,
        rss_kib=rss_kib,
        pid=record.pid,
        current=is_current(record, identity),
        config_dir=str(config_dir),
        transcript=started.transcript if started is not None else None,
        record=record,
        lifecycle=life,
    )


def _row_for_lifecycle(
    life: SessionLifecycle,
    *,
    profiles: dict[str, str],
    now_ms: int,
) -> SessionRow:
    """One table row for a session no registry record answers for.

    Its process is gone (or was never registered under a profile this workspace
    knows), so everything comes from what the store recorded, and the Kind cell
    says so: nothing ever wrote down what kind of session it was.
    """
    started = life.started
    config_dir = started.config_dir if started is not None else None
    profile = started.profile if started is not None else None
    if profile is None and config_dir is not None:
        profile = profiles.get(config_dir) or Path(config_dir).name
    return SessionRow(
        session=life.session,
        name=life.name.name if life.name is not None else UNNAMED,
        name_source=life.name.name_source if life.name is not None else None,
        state=lifecycle.derive_state(
            life,
            live=False,
            verified=False,
            status=None,
            registry_present=False,
            now_ms=now_ms,
        ),
        kind=sessions_table.KIND_UNKNOWN,
        cwd=started.cwd if started is not None else None,
        profile=profile,
        version=started.claude_version if started is not None else None,
        model=started.model if started is not None else None,
        started_ms=(
            lifecycle.parse_timestamp_ms(started.at) if started is not None else None
        ),
        rss_kib=None,
        pid=started.pid if started is not None else None,
        current=False,
        config_dir=config_dir,
        transcript=started.transcript if started is not None else None,
        record=None,
        lifecycle=life,
    )


def gather_rows(
    workspace: Workspace, *, now_ms: int, identity: SessionIdentity | None
) -> tuple[list[SessionRow], int, int]:
    """Read the whole machine into sorted rows; return them with what was written.

    The two counts are the two writes the pass performs: how many crashed
    sessions it recorded an end for, and how many live names it copied into the
    lifecycle store. When either wrote something the store is read again, so the
    rows show the file as it now stands rather than as it was a moment before.
    """
    lifecycle_dir = workspace.shared.lifecycle_dir
    lifecycles = lifecycle.load_all(lifecycle_dir)

    found: list[tuple[str, Path, SessionRecord]] = []
    profiles: dict[str, str] = {}
    for profile in workspace.profiles.enumerate():
        config_dir = workspace.profiles.path_for(profile.name)
        profiles[str(config_dir)] = profile.name
        for record in session_registry.read_records(config_dir):
            found.append((profile.name, config_dir, record))

    live_sessions = {
        record.session_id
        for _, _, record in found
        if record.live and record.session_id is not None
    }
    swept = lifecycle.sweep_crashed(
        lifecycle_dir, lifecycles, live_sessions=live_sessions, now_ms=now_ms
    )
    named = 0
    for _, _, record in found:
        if not record.live or not _recordable(record.session_id):
            continue
        assert record.session_id is not None  # _recordable said so
        event = lifecycle.capture_name(
            lifecycle_dir,
            lifecycles.get(record.session_id),
            session=record.session_id,
            name=record.name,
            name_source=None,
        )
        if event is not None:
            named += 1
    if swept or named:
        lifecycles = lifecycle.load_all(lifecycle_dir)

    memory = processes.resident_memory(
        [record.pid for _, _, record in found if record.live]
    )

    rows: list[SessionRow] = []
    registered: set[str] = set()
    for profile_name, config_dir, record in found:
        if record.session_id is not None:
            registered.add(record.session_id)
        rows.append(
            _row_for_record(
                record,
                lifecycles.get(record.session_id or ""),
                profile=profile_name,
                config_dir=config_dir,
                rss_kib=memory.get(record.pid) if record.live else None,
                identity=identity,
                now_ms=now_ms,
            )
        )
    for session, life in lifecycles.items():
        if session in registered:
            continue
        rows.append(_row_for_lifecycle(life, profiles=profiles, now_ms=now_ms))

    return sessions_table.sort_rows(rows), len(swept), named


def _crashed_records(rows: Iterable[SessionRow]) -> list[SessionRecord]:
    """The registry files the prune key offers up, from the rows on screen."""
    return [
        row.record for row in rows if row.state == "crashed" and row.record is not None
    ]


def _refocus(rows: Sequence[SessionRow], session: str | None, focus: int) -> int:
    """Where the focus belongs after a re-gather, given what it was on.

    By session id rather than index: rows come and go between gathers, and an
    index would silently land the focus on a different session. A session that
    is no longer listed falls back to the clamped index.
    """
    if not rows:
        return -1
    if session is not None:
        for index, row in enumerate(rows):
            if row.session == session:
                return index
    return max(0, min(len(rows) - 1, focus))


def run_overview(
    workspace: Workspace,
    *,
    theme: ThemeColors,
    terminal: Terminal,
    clock: Callable[[], int],
    identity: SessionIdentity | None,
    home: str,
) -> OverviewOutcome:
    """Show the machine's sessions until the user leaves, and report what changed.

    Up and down move the focus, page up and down move it by a window, home and
    end jump to the ends, left and right scroll the columns, Enter expands the
    focused row into its details, ``a`` reveals what is finished, ``p`` prunes
    the registry files of the rows that crashed, ``r`` gathers again and ``m``
    enters mark mode, where one more key records the user's own word about the
    session (on hold, blocked, done, or cleared). Every other key is ignored
    rather than doing something adjacent.

    The frame is rebuilt at the terminal's current size on every draw, so a
    resize reflows it and a window too small for the table draws the prefix that
    fits instead of raising.
    """
    now = clock()
    rows, swept, _named = gather_rows(workspace, now_ms=now, identity=identity)
    focus = 0
    expanded: int | None = None
    hscroll = 0
    show_all = False
    mark_mode = False
    message: str | None = None
    pruned: list[SessionRecord] = []
    marked = 0
    window = 1

    def visible() -> list[SessionRow]:
        return sessions_table.visible_rows(rows, show_all)

    def focused() -> SessionRow | None:
        shown = visible()
        return shown[focus] if 0 <= focus < len(shown) else None

    def render() -> None:
        nonlocal hscroll, window
        term_rows, term_cols = terminal.get_size()
        frame = sessions_table.layout(
            rows,
            focus=focus,
            expanded=expanded,
            height=term_rows,
            width=term_cols,
            hscroll=hscroll,
            now_ms=now,
            home=home,
            show_all=show_all,
        )
        hscroll = frame.hscroll
        window = max(1, frame.window)
        if mark_mode:
            hint = _HINT_MARK
        else:
            hint = _HINT_SHOW_ALL if show_all else _HINT_DEFAULT
        draw(
            terminal,
            theme,
            frame,
            footer=message or hint,
            message=message is not None,
            rows=term_rows,
            cols=term_cols,
        )

    def regather() -> None:
        nonlocal rows, focus, now, expanded, swept
        keep = focused()
        now = clock()
        rows, swept_now, _captured = gather_rows(
            workspace, now_ms=now, identity=identity
        )
        swept += swept_now
        focus = _refocus(visible(), keep.session if keep is not None else None, focus)
        if expanded is not None:
            expanded = focus if focus >= 0 else None

    with screen_session(terminal, True, render):
        render()
        while True:
            try:
                key = terminal.read_key()
            except KeyboardInterrupt:
                break
            message = None
            count = len(visible())

            if mark_mode:
                mark_mode = False
                row = focused()
                if key in MARK_STATES and row is not None and row.session is not None:
                    state = MARK_STATES[key]
                    lifecycle.append_event(
                        workspace.shared.lifecycle_dir,
                        MarkEvent(
                            session=row.session,
                            source="user",
                            state=state,
                            note=None,
                        ),
                    )
                    marked += 1
                    name = row.name
                    message = (
                        f"Marked {name} {state}"
                        if state is not None
                        else f"Cleared mark on {name}"
                    )
                    regather()
                render()
                continue

            if key in CLOSE_KEYS:
                break
            if key == "DOWN":
                focus = move_focus(focus, count, 1)
            elif key == "UP":
                focus = move_focus(focus, count, -1)
            elif key == "PGDN":
                focus = move_focus(focus, count, max(1, window - 1))
            elif key == "PGUP":
                focus = move_focus(focus, count, -max(1, window - 1))
            elif key == "HOME":
                focus = move_focus(focus, count, -count)
            elif key == "END":
                focus = move_focus(focus, count, count)
            elif key == "LEFT":
                hscroll = max(0, hscroll - HSCROLL_STEP)
            elif key == "RIGHT":
                hscroll += HSCROLL_STEP
            elif key == "ENTER":
                expanded = None if expanded == focus else focus
            elif key in SHOW_ALL_KEYS:
                keep = focused()
                show_all = not show_all
                focus = _refocus(
                    visible(), keep.session if keep is not None else None, focus
                )
                expanded = None
            elif key in REFRESH_KEYS:
                regather()
            elif key in PRUNE_KEYS:
                gone = session_registry.prune(_crashed_records(visible()))
                pruned.extend(gone)
                regather()
                message = f"Pruned {len(gone)} crashed record(s)"
            elif key in MARK_KEYS:
                row = focused()
                if row is None or row.session is None:
                    message = "No session id; cannot mark"
                else:
                    mark_mode = True
            render()

    return OverviewOutcome(pruned=tuple(pruned), marked=marked, swept=swept)
