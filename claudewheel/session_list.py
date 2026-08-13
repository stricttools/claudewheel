"""The list component both session screens are drawn with.

Two screens show the same column of session blocks: the checklist deletion
presents of everything holding a profile, and the sessions overview.  They
differ in what a key does, not in how the column looks or scrolls, so the
looking and the scrolling live here once -- built on
:func:`claudewheel.vertical_viewport.compute_viewport` for the arithmetic and
:func:`claudewheel.session_rows.format_row` for the text.

The frame builder is pure: rows, dimensions, a clock and an identity in, a list
of :class:`FrameLine` out.  Each line carries a *style* naming what it is
(title, hint, an ordinary row line, the focused row, or whatever a row declared
for its state line), and :func:`render_frame` is the only place a style becomes
an escape sequence.  That split is what lets the whole layout -- scrolling,
clipping, truncation, the current-session mark -- be tested without a terminal.

A screen owns its own key loop.  What it does with a row (tick it, stop it,
prune it) is the screen's business; getting the right lines onto the right
rows of a window that may be too short is this module's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .constants import BOLD, CLEAR_SCREEN, GREEN, RED, RESET, move_to
from .session_registry import SessionRecord
from .session_rows import SessionIdentity, format_row
from .terminal import Terminal
from .theme import ThemeColors
from .vertical_viewport import compute_viewport

#: The tick a row carries when it is selected, and when it is not.
SELECTOR_ON = "[x]"
SELECTOR_OFF = "[ ]"

#: Style names the builder assigns and :func:`render_frame` colours.
STYLE_TITLE = "title"
STYLE_HINT = "hint"
STYLE_ROW = "row"
STYLE_FOCUS = "focus"
STYLE_EMPTY = "empty"
STYLE_BLANK = "blank"

#: Style names a caller may put on a row's state line.  ``running`` is the
#: indicator's live colour and ``stopped`` its dead one -- the green-to-red
#: transition the deletion checklist plays as it stops what was ticked.
STYLE_RUNNING = "running"
STYLE_STOPPED = "stopped"

#: Lines reserved around the scrolling window: the title, a blank line under
#: it, a blank line above the hint, and the hint.
_CHROME_LINES = 4

#: The two things a screen can do with a row only partly inside the window.
#: ``PARTIAL_CLIP`` draws the lines that fit; ``PARTIAL_HIDE`` leaves the whole
#: block out and blanks the lines it would have used.
PARTIAL_CLIP = "clip"
PARTIAL_HIDE = "hide"


@dataclass(frozen=True)
class FrameLine:
    """One rendered line and what kind of line it is."""

    text: str
    style: str


@dataclass(frozen=True)
class ListRow:
    """One session in the list, with whatever the screen has decided about it.

    *selected* is the screen's tick state -- ``None`` for a screen with no
    selector column at all, which is how the overview asks for no ``[x]``.
    *state* is the per-row state line; *state_style* names its colour.
    *rss_kib* is the resident memory measured for the record's pid, or ``None``
    when nothing measured it.
    """

    record: SessionRecord
    selected: bool | None = None
    state: str | None = None
    state_style: str = STYLE_RUNNING
    rss_kib: int | None = None

    @property
    def selector(self) -> str | None:
        """The ``[x]`` / ``[ ]`` toggle, or None for a screen without one."""
        if self.selected is None:
            return None
        return SELECTOR_ON if self.selected else SELECTOR_OFF


def _lines_for(
    row: ListRow, *, highlighted: bool, now_ms: int, identity: SessionIdentity | None
) -> tuple[str, ...]:
    return format_row(
        row.record,
        highlighted=highlighted,
        now_ms=now_ms,
        identity=identity,
        rss_kib=row.rss_kib,
        state=row.state,
        selector=row.selector,
    )


def row_heights(
    rows: Sequence[ListRow],
    *,
    focus: int,
    now_ms: int,
    identity: SessionIdentity | None = None,
) -> list[int]:
    """The block height of each row, which is what the viewport scrolls over."""
    return [
        len(
            _lines_for(
                row, highlighted=(index == focus), now_ms=now_ms, identity=identity
            )
        )
        for index, row in enumerate(rows)
    ]


def move_focus(focus: int, count: int, step: int) -> int:
    """Move the focus by *step* within *count* rows, clamped at both ends.

    Clamping rather than wrapping, matching the form runner's own traversal.
    An empty list has no focus at all, which is ``-1``.
    """
    if count <= 0:
        return -1
    return max(0, min(count - 1, focus + step))


def build_frame(
    rows: Sequence[ListRow],
    *,
    focus: int,
    now_ms: int,
    title: str,
    hint: str,
    height: int,
    width: int,
    partial_rows: str = PARTIAL_CLIP,
    identity: SessionIdentity | None = None,
    empty_text: str = "No sessions.",
) -> list[FrameLine]:
    """Lay the list out into at most *height* lines of at most *width* columns.

    The window is centered on the focused row and clamped to the content (the
    viewport's rule), and the screen never draws past its last row.  A *height*
    too small even for the chrome yields whatever prefix of it fits, so a tiny
    terminal renders something rather than raising.

    *partial_rows* decides what happens to a row only partly inside the window
    -- the viewport reports both the lines that fit and the ones cut, and says
    nothing about which to prefer:

    ``PARTIAL_CLIP``
        Draw the lines that fit.  The window is always full; the block at an
        edge is cut mid-way.
    ``PARTIAL_HIDE``
        Draw whole blocks only, leaving the cut ones out and the freed lines
        blank.  The focused row is the one exception: it is clipped rather than
        hidden, because a window too short for it would otherwise show nothing
        at all.

    Both behaviours exist so they can be compared on a real screen before one
    of them is kept and the other deleted; the default names the incumbent for
    the length of that comparison, and every screen passes the value it wants
    explicitly.
    """
    if partial_rows not in (PARTIAL_CLIP, PARTIAL_HIDE):
        raise ValueError(f"unknown partial_rows behaviour: {partial_rows!r}")

    frame: list[FrameLine] = [
        FrameLine(title[:width], STYLE_TITLE),
        FrameLine("", STYLE_BLANK),
    ]
    window = max(0, height - _CHROME_LINES)

    if not rows:
        frame.append(FrameLine(empty_text[:width], STYLE_EMPTY))
    else:
        heights = row_heights(rows, focus=focus, now_ms=now_ms, identity=identity)
        viewport = compute_viewport(heights, focus, window)
        for slice_ in viewport.rows:
            row = rows[slice_.index]
            highlighted = slice_.index == focus
            if partial_rows == PARTIAL_HIDE and slice_.clipped and not highlighted:
                frame.extend(FrameLine("", STYLE_BLANK) for _ in range(slice_.lines))
                continue
            lines = _lines_for(
                row, highlighted=highlighted, now_ms=now_ms, identity=identity
            )
            visible = lines[slice_.skip_top : slice_.skip_top + slice_.lines]
            state_index = len(lines) - 1 if row.state is not None else None
            for offset, text in enumerate(visible):
                absolute = slice_.skip_top + offset
                if state_index is not None and absolute == state_index:
                    style = row.state_style
                elif highlighted:
                    style = STYLE_FOCUS
                else:
                    style = STYLE_ROW
                frame.append(FrameLine(text[:width], style))

    frame.append(FrameLine("", STYLE_BLANK))
    frame.append(FrameLine(hint[:width], STYLE_HINT))
    return frame[:height] if height >= 0 else frame


def render_frame(
    terminal: Terminal,
    theme: ThemeColors,
    frame: Sequence[FrameLine],
    *,
    left_col: int = 2,
) -> None:
    """Draw *frame* over a cleared screen, one line per terminal row.

    The two indicator colours are the terminal's own green and red rather than
    theme entries: "this process is still up" and "this process is gone" mean
    the same thing under every palette, and a theme that recoloured them would
    be recolouring the answer.
    """
    styles = {
        STYLE_TITLE: BOLD + theme.forms_title_fg,
        STYLE_HINT: theme.forms_hint_fg,
        STYLE_ROW: theme.forms_field_fg,
        STYLE_FOCUS: BOLD + theme.forms_focus_fg,
        STYLE_EMPTY: theme.forms_readonly_fg,
        STYLE_BLANK: "",
        STYLE_RUNNING: GREEN,
        STYLE_STOPPED: RED,
    }
    buf: list[str] = [CLEAR_SCREEN]
    for index, line in enumerate(frame):
        colour = styles.get(line.style, theme.forms_field_fg)
        buf.append(move_to(index + 1, left_col) + colour + line.text + RESET)
    terminal.write("".join(buf))
