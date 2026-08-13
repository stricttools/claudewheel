"""Tests for the list component both session screens draw with.

The frame builder is pure -- rows, dimensions and a clock in, styled lines out
-- so every assertion here is about arithmetic and text, with no terminal.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from claudewheel.session_list import (
    PARTIAL_CLIP,
    PARTIAL_HIDE,
    SELECTOR_OFF,
    SELECTOR_ON,
    STYLE_HINT,
    STYLE_ROW,
    STYLE_TITLE,
    ListRow,
    build_frame,
    move_focus,
    row_heights,
)
from claudewheel.session_registry import SessionRecord
from claudewheel.session_rows import CURRENT_MARK

STARTED_AT = 1_786_536_700_326
NOW_MS = STARTED_AT + 3_600_000


def _record(pid: int = 1000, **overrides: object) -> SessionRecord:
    fields: dict[str, object] = dict(
        path=Path(f"/tmp/sessions/{pid}.json"),
        pid=pid,
        kind="interactive",
        live=True,
        session_id=f"session-{pid}",
        cwd="/home/m/Projects",
        status="idle",
        name=f"row-{pid}",
        version="2.1.226",
        started_at=STARTED_AT,
        proc_start="1",
    )
    fields.update(overrides)
    return SessionRecord(**fields)  # type: ignore[arg-type]


def _rows(count: int, **row_kwargs: object) -> list[ListRow]:
    return [
        ListRow(record=_record(1000 + i), **row_kwargs)  # type: ignore[arg-type]
        for i in range(count)
    ]


class RowHeightTests(unittest.TestCase):
    def test_the_focused_row_is_the_tall_one(self) -> None:
        rows = _rows(3, state="running")
        heights = row_heights(rows, focus=1, now_ms=NOW_MS)
        self.assertEqual(heights, [3, 5, 3])

    def test_without_a_state_a_collapsed_row_is_two_lines(self) -> None:
        heights = row_heights(_rows(2), focus=0, now_ms=NOW_MS)
        self.assertEqual(heights, [5, 2])

    def test_a_focus_naming_no_row_leaves_every_row_collapsed(self) -> None:
        heights = row_heights(_rows(2, state="running"), focus=-1, now_ms=NOW_MS)
        self.assertEqual(heights, [3, 3])


class FrameTests(unittest.TestCase):
    def test_the_title_leads_and_the_hint_trails(self) -> None:
        frame = build_frame(
            _rows(2, state="running"),
            focus=0,
            now_ms=NOW_MS,
            title="Holding 'work'",
            hint="space: toggle",
            height=20,
            width=80,
        )
        self.assertEqual(frame[0].style, STYLE_TITLE)
        self.assertIn("Holding 'work'", frame[0].text)
        self.assertEqual(frame[-1].style, STYLE_HINT)
        self.assertIn("space: toggle", frame[-1].text)

    def test_every_row_appears_when_they_all_fit(self) -> None:
        frame = build_frame(
            _rows(3, state="running"),
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=40,
            width=80,
        )
        text = "\n".join(line.text for line in frame)
        for i in range(3):
            self.assertIn(f"row-{1000 + i}", text)

    def test_a_row_scrolled_out_of_the_window_is_absent(self) -> None:
        """Ten rows in a window with space for two blocks: the last is gone."""
        frame = build_frame(
            _rows(10, state="running"),
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=12,
            width=80,
        )
        text = "\n".join(line.text for line in frame)
        self.assertIn("row-1000", text)
        self.assertNotIn("row-1009", text)

    def test_scrolling_follows_the_focus(self) -> None:
        """Focusing the last row brings it into the window and drops the first."""
        frame = build_frame(
            _rows(10, state="running"),
            focus=9,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=12,
            width=80,
        )
        text = "\n".join(line.text for line in frame)
        self.assertIn("row-1009", text)
        self.assertNotIn("row-1000", text)

    def test_a_clipped_row_contributes_only_its_visible_lines(self) -> None:
        """The window never draws past its own last line."""
        rows = _rows(6, state="running")
        for height in range(6, 20):
            with self.subTest(height=height):
                frame = build_frame(
                    rows,
                    focus=2,
                    now_ms=NOW_MS,
                    title="t",
                    hint="h",
                    height=height,
                    width=80,
                )
                self.assertLessEqual(len(frame), height)

    def test_a_terminal_too_short_for_anything_still_renders(self) -> None:
        frame = build_frame(
            _rows(3, state="running"),
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=2,
            width=80,
        )
        self.assertLessEqual(len(frame), 2)

    def test_an_empty_list_says_so(self) -> None:
        frame = build_frame(
            [],
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=20,
            width=80,
            empty_text="Nothing holds this profile.",
        )
        text = "\n".join(line.text for line in frame)
        self.assertIn("Nothing holds this profile.", text)

    def test_lines_are_truncated_to_the_width(self) -> None:
        frame = build_frame(
            _rows(1, state="running"),
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=20,
            width=24,
        )
        for line in frame:
            self.assertLessEqual(len(line.text), 24)

    def test_the_selector_reflects_the_tick(self) -> None:
        rows = [
            ListRow(record=_record(1), selected=True, state="running"),
            ListRow(record=_record(2), selected=False, state="running"),
        ]
        frame = build_frame(
            rows,
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=30,
            width=80,
        )
        text = "\n".join(line.text for line in frame)
        self.assertIn(SELECTOR_ON, text)
        self.assertIn(SELECTOR_OFF, text)

    def test_a_row_with_no_selector_draws_none(self) -> None:
        frame = build_frame(
            _rows(1),
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=30,
            width=80,
        )
        text = "\n".join(line.text for line in frame)
        self.assertNotIn(SELECTOR_ON, text)
        self.assertNotIn(SELECTOR_OFF, text)

    def test_the_state_line_carries_the_style_the_row_declared(self) -> None:
        rows = [
            ListRow(record=_record(1), state="stopped", state_style="stopped"),
        ]
        frame = build_frame(
            rows,
            focus=-1,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=30,
            width=80,
        )
        styled = [line for line in frame if line.style == "stopped"]
        self.assertEqual(len(styled), 1)
        self.assertIn("stopped", styled[0].text)

    def test_row_lines_without_a_state_are_plain_rows(self) -> None:
        frame = build_frame(
            _rows(1),
            focus=-1,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=30,
            width=80,
        )
        row_lines = [line for line in frame if line.style == STYLE_ROW]
        self.assertEqual(len(row_lines), 2)

    def test_memory_is_drawn_when_it_was_measured(self) -> None:
        rows = [ListRow(record=_record(1), rss_kib=2048, state="running")]
        frame = build_frame(
            rows,
            focus=0,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=30,
            width=120,
        )
        text = "\n".join(line.text for line in frame)
        self.assertIn("2.0 MiB", text)

    def test_the_current_session_is_marked(self) -> None:
        from claudewheel.session_rows import SessionIdentity

        rows = [ListRow(record=_record(77), state="running")]
        frame = build_frame(
            rows,
            focus=-1,
            now_ms=NOW_MS,
            title="t",
            hint="h",
            height=30,
            width=120,
            identity=SessionIdentity(session_id="session-77", pid=77),
        )
        text = "\n".join(line.text for line in frame)
        self.assertIn(CURRENT_MARK, text)


class PartialRowTests(unittest.TestCase):
    """The two things a screen can do with a row only partly in the window.

    Both are built so they can be compared on a real 24-row terminal; these
    tests pin what each one actually does to the same content.
    """

    def _frame(self, behaviour: str, *, focus: int, height: int = 24) -> list[str]:
        return [
            line.text
            for line in build_frame(
                _rows(10),
                focus=focus,
                now_ms=NOW_MS,
                title="Sessions under 'work'",
                hint="up/down: move   q/esc: close",
                height=height,
                width=78,
                partial_rows=behaviour,
            )
        ]

    def test_both_fill_the_same_number_of_lines(self) -> None:
        """Hiding a partial row blanks its lines rather than shifting the hint."""
        for focus in (0, 4, 9):
            with self.subTest(focus=focus):
                self.assertEqual(
                    len(self._frame(PARTIAL_CLIP, focus=focus)),
                    len(self._frame(PARTIAL_HIDE, focus=focus)),
                )

    def test_clip_draws_the_lines_that_fit(self) -> None:
        """At the bottom edge that is a header with no detail line under it."""
        frame = self._frame(PARTIAL_CLIP, focus=0)
        self.assertIn("row-1008", "\n".join(frame))
        self.assertNotIn("row-1009", "\n".join(frame))

    def test_hide_leaves_a_partly_visible_row_out(self) -> None:
        frame = self._frame(PARTIAL_HIDE, focus=0)
        self.assertNotIn("row-1008", "\n".join(frame))
        self.assertNotIn("row-1009", "\n".join(frame))

    def test_clip_can_orphan_a_detail_line_at_the_top_edge(self) -> None:
        """The line whose header is above the window, drawn with no owner."""
        frame = self._frame(PARTIAL_CLIP, focus=9)
        first_row_line = frame[2]
        self.assertTrue(first_row_line.strip().startswith("/home"))

    def test_hide_never_draws_a_line_whose_header_is_off_screen(self) -> None:
        frame = self._frame(PARTIAL_HIDE, focus=9)
        self.assertEqual(frame[2], "")

    def test_hide_still_draws_the_focused_row_when_it_cannot_fit(self) -> None:
        """Hiding it would leave a window with nothing in it at all."""
        frame = self._frame(PARTIAL_HIDE, focus=0, height=7)
        self.assertIn("row-1000", "\n".join(frame))

    def test_an_unknown_behaviour_is_a_hard_error(self) -> None:
        with self.assertRaises(ValueError):
            self._frame("shrink", focus=0)


class FocusMovementTests(unittest.TestCase):
    def test_moving_within_the_list(self) -> None:
        self.assertEqual(move_focus(0, 3, 1), 1)
        self.assertEqual(move_focus(2, 3, -1), 1)

    def test_both_ends_clamp(self) -> None:
        self.assertEqual(move_focus(0, 3, -1), 0)
        self.assertEqual(move_focus(2, 3, 1), 2)

    def test_an_empty_list_has_no_focus(self) -> None:
        self.assertEqual(move_focus(0, 0, 1), -1)


if __name__ == "__main__":
    unittest.main()
