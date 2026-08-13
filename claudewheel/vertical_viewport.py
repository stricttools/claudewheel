"""Scroll a column of variable-height row blocks, as arithmetic over dimensions.

This is the vertical mirror of the renderer's horizontal viewport
(:meth:`claudewheel.renderer.Renderer._compute_viewport`), and it keeps that
one's rule: when the content does not fit, the window is *centered on the
focused item* and then clamped to the content, so scrolling is a function of
the focus alone and nothing has to be remembered between frames.

Two deliberate differences from the horizontal original:

* **It is pure.** The horizontal version reads ``self.term.cols`` in the middle
  of its arithmetic, so it cannot be exercised without a terminal object.  Here
  every dimension -- the row heights and the window height -- is a parameter,
  and the result is a plain value.
* **Rows have heights.** A horizontal segment occupies one screen row; a
  session block occupies two or three lines collapsed and five when
  highlighted, so the arithmetic runs over cumulative line offsets rather than
  a single row index, and the focused row may itself be taller than the whole
  window.  When it is, its *top* is pinned to the top of the window: centering
  a block that cannot fit would cut off the line carrying its name.

The result reports one :class:`RowSlice` per row with any visible line,
including the partially visible rows at each edge, and says how many lines of
each were cut.  It never decides what to do about a partial row -- drawing it
clipped or dropping it is the screen's choice, and both are expressible from
the same result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RowSlice:
    """The visible part of one row block.

    ``screen_top`` is window-relative (0 is the window's first line);
    ``skip_top`` counts the row's own leading lines that fall above the window,
    and ``lines`` how many of its lines are visible.
    """

    index: int
    screen_top: int
    skip_top: int
    lines: int
    height: int

    @property
    def skip_bottom(self) -> int:
        """The row's own trailing lines that fall below the window."""
        return self.height - self.skip_top - self.lines

    @property
    def clipped(self) -> bool:
        """True when some of the row's lines are outside the window."""
        return self.lines < self.height


@dataclass(frozen=True)
class Viewport:
    """Where the window sits over the content, and what it shows.

    ``start`` is the first visible content line, ``height`` the window as the
    caller declared it, and ``total`` the summed height of every row.
    """

    start: int
    height: int
    total: int
    rows: tuple[RowSlice, ...] = field(default_factory=tuple)
    hidden_above: int = 0
    hidden_below: int = 0

    @property
    def scrolling(self) -> bool:
        """True when the content is taller than the window."""
        return self.total > self.height


def row_tops(row_heights: Sequence[int]) -> tuple[int, ...]:
    """The content line each row starts at, one entry per row."""
    tops: list[int] = []
    offset = 0
    for height in row_heights:
        tops.append(offset)
        offset += height
    return tuple(tops)


def _start_line(
    row_heights: Sequence[int], tops: Sequence[int], focus_idx: int, window_height: int
) -> int:
    """The first visible content line, by the centering rule described above."""
    total = sum(row_heights)
    if window_height <= 0 or total <= window_height:
        return 0
    if not 0 <= focus_idx < len(row_heights):
        # No row is focused -- the horizontal original's same fallback.
        return 0
    top = tops[focus_idx]
    height = row_heights[focus_idx]
    start = top + (height - window_height) // 2
    start = max(0, min(start, total - window_height))
    if height > window_height:
        # Centering a block taller than the window would cut off its first
        # line, which is the one carrying its name.
        start = min(start, top)
    return start


def compute_viewport(
    row_heights: Sequence[int], focus_idx: int, window_height: int
) -> Viewport:
    """Place a *window_height*-line window over rows of the given heights.

    The window is centered on the row at *focus_idx* and clamped to the
    content; a focused row taller than the window has its top pinned instead.
    A *focus_idx* naming no row, and a window with no lines, both leave the
    window at the top. Negative heights are a caller bug and raise.
    """
    if window_height < 0:
        raise ValueError(f"window_height must not be negative: {window_height}")
    for index, height in enumerate(row_heights):
        if height < 0:
            raise ValueError(f"row {index} has a negative height: {height}")

    tops = row_tops(row_heights)
    total = sum(row_heights)
    start = _start_line(row_heights, tops, focus_idx, window_height)
    end = start + window_height

    slices: list[RowSlice] = []
    hidden_above = 0
    hidden_below = 0
    for index, height in enumerate(row_heights):
        top = tops[index]
        bottom = top + height
        if bottom <= start:
            hidden_above += 1
            continue
        if top >= end:
            hidden_below += 1
            continue
        visible_top = max(top, start)
        visible_bottom = min(bottom, end)
        slices.append(
            RowSlice(
                index=index,
                screen_top=visible_top - start,
                skip_top=visible_top - top,
                lines=visible_bottom - visible_top,
                height=height,
            )
        )
    return Viewport(
        start=start,
        height=window_height,
        total=total,
        rows=tuple(slices),
        hidden_above=hidden_above,
        hidden_below=hidden_below,
    )
