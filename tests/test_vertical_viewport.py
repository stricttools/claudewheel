"""Tests for the vertical viewport arithmetic over variable-height row blocks.

No terminal is involved anywhere in this file: every dimension is a parameter,
which is the deliberate difference from the renderer's horizontal viewport
(that one reads the Terminal object directly).
"""

from __future__ import annotations

import unittest

from claudewheel.vertical_viewport import Viewport, compute_viewport, row_tops


class RowTopsTests(unittest.TestCase):
    def test_cumulative_offsets(self) -> None:
        """Each row's top is the sum of the heights before it."""
        self.assertEqual(row_tops([2, 3, 5]), (0, 2, 5))

    def test_empty(self) -> None:
        """No rows means no offsets."""
        self.assertEqual(row_tops([]), ())


class FitsEntirelyTests(unittest.TestCase):
    def test_no_scroll_when_content_fits(self) -> None:
        """Content shorter than the window starts at line 0 and never scrolls."""
        vp = compute_viewport([2, 2, 3], focus_idx=0, window_height=10)
        self.assertEqual(vp.start, 0)
        self.assertEqual(vp.total, 7)
        self.assertFalse(vp.scrolling)
        self.assertEqual(len(vp.rows), 3)
        self.assertEqual([r.index for r in vp.rows], [0, 1, 2])
        self.assertEqual([r.screen_top for r in vp.rows], [0, 2, 4])
        self.assertEqual([r.lines for r in vp.rows], [2, 2, 3])
        self.assertFalse(any(r.clipped for r in vp.rows))
        self.assertEqual((vp.hidden_above, vp.hidden_below), (0, 0))

    def test_fits_exactly(self) -> None:
        """Content exactly as tall as the window is not scrolling."""
        vp = compute_viewport([4, 6], focus_idx=1, window_height=10)
        self.assertEqual(vp.start, 0)
        self.assertFalse(vp.scrolling)
        self.assertEqual(len(vp.rows), 2)

    def test_focus_anywhere_is_irrelevant_when_it_fits(self) -> None:
        """Every focus index yields the same viewport while the content fits."""
        for focus in range(3):
            with self.subTest(focus=focus):
                vp = compute_viewport([2, 2, 3], focus_idx=focus, window_height=10)
                self.assertEqual(vp.start, 0)


class ScrollDirectionTests(unittest.TestCase):
    """Ten uniform rows of three lines in a nine-line window."""

    HEIGHTS = [3] * 10
    WINDOW = 9

    def _start(self, focus: int) -> int:
        return compute_viewport(
            self.HEIGHTS, focus_idx=focus, window_height=self.WINDOW
        ).start

    def test_scrolling_down(self) -> None:
        """Moving focus toward the end moves the window down, never up."""
        starts = [self._start(i) for i in range(10)]
        self.assertEqual(starts, [0, 0, 3, 6, 9, 12, 15, 18, 21, 21])
        for earlier, later in zip(starts, starts[1:]):
            self.assertLessEqual(earlier, later)

    def test_scrolling_up(self) -> None:
        """Moving focus back toward the start moves the window up."""
        self.assertEqual(self._start(6), 15)
        self.assertEqual(self._start(4), 9)
        self.assertLess(self._start(4), self._start(6))

    def test_focused_row_is_always_fully_visible(self) -> None:
        """Whatever the focus, its whole block lies inside the window."""
        for focus in range(10):
            with self.subTest(focus=focus):
                vp = compute_viewport(
                    self.HEIGHTS, focus_idx=focus, window_height=self.WINDOW
                )
                slices = {r.index: r for r in vp.rows}
                self.assertIn(focus, slices)
                self.assertFalse(slices[focus].clipped)

    def test_middle_focus_positions_and_counts(self) -> None:
        """A mid-list focus centers the window and reports both hidden counts."""
        vp = compute_viewport(self.HEIGHTS, focus_idx=5, window_height=self.WINDOW)
        self.assertEqual(vp.start, 12)
        self.assertEqual([r.index for r in vp.rows], [4, 5, 6])
        self.assertEqual([r.screen_top for r in vp.rows], [0, 3, 6])
        self.assertEqual((vp.hidden_above, vp.hidden_below), (4, 3))
        self.assertTrue(vp.scrolling)


class ClampTests(unittest.TestCase):
    def test_clamp_at_top(self) -> None:
        """Focusing the first row never scrolls above line 0."""
        vp = compute_viewport([3] * 10, focus_idx=0, window_height=9)
        self.assertEqual(vp.start, 0)
        self.assertEqual([r.index for r in vp.rows], [0, 1, 2])
        self.assertEqual((vp.hidden_above, vp.hidden_below), (0, 7))

    def test_clamp_at_bottom(self) -> None:
        """Focusing the last row never scrolls past the final line."""
        vp = compute_viewport([3] * 10, focus_idx=9, window_height=9)
        self.assertEqual(vp.start, 21)
        self.assertEqual(vp.start + vp.height, vp.total)
        self.assertEqual([r.index for r in vp.rows], [7, 8, 9])
        self.assertEqual((vp.hidden_above, vp.hidden_below), (7, 0))


class TallFocusedRowTests(unittest.TestCase):
    def test_focused_row_taller_than_window_pins_its_top(self) -> None:
        """A block taller than the window shows its first line, not its middle."""
        vp = compute_viewport([2, 12, 2], focus_idx=1, window_height=5)
        self.assertEqual(vp.start, 2)
        self.assertEqual([r.index for r in vp.rows], [1])
        row = vp.rows[0]
        self.assertEqual(row.screen_top, 0)
        self.assertEqual(row.skip_top, 0)
        self.assertEqual(row.lines, 5)
        self.assertEqual(row.skip_bottom, 7)
        self.assertTrue(row.clipped)
        self.assertEqual((vp.hidden_above, vp.hidden_below), (1, 1))

    def test_single_row_taller_than_window(self) -> None:
        """The only row, taller than the window, still starts at its own top."""
        vp = compute_viewport([12], focus_idx=0, window_height=4)
        self.assertEqual(vp.start, 0)
        self.assertEqual(len(vp.rows), 1)
        self.assertEqual(vp.rows[0].skip_top, 0)
        self.assertEqual(vp.rows[0].lines, 4)


class PartialRowTests(unittest.TestCase):
    def test_partial_rows_report_what_is_cut(self) -> None:
        """A row straddling the top edge reports its skipped lines."""
        vp = compute_viewport([4, 4, 4], focus_idx=1, window_height=5)
        self.assertEqual(vp.start, 3)
        self.assertEqual([r.index for r in vp.rows], [0, 1])
        first, second = vp.rows
        self.assertEqual((first.skip_top, first.lines, first.screen_top), (3, 1, 0))
        self.assertTrue(first.clipped)
        self.assertEqual((second.skip_top, second.lines, second.screen_top), (0, 4, 1))
        self.assertFalse(second.clipped)
        self.assertEqual((vp.hidden_above, vp.hidden_below), (0, 1))

    def test_row_straddling_the_bottom_edge(self) -> None:
        """A row cut by the bottom edge reports the lines below the fold."""
        vp = compute_viewport([4, 4, 4], focus_idx=0, window_height=6)
        self.assertEqual(vp.start, 0)
        second = vp.rows[1]
        self.assertEqual((second.skip_top, second.lines, second.skip_bottom), (0, 2, 2))


class DegenerateInputTests(unittest.TestCase):
    def test_zero_rows(self) -> None:
        """An empty list is an empty viewport, not an error."""
        vp = compute_viewport([], focus_idx=0, window_height=10)
        self.assertEqual(vp, Viewport(start=0, height=10, total=0, rows=()))
        self.assertEqual(vp.rows, ())
        self.assertFalse(vp.scrolling)
        self.assertEqual((vp.hidden_above, vp.hidden_below), (0, 0))

    def test_one_row_that_fits(self) -> None:
        """A single fitting row occupies the top of the window."""
        vp = compute_viewport([3], focus_idx=0, window_height=10)
        self.assertEqual(vp.start, 0)
        self.assertEqual(len(vp.rows), 1)
        self.assertEqual(vp.rows[0].lines, 3)
        self.assertFalse(vp.rows[0].clipped)

    def test_zero_height_window_shows_nothing(self) -> None:
        """A window with no lines shows no rows and stays at line 0."""
        vp = compute_viewport([3, 3], focus_idx=1, window_height=0)
        self.assertEqual(vp.start, 0)
        self.assertEqual(vp.rows, ())
        self.assertEqual((vp.hidden_above, vp.hidden_below), (0, 2))

    def test_zero_height_rows_produce_no_slice(self) -> None:
        """A row with no lines is not a visible row."""
        vp = compute_viewport([0, 3], focus_idx=1, window_height=10)
        self.assertEqual([r.index for r in vp.rows], [1])

    def test_focus_out_of_range_starts_at_the_top(self) -> None:
        """An index naming no row falls back to the top, like the horizontal one."""
        for focus in (-1, 10, 99):
            with self.subTest(focus=focus):
                vp = compute_viewport([3] * 10, focus_idx=focus, window_height=9)
                self.assertEqual(vp.start, 0)

    def test_negative_window_height_is_an_error(self) -> None:
        """A negative window is a caller bug, not a viewport to compute."""
        with self.assertRaises(ValueError):
            compute_viewport([3], focus_idx=0, window_height=-1)

    def test_negative_row_height_is_an_error(self) -> None:
        """A negative row height is a caller bug."""
        with self.assertRaises(ValueError):
            compute_viewport([3, -1], focus_idx=0, window_height=10)


class PurityTests(unittest.TestCase):
    def test_repeated_calls_agree(self) -> None:
        """The same inputs give the same viewport every time."""
        first = compute_viewport([2, 5, 2, 9], focus_idx=2, window_height=7)
        second = compute_viewport([2, 5, 2, 9], focus_idx=2, window_height=7)
        self.assertEqual(first, second)

    def test_input_sequence_is_not_mutated(self) -> None:
        """The heights the caller passed come back unchanged."""
        heights = [2, 5, 2, 9]
        compute_viewport(heights, focus_idx=3, window_height=7)
        self.assertEqual(heights, [2, 5, 2, 9])


if __name__ == "__main__":
    unittest.main()
