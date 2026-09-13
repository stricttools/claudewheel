"""Tests for the pure sessions-table layout.

Nothing here touches a terminal, a clock or the filesystem: rows go in,
:class:`claudewheel.sessions_table.Frame` comes out, and every assertion is
made against the plain text of its spans.
"""

from __future__ import annotations

import math
import unittest
from typing import Any

from claudewheel.lifecycle import HIDDEN_BY_DEFAULT_STATES, STATES
from claudewheel.sessions_table import (
    Frame,
    Line,
    SessionRow,
    layout,
    sort_rows,
    visible_rows,
)

NOW_MS = 1_786_536_700_326
HOME = "/home/m"


def _row(**overrides: Any) -> SessionRow:
    fields: dict[str, Any] = dict(
        session="4d97ca01-9d56-4f49-8047-77f5160febde",
        name="projects-9a",
        name_source="derived",
        state="idle",
        kind="interactive",
        cwd="/home/m/Projects",
        profile="work",
        version="2.1.226",
        model="opus",
        started_ms=NOW_MS - 3_600_000,
        rss_kib=None,
        pid=4242,
        current=False,
        config_dir="/home/m/.claudewheel/profiles/work",
        transcript=None,
        record=None,
        lifecycle=None,
    )
    fields.update(overrides)
    return SessionRow(**fields)


def _text(line: Line) -> str:
    return "".join(span.text for span in line)


def _lines(frame: Frame) -> list[str]:
    return [_text(line) for line in frame.lines]


def _layout(rows: list[SessionRow], **overrides: Any) -> Frame:
    kwargs: dict[str, Any] = dict(
        focus=0,
        expanded=None,
        height=14,
        width=100,
        hscroll=0,
        now_ms=NOW_MS,
        home=HOME,
        show_all=True,
    )
    kwargs.update(overrides)
    return layout(rows, **kwargs)


class FrameShapeTests(unittest.TestCase):
    def test_every_line_is_exactly_the_terminal_width(self) -> None:
        frame = _layout([_row(), _row(name="other")], width=100, height=14)
        for index, line in enumerate(_lines(frame)):
            self.assertEqual(len(line), 100, f"line {index}: {line!r}")

    def test_the_frame_leaves_the_last_terminal_row_to_the_hint(self) -> None:
        for height in (8, 14, 30):
            with self.subTest(height=height):
                frame = _layout([_row()], height=height)
                self.assertEqual(len(frame.lines), height - 1)

    def test_a_terminal_too_short_for_the_chrome_draws_what_fits(self) -> None:
        for height in (0, 1, 2, 3, 4, 5):
            with self.subTest(height=height):
                frame = _layout([_row()], height=height)
                self.assertEqual(len(frame.lines), max(0, height - 1))

    def test_a_narrow_terminal_still_produces_lines_of_its_own_width(self) -> None:
        for width in (1, 2, 3, 20):
            with self.subTest(width=width):
                frame = _layout([_row()], width=width)
                for line in _lines(frame):
                    self.assertEqual(len(line), width)

    def test_the_corners_and_joints_sit_where_the_columns_end(self) -> None:
        frame = _layout([_row()], width=200, height=14)
        lines = _lines(frame)
        top, header, rule = lines[0], lines[1], lines[2]
        bottom = lines[-1]
        self.assertTrue(top.startswith("╭"))
        self.assertTrue(top.endswith("╮"))
        self.assertTrue(bottom.startswith("╰"))
        self.assertTrue(bottom.endswith("╯"))
        self.assertTrue(header.startswith("│"))
        self.assertTrue(header.endswith("│"))
        # A wide-enough terminal shows every joint, at the same columns on the
        # top border, the header's separators, the rule and the bottom border.
        separators = [i for i, ch in enumerate(header) if ch == "│"]
        interior = separators[1:-1]
        self.assertTrue(interior)
        for column in interior:
            self.assertEqual(top[column], "┬")
            self.assertEqual(rule[column], "┼")
            self.assertEqual(bottom[column], "┴")
        self.assertTrue(rule.startswith("├"))
        self.assertTrue(rule.endswith("┤"))

    def test_the_header_names_the_columns_in_order(self) -> None:
        header = _lines(_layout([_row()], width=200))[1]
        labels = [cell.strip() for cell in header.strip("│").split("│")]
        self.assertEqual(
            labels,
            [
                "Name",
                "State",
                "Kind",
                "Directory",
                "Version",
                "Model",
                "Started",
                "MiB",
            ],
        )


class CellTests(unittest.TestCase):
    def _cells(self, row: SessionRow, **overrides: Any) -> list[str]:
        frame = _layout([row], width=200, **overrides)
        return [cell.strip() for cell in _lines(frame)[3].strip("│").split("│")]

    def test_the_home_prefix_is_written_as_a_tilde(self) -> None:
        self.assertIn("~/Projects", self._cells(_row(cwd="/home/m/Projects")))
        self.assertIn("~", self._cells(_row(cwd="/home/m")))
        self.assertIn("/etc", self._cells(_row(cwd="/etc")))

    def test_a_missing_cell_reads_as_a_dash(self) -> None:
        cells = self._cells(_row(cwd=None, version=None, model=None, rss_kib=None))
        self.assertEqual(cells[3], "-")
        self.assertEqual(cells[4], "-")
        self.assertEqual(cells[5], "-")
        self.assertEqual(cells[7], "-")

    def test_the_start_time_reads_as_an_age(self) -> None:
        self.assertEqual(self._cells(_row())[6], "1h 0m ago")
        self.assertEqual(self._cells(_row(started_ms=None))[6], "unknown")

    def test_memory_is_rounded_to_whole_mebibytes(self) -> None:
        self.assertEqual(self._cells(_row(rss_kib=1024))[7], "1")
        self.assertEqual(self._cells(_row(rss_kib=1536))[7], "2")
        self.assertEqual(self._cells(_row(rss_kib=524_288))[7], "512")

    def test_memory_is_right_aligned_in_its_column(self) -> None:
        rows = [_row(rss_kib=1024)]
        # At a width the strip exactly fills there is no padding after the last
        # column, so the cell's own alignment is what the line ends with.
        exact = _layout(rows, width=_layout(rows, width=200).strip_width + 2)
        self.assertTrue(_lines(exact)[3].endswith("│     1 │"))

    def test_the_current_session_carries_a_star(self) -> None:
        cells = self._cells(_row(current=True))
        self.assertEqual(cells[0], "* projects-9a")
        self.assertEqual(self._cells(_row())[0], "projects-9a")

    def test_a_long_name_truncates_on_the_right(self) -> None:
        cells = self._cells(_row(name="n" * 60))
        self.assertEqual(cells[0], "n" * 39 + "…")

    def test_a_long_directory_truncates_on_the_left(self) -> None:
        deep = "/home/m/" + "/".join(f"segment{i}" for i in range(10))
        cells = self._cells(_row(cwd=deep))
        self.assertTrue(cells[3].startswith("…"))
        self.assertEqual(len(cells[3]), 40)
        self.assertTrue(deep.replace("/home/m", "~").endswith(cells[3][1:]))

    def test_a_long_model_truncates_at_its_own_ceiling(self) -> None:
        cells = self._cells(_row(model="claude-opus-5-20260801-1m"))
        self.assertEqual(len(cells[5]), 24)
        self.assertTrue(cells[5].endswith("…"))


class HorizontalScrollTests(unittest.TestCase):
    def _wide(self) -> list[SessionRow]:
        """Rows whose columns add up to a strip of 129 columns."""
        return [
            _row(
                name="n" * 18,
                cwd="/home/m/" + "x" * 80,
                model="opus",
            )
        ]

    def test_the_strip_is_the_width_the_columns_ask_for(self) -> None:
        frame = _layout(self._wide(), width=96)
        self.assertEqual(frame.strip_width, 129)
        self.assertEqual(frame.interior_width, 94)

    def test_the_handle_covers_the_visible_fraction_of_the_strip(self) -> None:
        frame = _layout(self._wide(), width=96, hscroll=0)
        bottom = _text(frame.lines[-1])
        handle = bottom.count("━")
        self.assertEqual(handle, math.ceil(94 * 94 / 129))
        self.assertEqual(handle, 69)
        self.assertEqual(bottom.index("━"), 1)

    def test_the_handle_moves_with_the_scroll_and_never_leaves_the_track(
        self,
    ) -> None:
        rows = self._wide()
        for hscroll in range(0, 60):
            with self.subTest(hscroll=hscroll):
                frame = _layout(rows, width=96, hscroll=hscroll)
                bottom = _text(frame.lines[-1])
                first = bottom.index("━")
                length = bottom.count("━")
                self.assertGreaterEqual(length, 1)
                self.assertGreaterEqual(first, 1)
                self.assertLessEqual(first + length, 95)

    def test_scrolling_shifts_the_header_and_the_rows_together(self) -> None:
        rows = self._wide()
        flush = _lines(_layout(rows, width=96, hscroll=0))
        shifted = _lines(_layout(rows, width=96, hscroll=8))
        self.assertNotEqual(flush[1], shifted[1])
        self.assertEqual(flush[1][9:-1], shifted[1][1:-9])
        self.assertEqual(flush[3][9:-1], shifted[3][1:-9])

    def test_the_scroll_is_clamped_to_what_the_strip_can_offer(self) -> None:
        rows = self._wide()
        far = _layout(rows, width=96, hscroll=9999)
        self.assertEqual(far.hscroll, 129 - 94)
        self.assertEqual(_lines(far), _lines(_layout(rows, width=96, hscroll=35)))
        behind = _layout(rows, width=96, hscroll=-5)
        self.assertEqual(behind.hscroll, 0)

    def test_a_strip_that_fits_has_a_plain_bottom_border(self) -> None:
        frame = _layout([_row()], width=200)
        bottom = _text(frame.lines[-1])
        self.assertNotIn("━", bottom)
        self.assertIn("┴", bottom)


class VerticalScrollTests(unittest.TestCase):
    def _many(self, count: int = 20) -> list[SessionRow]:
        return [
            _row(name=f"row-{i:02d}", started_ms=NOW_MS - i * 1000)
            for i in range(count)
        ]

    def _right_border(self, frame: Frame) -> str:
        """The rightmost character of each data-window line."""
        window = frame.lines[3:-1]
        return "".join(_text(line)[-1] for line in window)

    def test_the_handle_covers_the_visible_fraction_of_the_column(self) -> None:
        frame = _layout(self._many(), height=13, focus=0)
        self.assertEqual(frame.window, 8)
        self.assertEqual(frame.total_lines, 20)
        border = self._right_border(frame)
        self.assertEqual(border, "┃┃┃┃││││")

    def test_the_handle_sits_at_the_end_when_the_last_row_is_focused(self) -> None:
        frame = _layout(self._many(), height=13, focus=19)
        self.assertEqual(self._right_border(frame), "││││┃┃┃┃")

    def test_the_handle_is_never_shorter_than_one_line(self) -> None:
        frame = _layout(self._many(400), height=13, focus=0)
        border = self._right_border(frame)
        self.assertEqual(border.count("┃"), 1)

    def test_the_handle_never_runs_past_the_track(self) -> None:
        rows = self._many(37)
        for focus in range(37):
            with self.subTest(focus=focus):
                border = self._right_border(_layout(rows, height=13, focus=focus))
                self.assertEqual(len(border), 8)
                self.assertGreaterEqual(border.count("┃"), 1)
                self.assertEqual(border.strip("│").strip("┃"), "")

    def test_a_column_that_fits_has_a_plain_right_border(self) -> None:
        frame = _layout(self._many(3), height=13, focus=0)
        self.assertEqual(self._right_border(frame), "││││││││")

    def test_the_focused_row_is_inside_the_window(self) -> None:
        rows = self._many()
        for focus in (0, 7, 12, 19):
            with self.subTest(focus=focus):
                frame = _layout(rows, height=13, focus=focus)
                window = [_text(line) for line in frame.lines[3:-1]]
                self.assertTrue(
                    any(rows[focus].name in line for line in window),
                    window,
                )


class ExpandedRowTests(unittest.TestCase):
    def _window(self, frame: Frame) -> list[str]:
        return [_text(line)[1:-1] for line in frame.lines[3:-1]]

    def test_the_expanded_row_is_followed_by_its_three_detail_lines(self) -> None:
        frame = _layout(
            [_row(rss_kib=2048), _row(name="second")],
            width=200,
            expanded=0,
            focus=0,
        )
        window = self._window(frame)
        self.assertIn("projects-9a", window[0])
        self.assertEqual(
            window[1].strip(),
            "session 4d97ca01-9d56-4f49-8047-77f5160febde",
        )
        self.assertEqual(
            window[2].strip(),
            "pid 4242 · profile work · name source derived · "
            "config ~/.claudewheel/profiles/work",
        )
        self.assertEqual(window[3].strip(), "transcript -")
        self.assertIn("second", window[4])

    def test_the_detail_lines_say_what_is_unknown(self) -> None:
        frame = _layout(
            [
                _row(
                    session=None,
                    pid=None,
                    profile=None,
                    name_source=None,
                    config_dir=None,
                    transcript="/home/m/.claude/projects/x/y.jsonl",
                )
            ],
            width=200,
            expanded=0,
        )
        window = self._window(frame)
        self.assertEqual(window[1].strip(), "session unknown")
        self.assertEqual(
            window[2].strip(),
            "pid - · profile - · name source - · config -",
        )
        self.assertEqual(window[3].strip(), "transcript ~/.claude/projects/x/y.jsonl")

    def test_the_detail_lines_are_clipped_rather_than_scrolled(self) -> None:
        """They are not part of the column strip, so hscroll leaves them alone."""
        rows = [_row(name="n" * 18, cwd="/home/m/" + "x" * 80)]
        flush = self._window(_layout(rows, width=96, expanded=0, hscroll=0))
        shifted = self._window(_layout(rows, width=96, expanded=0, hscroll=20))
        self.assertEqual(flush[1], shifted[1])
        self.assertEqual(len(flush[1]), 94)
        self.assertTrue(flush[1].startswith("  session "))

    def test_an_expanded_row_is_four_lines_tall_for_the_viewport(self) -> None:
        rows = [_row(name=f"row-{i}") for i in range(6)]
        frame = _layout(rows, height=13, expanded=0, focus=0)
        self.assertEqual(frame.total_lines, 6 + 3)


class EmptyTests(unittest.TestCase):
    def test_an_empty_table_still_draws_its_frame(self) -> None:
        frame = _layout([], width=60, height=14)
        lines = _lines(frame)
        self.assertEqual(len(lines), 13)
        self.assertTrue(lines[0].startswith("╭"))
        self.assertIn("No sessions.", lines[3])
        self.assertEqual({len(line) for line in lines}, {60})

    def test_the_empty_line_carries_its_own_style(self) -> None:
        frame = _layout([], width=60)
        styles = {span.style for span in frame.lines[3][1:-1]}
        self.assertEqual(styles, {"empty"})


class StyleTests(unittest.TestCase):
    def test_the_focused_row_carries_the_focus_style_across_its_whole_width(
        self,
    ) -> None:
        frame = _layout([_row(state="working"), _row(name="other")], focus=0, width=100)
        focused = frame.lines[3]
        self.assertEqual(len(_text(focused)), 100)
        inner = focused[1:-1]
        self.assertEqual(sum(len(span.text) for span in inner), 98)
        self.assertEqual(
            {span.style for span in inner},
            {"row_focus", "state_focus:working"},
        )

    def test_an_unfocused_row_carries_the_plain_styles(self) -> None:
        frame = _layout([_row(), _row(name="other", state="crashed")], focus=0)
        inner = frame.lines[4][1:-1]
        self.assertEqual({span.style for span in inner}, {"row", "state:crashed"})

    def test_the_borders_and_the_header_carry_their_own_styles(self) -> None:
        frame = _layout([_row()], width=100)
        self.assertEqual({span.style for span in frame.lines[0]}, {"frame"})
        self.assertEqual({span.style for span in frame.lines[-1]}, {"frame"})
        self.assertEqual({span.style for span in frame.lines[1]}, {"frame", "header"})

    def test_every_state_gets_its_own_style_name(self) -> None:
        for state in STATES:
            with self.subTest(state=state):
                frame = _layout([_row(state=state)], focus=-1)
                styles = {span.style for span in frame.lines[3]}
                self.assertIn(f"state:{state}", styles)


class SortAndFilterTests(unittest.TestCase):
    def test_states_are_grouped_live_then_loose_then_finished(self) -> None:
        states = [
            "exited",
            "done",
            "crashed",
            "blocked",
            "on-hold",
            "starting",
            "idle",
        ]
        rows = [_row(name=state, state=state, started_ms=None) for state in states]
        # on-hold and blocked share one group, so between those two it is the
        # within-group order (here: the name) that decides.
        self.assertEqual(
            [row.state for row in sort_rows(rows)],
            ["idle", "starting", "blocked", "on-hold", "crashed", "done", "exited"],
        )

    def test_on_hold_and_blocked_share_a_group_and_sort_by_age(self) -> None:
        rows = [
            _row(name="b", state="blocked", started_ms=200),
            _row(name="h", state="on-hold", started_ms=300),
        ]
        self.assertEqual([row.name for row in sort_rows(rows)], ["h", "b"])

    def test_the_newest_session_in_a_group_comes_first(self) -> None:
        rows = [
            _row(name="old", state="idle", started_ms=100),
            _row(name="new", state="working", started_ms=300),
            _row(name="mid", state="idle", started_ms=200),
        ]
        self.assertEqual([row.name for row in sort_rows(rows)], ["new", "mid", "old"])

    def test_a_session_with_no_start_time_sorts_last_in_its_group(self) -> None:
        rows = [
            _row(name="nameless", state="idle", started_ms=None),
            _row(name="dated", state="idle", started_ms=1),
        ]
        self.assertEqual([row.name for row in sort_rows(rows)], ["dated", "nameless"])

    def test_rows_of_the_same_age_sort_by_name(self) -> None:
        rows = [
            _row(name="zeta", state="idle", started_ms=5),
            _row(name="alpha", state="idle", started_ms=5),
        ]
        self.assertEqual([row.name for row in sort_rows(rows)], ["alpha", "zeta"])

    def test_finished_sessions_are_hidden_until_asked_for(self) -> None:
        rows = [_row(name=state, state=state) for state in STATES]
        shown = {row.state for row in visible_rows(rows, False)}
        self.assertEqual(shown, set(STATES) - HIDDEN_BY_DEFAULT_STATES)
        self.assertEqual({row.state for row in visible_rows(rows, True)}, set(STATES))

    def test_the_layout_filters_with_the_same_rule(self) -> None:
        rows = [_row(name="here", state="idle"), _row(name="gone", state="done")]
        self.assertNotIn("gone", "".join(_lines(_layout(rows, show_all=False))))
        self.assertIn("gone", "".join(_lines(_layout(rows, show_all=True))))


if __name__ == "__main__":
    unittest.main()
