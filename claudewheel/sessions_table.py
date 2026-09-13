"""Lay every Claude Code session on this machine out as one framed table.

This module is the looking, and nothing else: rows, dimensions, a clock, a home
directory and two view flags in, a :class:`Frame` of styled :class:`Span`\\ s
out.  It opens no file, reads no environment and knows no terminal, so the whole
layout -- the column widths, both truncation directions, both scrollbars, the
expanded row's detail lines -- is exercised as arithmetic over strings.

What the frame is made of
-------------------------

The bottom terminal row belongs to the hint line, which is the screen's
business, not this module's; the frame occupies everything above it:

=================  ============================================================
Top border         ``╭``, the column joints, ``╮``
Header             the column labels
Header rule        ``├``, the column joints, ``┤``
Data window        one line per row, four for the expanded one
Bottom border      ``╰``, the column joints, ``╯``, and the horizontal handle
=================  ============================================================

Two scrollbars, each drawn INTO a border rather than beside it: the vertical one
over the right border of the data-window lines, the horizontal one over the
interior of the bottom border.  Neither costs a column or a line, which is why
the table can afford eight columns on an 80-column terminal.

The column strip and the two things that are not in it
------------------------------------------------------

Every cell line -- the header, the joint lines, each row -- is one *strip* as
wide as the columns ask for (:attr:`Frame.strip_width`), shifted left by
``hscroll`` and clipped to the frame's interior.  The expanded row's detail
lines are deliberately NOT part of that strip: they are full-width prose about
one session, and scrolling them sideways with the columns would hide the
beginning of a path rather than reveal the end of a column.

Styles, not colours
-------------------

A span carries a style NAME (``frame``, ``header``, ``row``, ``row_focus``,
``state:<state>``, ``state_focus:<state>``, ``detail``, ``empty``) and
:mod:`claudewheel.sessions_overview` is the only place one becomes an escape
sequence.  The focused row's every span -- including the padding out to the
frame's edge -- carries a focus style, so the highlight is a full-width band
rather than a coloured word.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .lifecycle import (
    HIDDEN_BY_DEFAULT_STATES,
    LIVE_STATES,
    STATES,
    SessionLifecycle,
)
from .session_registry import SessionRecord
from .session_rows import format_uptime
from .vertical_viewport import compute_viewport

#: What the registry's ``kind`` reads as in the Kind column.
KIND_LABELS = {
    "interactive": "interactive",
    "bg": "background",
    "daemon": "daemon",
    "daemon-worker": "worker",
}

#: The Kind cell of a session only the lifecycle store knows about: no registry
#: record, so nothing ever said what kind of session it was.
KIND_UNKNOWN = "-"

#: What an absent value reads as in every column.
MISSING = "-"

#: The box-drawing pieces.  Two handles and two tracks: the vertical scrollbar
#: replaces the right border's ``│`` with ``┃``, the horizontal one the bottom
#: border's ``─`` and ``┴`` with ``━``.
TOP_LEFT = "╭"
TOP_RIGHT = "╮"
BOTTOM_LEFT = "╰"
BOTTOM_RIGHT = "╯"
RULE_LEFT = "├"
RULE_RIGHT = "┤"
HORIZONTAL = "─"
VERTICAL = "│"
JOINT_TOP = "┬"
JOINT_RULE = "┼"
JOINT_BOTTOM = "┴"
VERTICAL_HANDLE = "┃"
HORIZONTAL_HANDLE = "━"

#: The ellipsis a truncated cell carries, at the end or at the start.
ELLIPSIS = "…"

#: Style names, resolved to escape sequences by the screen that draws them.
STYLE_FRAME = "frame"
STYLE_HEADER = "header"
STYLE_ROW = "row"
STYLE_ROW_FOCUS = "row_focus"
STYLE_DETAIL = "detail"
STYLE_EMPTY = "empty"

#: What an empty table says, in the data window, inside the frame.
EMPTY_TEXT = "No sessions."

#: Lines of the frame that are not the data window: the two borders, the header
#: and the header rule.
CHROME_LINES = 4

#: How many lines an expanded row occupies: its own, plus three of detail.
EXPANDED_HEIGHT = 4

#: What separates the clauses of a detail line, and what indents them.
DETAIL_SEP = " · "
DETAIL_INDENT = "  "


def state_style(state: str, *, focused: bool) -> str:
    """The style name of a State cell in *state*."""
    return f"state_focus:{state}" if focused else f"state:{state}"


@dataclass(frozen=True)
class SessionRow:
    """One session, as the table needs it: already joined, already resolved.

    Everything here is a value the gatherer decided -- the state it derived, the
    profile it attributed a lifecycle-only session to, the memory it measured --
    so the layout never has to ask anything about the world.  ``record`` and
    ``lifecycle`` are carried for the screen's own keys (pruning needs the
    record, marking needs the session id), not for the layout.
    """

    session: str | None
    name: str
    name_source: str | None
    state: str
    kind: str
    cwd: str | None
    profile: str | None
    version: str | None
    model: str | None
    started_ms: int | None
    rss_kib: int | None
    pid: int | None
    current: bool
    config_dir: str | None
    transcript: str | None
    record: SessionRecord | None
    lifecycle: SessionLifecycle | None


@dataclass(frozen=True)
class Span:
    """A run of characters drawn in one style."""

    text: str
    style: str


#: One drawn line: its spans, left to right, totalling the frame's width.
Line = tuple[Span, ...]


@dataclass(frozen=True)
class Frame:
    """A laid-out table, and the dimensions the key loop needs back.

    ``hscroll`` is the CLAMPED horizontal offset, so a screen that asked for
    more than the strip can offer reads the real one back instead of
    accumulating a number that stopped meaning anything.  ``window`` is the
    data window in lines (what a page key moves by) and ``total_lines`` the
    summed height of every visible row.
    """

    lines: tuple[Line, ...]
    window: int
    total_lines: int
    strip_width: int
    interior_width: int
    hscroll: int


@dataclass(frozen=True)
class _Spec:
    """One column: its label, how wide it gets, and how it handles overflow.

    ``fixed`` is a width the content cannot change.  Otherwise the column is
    *natural*: as wide as its longest cell (never narrower than its own label),
    clamped between ``minimum`` and ``maximum``.
    """

    label: str
    fixed: int | None = None
    minimum: int = 0
    maximum: int = 0
    align_right: bool = False
    truncate_left: bool = False


#: The columns, in order.  State is as wide as the longest state name, so the
#: column cannot be outgrown by a state added to the lifecycle model.
SPECS: tuple[_Spec, ...] = (
    _Spec("Name", minimum=8, maximum=40),
    _Spec("State", fixed=max(len(state) for state in STATES)),
    _Spec("Kind", fixed=11),
    _Spec("Directory", minimum=10, maximum=40, truncate_left=True),
    _Spec("Version", fixed=7),
    _Spec("Model", maximum=24),
    _Spec("Started", fixed=10),
    _Spec("MiB", fixed=5, align_right=True),
)

#: Which column carries the state, and therefore its own colour.
STATE_COLUMN = 1

#: The order the state groups are listed in: running sessions, then the things
#: wanting attention, then what is over.
_GROUPS: tuple[frozenset[str], ...] = (
    LIVE_STATES,
    frozenset({"starting"}),
    frozenset({"on-hold", "blocked"}),
    frozenset({"crashed"}),
    frozenset({"done"}),
    frozenset({"exited"}),
)


def _group_of(state: str) -> int:
    for index, group in enumerate(_GROUPS):
        if state in group:
            return index
    return len(_GROUPS)


def sort_rows(rows: Sequence[SessionRow]) -> list[SessionRow]:
    """Group *rows* by state, newest first inside each group.

    A row with no start time sorts after every dated row of its group rather
    than among them, and rows that cannot be told apart by age sort by name, so
    the order is total and a redraw never reshuffles equals.
    """
    return sorted(
        rows,
        key=lambda row: (
            _group_of(row.state),
            row.started_ms is None,
            -(row.started_ms or 0),
            row.name,
        ),
    )


def visible_rows(rows: Sequence[SessionRow], show_all: bool) -> list[SessionRow]:
    """The rows shown at this filter setting, in the order given.

    The default view hides what is finished -- an exited session and one the
    user marked done -- because those accumulate without end and are not what
    the screen is opened to look at.
    """
    if show_all:
        return list(rows)
    return [row for row in rows if row.state not in HIDDEN_BY_DEFAULT_STATES]


def tildify(path: str | None, home: str) -> str:
    """*path* with *home* written as ``~``, or :data:`MISSING` when absent.

    *home* is a parameter, never read from the environment: this module has no
    business knowing whose machine it is drawing.
    """
    if not path:
        return MISSING
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home) :]
    return path


def _truncate(text: str, width: int, *, from_left: bool) -> str:
    """*text* cut to *width*, with an ellipsis where the cut was made."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS
    if from_left:
        return ELLIPSIS + text[len(text) - (width - 1) :]
    return text[: width - 1] + ELLIPSIS


def _mib(rss_kib: int | None) -> str:
    return MISSING if rss_kib is None else str(round(rss_kib / 1024))


def _started(started_ms: int | None, now_ms: int) -> str:
    if started_ms is None:
        return "unknown"
    return format_uptime(started_ms, now_ms) + " ago"


def cells(row: SessionRow, *, now_ms: int, home: str) -> tuple[str, ...]:
    """The eight untruncated cell texts of *row*, in column order."""
    name = f"* {row.name}" if row.current else row.name
    return (
        name,
        row.state,
        row.kind,
        tildify(row.cwd, home),
        row.version or MISSING,
        row.model or MISSING,
        _started(row.started_ms, now_ms),
        _mib(row.rss_kib),
    )


def _widths(table: Sequence[Sequence[str]]) -> tuple[int, ...]:
    """The drawn width of each column, given every row's cell texts."""
    widths: list[int] = []
    for index, spec in enumerate(SPECS):
        if spec.fixed is not None:
            widths.append(spec.fixed)
            continue
        natural = max(
            [len(spec.label)] + [len(row[index]) for row in table],
        )
        widths.append(max(spec.minimum, min(spec.maximum, natural)))
    return tuple(widths)


def _pad(text: str, width: int, spec: _Spec) -> str:
    """*text* fitted to *width*: truncated if long, aligned if short."""
    cut = _truncate(text, width, from_left=spec.truncate_left)
    return cut.rjust(width) if spec.align_right else cut.ljust(width)


def _strip_cells(texts: Sequence[str], widths: Sequence[int]) -> list[str]:
    """Each cell as it is drawn, one space of padding on either side."""
    return [
        " " + _pad(text, width, spec) + " "
        for text, width, spec in zip(texts, widths, SPECS, strict=True)
    ]


def _strip_width(widths: Sequence[int]) -> int:
    return sum(width + 2 for width in widths) + len(widths) - 1


def _joint_line(widths: Sequence[int], joint: str) -> str:
    """A horizontal rule with a *joint* wherever a column separator falls."""
    return joint.join(HORIZONTAL * (width + 2) for width in widths)


def _clip(
    pieces: Sequence[Span], start: int, width: int, *, pad: str, pad_style: str
) -> Line:
    """The *width* characters of *pieces* beginning at *start*, padded if short.

    The one place the horizontal viewport is applied, over a sequence of spans
    rather than a string, so a row's state cell keeps its own style through the
    shift and the clip.
    """
    if width <= 0:
        return ()
    out: list[Span] = []
    cursor = 0
    for piece in pieces:
        begin, end = cursor, cursor + len(piece.text)
        cursor = end
        low, high = max(begin, start), min(end, start + width)
        if high > low:
            out.append(Span(piece.text[low - begin : high - begin], piece.style))
    drawn = sum(len(span.text) for span in out)
    if drawn < width:
        out.append(Span(pad * (width - drawn), pad_style))
    return tuple(out)


def _cut(line: Line, width: int) -> Line:
    """*line* with everything past *width* dropped, for a terminal too narrow."""
    out: list[Span] = []
    room = width
    for span in line:
        if room <= 0:
            break
        out.append(Span(span.text[:room], span.style))
        room -= len(span.text[:room])
    return tuple(out)


def _handle(visible: int, total: int, offset: int) -> tuple[int, int]:
    """Where a scrollbar handle starts and how long it is, in track units.

    The handle is the visible fraction of the content, never shorter than one
    unit (a handle that rounded to nothing would leave a scrollbar with no
    handle at all), and never past the end of the track.
    """
    length = max(1, min(visible, math.ceil(visible * visible / total)))
    start = min(offset * visible // total, visible - length)
    return max(0, start), length


def _detail_lines(row: SessionRow, home: str) -> tuple[str, ...]:
    """The three lines under an expanded row."""
    identity = DETAIL_SEP.join(
        [
            f"pid {row.pid if row.pid is not None else MISSING}",
            f"profile {row.profile or MISSING}",
            f"name source {row.name_source or MISSING}",
            f"config {tildify(row.config_dir, home)}",
        ]
    )
    return (
        DETAIL_INDENT + f"session {row.session or 'unknown'}",
        DETAIL_INDENT + identity,
        DETAIL_INDENT + f"transcript {tildify(row.transcript, home)}",
    )


def _row_line(
    texts: Sequence[str], widths: Sequence[int], *, focused: bool
) -> tuple[Span, ...]:
    """One row as three strip pieces: before the State cell, it, and after.

    The column separators carry the row's own style rather than the frame's:
    inside a focused row they are part of the highlighted band, and splitting
    them out would draw a gap through it.
    """
    drawn = _strip_cells(texts, widths)
    style = STYLE_ROW_FOCUS if focused else STYLE_ROW
    before = VERTICAL.join(drawn[:STATE_COLUMN])
    after = VERTICAL.join(drawn[STATE_COLUMN + 1 :])
    return (
        Span(before + VERTICAL, style),
        Span(drawn[STATE_COLUMN], state_style(texts[STATE_COLUMN], focused=focused)),
        Span(VERTICAL + after, style),
    )


def labels() -> tuple[str, ...]:
    """The header labels, in column order."""
    return tuple(spec.label for spec in SPECS)


def _clipped_text(text: str, start: int, width: int, pad: str) -> str:
    """*text* shifted left by *start* and fitted to *width* with *pad*."""
    if width <= 0:
        return ""
    return text[start : start + width].ljust(width, pad)


def layout(
    rows: Sequence[SessionRow],
    *,
    focus: int,
    expanded: int | None,
    height: int,
    width: int,
    hscroll: int,
    now_ms: int,
    home: str,
    show_all: bool,
) -> Frame:
    """Lay *rows* out into a frame of *height* by *width* characters.

    *focus* and *expanded* index the VISIBLE rows -- the ones
    :func:`visible_rows` keeps at this *show_all* setting -- which is the same
    list the screen's keys act on.  The vertical window is placed by
    :func:`claudewheel.vertical_viewport.compute_viewport`, so a row at an edge
    is drawn clipped rather than dropped; the horizontal offset is clamped here
    and reported back on the frame.

    Every line comes out exactly *width* characters long, and a terminal too
    short even for the frame's own chrome yields the prefix that fits rather
    than raising.
    """
    shown = visible_rows(rows, show_all)
    interior = max(0, width - 2)
    window = max(0, height - 1 - CHROME_LINES)

    table = [cells(row, now_ms=now_ms, home=home) for row in shown]
    widths = _widths(table)
    strip = _strip_width(widths)
    hscroll = max(0, min(hscroll, max(0, strip - interior)))

    heights = [
        EXPANDED_HEIGHT if index == expanded else 1 for index in range(len(shown))
    ]
    total = sum(heights)
    viewport = compute_viewport(heights, focus, window)

    # The data window, one entry per drawn line: its spans, already clipped.
    body: list[Line] = []
    for slice_ in viewport.rows:
        row = shown[slice_.index]
        focused = slice_.index == focus
        pieces: list[tuple[Span, ...]] = [
            _row_line(table[slice_.index], widths, focused=focused)
        ]
        if slice_.height > 1:
            pieces.extend(
                (Span(text, STYLE_DETAIL),) for text in _detail_lines(row, home)
            )
        visible = pieces[slice_.skip_top : slice_.skip_top + slice_.lines]
        for offset, piece in enumerate(visible):
            detail = slice_.skip_top + offset > 0
            body.append(
                _clip(
                    piece,
                    0 if detail else hscroll,
                    interior,
                    pad=" ",
                    pad_style=STYLE_DETAIL
                    if detail
                    else (STYLE_ROW_FOCUS if focused else STYLE_ROW),
                )
            )
    if not shown and window > 0:
        body.append(
            _clip(
                (Span(EMPTY_TEXT, STYLE_EMPTY),),
                0,
                interior,
                pad=" ",
                pad_style=STYLE_EMPTY,
            )
        )
    while len(body) < window:
        body.append(_clip((), 0, interior, pad=" ", pad_style=STYLE_ROW))

    # The vertical handle, over the right border of the data-window lines.
    borders = [VERTICAL] * len(body)
    if total > window > 0:
        start, length = _handle(window, total, viewport.start)
        for index in range(start, start + length):
            if index < len(borders):
                borders[index] = VERTICAL_HANDLE

    # The bottom border's interior: the joint line, with the horizontal handle
    # drawn over it wherever the strip is wider than the frame.
    bottom = list(
        _clipped_text(_joint_line(widths, JOINT_BOTTOM), hscroll, interior, HORIZONTAL)
    )
    if strip > interior > 0:
        start, length = _handle(interior, strip, hscroll)
        for index in range(start, start + length):
            bottom[index] = HORIZONTAL_HANDLE

    lines: list[Line] = [
        (
            Span(TOP_LEFT, STYLE_FRAME),
            Span(
                _clipped_text(
                    _joint_line(widths, JOINT_TOP), hscroll, interior, HORIZONTAL
                ),
                STYLE_FRAME,
            ),
            Span(TOP_RIGHT, STYLE_FRAME),
        ),
        (
            Span(VERTICAL, STYLE_FRAME),
            *_clip(
                (Span(VERTICAL.join(_strip_cells(labels(), widths)), STYLE_HEADER),),
                hscroll,
                interior,
                pad=" ",
                pad_style=STYLE_HEADER,
            ),
            Span(VERTICAL, STYLE_FRAME),
        ),
        (
            Span(RULE_LEFT, STYLE_FRAME),
            Span(
                _clipped_text(
                    _joint_line(widths, JOINT_RULE), hscroll, interior, HORIZONTAL
                ),
                STYLE_FRAME,
            ),
            Span(RULE_RIGHT, STYLE_FRAME),
        ),
    ]
    for border, line in zip(borders, body, strict=True):
        lines.append((Span(VERTICAL, STYLE_FRAME), *line, Span(border, STYLE_FRAME)))
    lines.append(
        (
            Span(BOTTOM_LEFT, STYLE_FRAME),
            Span("".join(bottom), STYLE_FRAME),
            Span(BOTTOM_RIGHT, STYLE_FRAME),
        )
    )

    drawn = [_cut(line, width) for line in lines[: max(0, height - 1)]]
    return Frame(
        lines=tuple(drawn),
        window=window,
        total_lines=total,
        strip_width=strip,
        interior_width=interior,
        hscroll=hscroll,
    )
