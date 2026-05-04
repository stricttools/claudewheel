"""Tests for viewport scrolling, bar layout computation, offscreen counts, and minimap."""

from __future__ import annotations

import unittest

from claudewheel.renderer import ARROW_MARGIN, Renderer
from claudewheel.segment import Segment, SegmentBar
from claudewheel.theme import ThemeColors


class _MockTerminal:
    """Lightweight stand-in for Terminal that avoids opening /dev/tty."""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.rows = rows
        self.cols = cols

    def write(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass


def _make_theme(**overrides: str) -> ThemeColors:
    """Build a ThemeColors with safe defaults and optional overrides."""
    defaults = dict(
        global_fg="",
        label_fg="",
        separator_fg="",
        separator_char=" | ",
        empty_value_fg="",
        empty_value_text="---",
        segment_colors={},
        search_cursor_fg="",
        search_match_fg="",
        search_no_match_fg="",
        overflow_arrow_fg="",
        overflow_minimap_fg="",
        overflow_minimap_focused_bg="",
    )
    defaults.update(overrides)
    return ThemeColors(**defaults)


def _make_segment(key: str, label: str, **kwargs) -> Segment:
    """Shorthand for constructing a Segment with common defaults."""
    defaults = dict(
        options=["val1"],
        selected_idx=0,
        min_width=4,
        max_width=10,
    )
    defaults.update(kwargs)
    return Segment(key=key, label=label, **defaults)


# ---------------------------------------------------------------------------
# 1. BarLayoutTests
# ---------------------------------------------------------------------------


class BarLayoutTests(unittest.TestCase):
    """Test _compute_bar_layout() positions, widths, and total_width."""

    def setUp(self) -> None:
        self.theme = _make_theme()
        self.sep = self.theme.separator_char  # " | "

    def test_first_segment_col_is_2(self) -> None:
        """The first segment starts at column 2 (left margin)."""
        bar = SegmentBar(segments=[
            _make_segment("a", "AA"),
            _make_segment("b", "BB"),
            _make_segment("c", "CC"),
        ], focus_idx=0)
        r = Renderer(_MockTerminal(), self.theme)
        layout, _ = r._compute_bar_layout(bar)
        self.assertEqual(layout[0]["col"], 2)

    def test_subsequent_cols_follow_prev_width_plus_separator(self) -> None:
        """Each segment's col = prev col + prev label_width + prev value_width + len(sep)."""
        bar = SegmentBar(segments=[
            _make_segment("a", "AA"),
            _make_segment("b", "BB"),
            _make_segment("c", "CC"),
        ], focus_idx=0)
        r = Renderer(_MockTerminal(), self.theme)
        layout, _ = r._compute_bar_layout(bar)

        for i in range(1, len(layout)):
            prev = layout[i - 1]
            expected_col = prev["col"] + prev["label_width"] + prev["value_width"] + len(self.sep)
            self.assertEqual(
                layout[i]["col"], expected_col,
                f"segment {i} col mismatch: expected {expected_col}, got {layout[i]['col']}",
            )

    def test_total_width_equals_last_segment_end(self) -> None:
        """total_width = last segment's col + label_width + value_width."""
        bar = SegmentBar(segments=[
            _make_segment("a", "AA"),
            _make_segment("b", "BB"),
            _make_segment("c", "CC"),
        ], focus_idx=0)
        r = Renderer(_MockTerminal(), self.theme)
        layout, total_width = r._compute_bar_layout(bar)

        last = layout[-1]
        expected = last["col"] + last["label_width"] + last["value_width"]
        self.assertEqual(total_width, expected)

    def test_creating_segment_has_cursor_and_extra_width(self) -> None:
        """A focused segment with creating=True has has_cursor=True and total_width includes +1."""
        seg = _make_segment("a", "AA", creating=True, create_buffer="new")
        bar = SegmentBar(segments=[seg], focus_idx=0)
        r = Renderer(_MockTerminal(), self.theme)
        layout, total_width = r._compute_bar_layout(bar)

        self.assertTrue(layout[0]["has_cursor"])
        # total_width should be col + label_width + value_width + 1 (cursor)
        expected = layout[0]["col"] + layout[0]["label_width"] + layout[0]["value_width"] + 1
        self.assertEqual(total_width, expected)


# ---------------------------------------------------------------------------
# 2. ViewportMathTests
# ---------------------------------------------------------------------------


class ViewportMathTests(unittest.TestCase):
    """Test _compute_viewport() logic for various terminal widths and focus positions."""

    def setUp(self) -> None:
        self.theme = _make_theme()

    def _wide_bar_segments(self, count: int = 8) -> list[Segment]:
        """Build segments with long labels and wide values to exceed typical terminal width."""
        segs = []
        for i in range(count):
            key = f"seg{i}"
            label = f"Segment-{i}"
            segs.append(_make_segment(
                key, label,
                options=[f"longvalue{i}"],
                selected_idx=0,
                min_width=10,
                max_width=20,
            ))
        return segs

    def test_no_scrolling_when_fits(self) -> None:
        """When total_width <= term.cols, viewport start is 0."""
        bar = SegmentBar(segments=[
            _make_segment("a", "A"),
        ], focus_idx=0)
        r = Renderer(_MockTerminal(cols=200), self.theme)
        layout, total_width = r._compute_bar_layout(bar)
        vp = r._compute_viewport(layout, total_width)
        self.assertEqual(vp, 0)

    def test_focused_first_segment_clamped_to_zero(self) -> None:
        """When the first segment is focused, vp_start should be clamped to 0."""
        segs = self._wide_bar_segments()
        bar = SegmentBar(segments=segs, focus_idx=0)
        r = Renderer(_MockTerminal(cols=60), self.theme)
        layout, total_width = r._compute_bar_layout(bar)
        # Confirm the bar actually overflows
        self.assertGreater(total_width, 60)
        vp = r._compute_viewport(layout, total_width)
        self.assertEqual(vp, 0)

    def test_focused_last_segment_clamped_to_max(self) -> None:
        """When the last segment is focused, vp_start should be clamped to total_width - usable."""
        segs = self._wide_bar_segments()
        bar = SegmentBar(segments=segs, focus_idx=len(segs) - 1)
        r = Renderer(_MockTerminal(cols=60), self.theme)
        layout, total_width = r._compute_bar_layout(bar)
        self.assertGreater(total_width, 60)
        vp = r._compute_viewport(layout, total_width)
        usable = 60 - 2 * ARROW_MARGIN
        self.assertEqual(vp, total_width - usable)

    def test_focused_middle_segment_centers_viewport(self) -> None:
        """When a middle segment is focused, viewport centers on it."""
        segs = self._wide_bar_segments(count=10)
        mid = len(segs) // 2
        bar = SegmentBar(segments=segs, focus_idx=mid)
        r = Renderer(_MockTerminal(cols=60), self.theme)
        layout, total_width = r._compute_bar_layout(bar)
        self.assertGreater(total_width, 60)

        vp = r._compute_viewport(layout, total_width)
        usable = 60 - 2 * ARROW_MARGIN
        focused = layout[mid]
        seg_center = focused["col"] + (focused["label_width"] + focused["value_width"]) // 2
        expected = seg_center - usable // 2
        # Clamp to valid range
        expected = max(0, min(expected, total_width - usable))
        self.assertEqual(vp, expected)

    def test_very_narrow_terminal_returns_zero(self) -> None:
        """When term.cols < 2*ARROW_MARGIN, usable <= 0 and viewport returns 0."""
        segs = self._wide_bar_segments()
        bar = SegmentBar(segments=segs, focus_idx=0)
        r = Renderer(_MockTerminal(cols=5), self.theme)
        layout, total_width = r._compute_bar_layout(bar)
        self.assertGreater(total_width, 5)
        vp = r._compute_viewport(layout, total_width)
        self.assertEqual(vp, 0)


# ---------------------------------------------------------------------------
# 3. OffscreenCountTests
# ---------------------------------------------------------------------------


class OffscreenCountTests(unittest.TestCase):
    """Test _count_offscreen() with manually-set _bar_layout and _viewport_start."""

    def _make_renderer(self, cols: int = 80) -> Renderer:
        theme = _make_theme()
        return Renderer(_MockTerminal(cols=cols), theme)

    def _fake_layout_item(
        self, key: str, col: int, label_width: int, value_width: int
    ) -> dict:
        """Build a minimal layout dict with only the fields _count_offscreen reads."""
        return {
            "key": key,
            "col": col,
            "label_width": label_width,
            "value_width": value_width,
            "has_cursor": False,
            "is_focused": False,
            "label_str": "",
            "display_value": "",
        }

    def test_viewport_at_start_no_left_offscreen(self) -> None:
        """With viewport_start=0, no segments should be off-screen left."""
        r = self._make_renderer(cols=80)
        r._bar_layout = [
            self._fake_layout_item("a", col=2, label_width=4, value_width=6),
            self._fake_layout_item("b", col=15, label_width=4, value_width=6),
            self._fake_layout_item("c", col=28, label_width=4, value_width=6),
        ]
        r._viewport_start = 0
        left, right = r._count_offscreen()
        self.assertEqual(left, 0)

    def test_viewport_shifted_right_counts_left_offscreen(self) -> None:
        """Shifting the viewport right pushes segments off the left edge."""
        r = self._make_renderer(cols=40)
        # Three segments spread over a wide bar
        r._bar_layout = [
            self._fake_layout_item("a", col=2, label_width=4, value_width=6),
            self._fake_layout_item("b", col=50, label_width=4, value_width=6),
            self._fake_layout_item("c", col=100, label_width=4, value_width=6),
        ]
        # Shift viewport so segment "a" is entirely off-screen left.
        # screen_col = col - vp_start + ARROW_MARGIN
        # For seg "a": screen_col = 2 - 20 + 4 = -14, seg_right = -14 + 10 = -4
        # seg_right (-4) <= ARROW_MARGIN (4) => left_count=1
        r._viewport_start = 20
        left, right = r._count_offscreen()
        self.assertEqual(left, 1)

    def test_viewport_at_start_counts_right_offscreen(self) -> None:
        """With viewport at start, segments far to the right are off-screen right."""
        r = self._make_renderer(cols=40)
        r._bar_layout = [
            self._fake_layout_item("a", col=2, label_width=4, value_width=6),
            self._fake_layout_item("b", col=50, label_width=4, value_width=6),
            self._fake_layout_item("c", col=100, label_width=4, value_width=6),
        ]
        r._viewport_start = 0
        left, right = r._count_offscreen()
        self.assertEqual(left, 0)
        # right_margin = 40 - 4 = 36
        # seg "b": screen_col = 50 - 0 + 4 = 54, 54 >= 36 => right
        # seg "c": screen_col = 100 - 0 + 4 = 104, 104 >= 36 => right
        self.assertEqual(right, 2)

    def test_both_sides_offscreen(self) -> None:
        """When viewport is in the middle, segments can be off-screen on both sides."""
        r = self._make_renderer(cols=40)
        r._bar_layout = [
            self._fake_layout_item("a", col=2, label_width=4, value_width=6),
            self._fake_layout_item("b", col=50, label_width=4, value_width=6),
            self._fake_layout_item("c", col=100, label_width=4, value_width=6),
        ]
        # vp_start=45: seg "a" screen_col=2-45+4=-39, right=-39+10=-29 <=4 => left
        # seg "b" screen_col=50-45+4=9, right=9+10=19 => visible
        # seg "c" screen_col=100-45+4=59, 59>=36 => right
        r._viewport_start = 45
        left, right = r._count_offscreen()
        self.assertEqual(left, 1)
        self.assertEqual(right, 1)


# ---------------------------------------------------------------------------
# 4. MinimapVisibilityTests
# ---------------------------------------------------------------------------


class MinimapVisibilityTests(unittest.TestCase):
    """Test that _render_minimap appends to buf under the right conditions."""

    def _make_bar(self) -> SegmentBar:
        return SegmentBar(segments=[
            _make_segment("a", "AA"),
            _make_segment("b", "BB"),
        ], focus_idx=0)

    def _make_renderer(
        self, minimap_mode: str = "auto", cols: int = 80, scrolling: bool = False
    ) -> Renderer:
        theme = _make_theme()
        r = Renderer(_MockTerminal(cols=cols), theme, minimap_mode=minimap_mode)
        r._scrolling = scrolling
        return r

    def test_auto_mode_no_scrolling_does_not_render(self) -> None:
        """In 'auto' mode with _scrolling=False, minimap should NOT add to buf."""
        r = self._make_renderer(minimap_mode="auto", scrolling=False)
        bar = self._make_bar()
        buf: list[str] = []
        r._render_minimap(buf, bar)
        self.assertEqual(len(buf), 0)

    def test_auto_mode_scrolling_renders(self) -> None:
        """In 'auto' mode with _scrolling=True, minimap SHOULD add to buf."""
        r = self._make_renderer(minimap_mode="auto", scrolling=True)
        bar = self._make_bar()
        buf: list[str] = []
        r._render_minimap(buf, bar)
        self.assertGreater(len(buf), 0)

    def test_always_mode_no_scrolling_renders(self) -> None:
        """In 'always' mode, minimap renders even when _scrolling=False."""
        r = self._make_renderer(minimap_mode="always", scrolling=False)
        bar = self._make_bar()
        buf: list[str] = []
        r._render_minimap(buf, bar)
        self.assertGreater(len(buf), 0)

    def test_narrow_terminal_suppresses_minimap(self) -> None:
        """When start_col < 1 (terminal too narrow for minimap), buf stays empty."""
        # 2 segments need start_col = cols - 2. With cols=2, start_col=0 < 1.
        r = self._make_renderer(minimap_mode="always", cols=2, scrolling=True)
        bar = self._make_bar()
        buf: list[str] = []
        r._render_minimap(buf, bar)
        self.assertEqual(len(buf), 0)


if __name__ == "__main__":
    unittest.main()
