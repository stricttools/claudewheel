"""Tests for the sessions overview screen and its key loop.

No registry directory is read and no ``ps`` is spawned: the two seams the
screen gathers through (:mod:`claudewheel.session_registry` and
:mod:`claudewheel.processes`) are patched, and the loop is driven through the
shared FakeTerminal's recorded keystrokes.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import sessions_overview as so
from claudewheel.defaults import DEFAULT_THEME_DARK
from claudewheel.session_registry import SessionRecord
from claudewheel.session_rows import CURRENT_MARK, SessionIdentity
from claudewheel.theme import ThemeColors, parse_theme

from .wheelhelpers import FakeTerminal

STARTED_AT = 1_786_536_700_326
NOW_MS = STARTED_AT + 3_600_000

CONFIG_DIR = Path("/nowhere/profiles/work")


def _theme() -> ThemeColors:
    return parse_theme(DEFAULT_THEME_DARK)


def _record(pid: int, *, live: bool = True, **overrides: Any) -> SessionRecord:
    fields: dict[str, Any] = dict(
        path=CONFIG_DIR / "sessions" / f"{pid}.json",
        pid=pid,
        kind="interactive",
        live=live,
        session_id=f"session-{pid}",
        cwd="/home/m/Projects",
        status="idle",
        name=f"row-{pid}",
        version="2.1.226",
        started_at=STARTED_AT,
        proc_start="1",
    )
    fields.update(overrides)
    return SessionRecord(**fields)


@contextmanager
def _registry(
    *readings: Sequence[SessionRecord], memory: dict[int, int] | None = None
) -> Iterator[dict[str, Any]]:
    """Patch the two gather seams; each reading answers one registry read.

    The last reading answers every further read, so a test that only cares
    about one snapshot passes one.
    """
    answers = [list(r) for r in readings] or [[]]

    def read(config_dir: Path) -> list[SessionRecord]:
        index = min(read_records.call_count - 1, len(answers) - 1)
        return list(answers[index])

    with (
        mock.patch(
            "claudewheel.sessions_overview.session_registry.read_records",
            autospec=True,
            side_effect=read,
        ) as read_records,
        mock.patch(
            "claudewheel.sessions_overview.processes.resident_memory",
            autospec=True,
            return_value=dict(memory or {}),
        ) as resident_memory,
    ):
        yield {"read_records": read_records, "resident_memory": resident_memory}


class _Clock:
    """A clock that only moves when it is asked, so a test can see who asked."""

    def __init__(self, start: int = NOW_MS, step: int = 60_000) -> None:
        self.now = start
        self.step = step
        self.calls = 0

    def __call__(self) -> int:
        value = self.now + self.step * self.calls
        self.calls += 1
        return value


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: The cursor move that opens every drawn line (``move_to(row, col)``).
_MOVE = re.compile(r"\x1b\[\d+;\d+H")


def _frame_lines(frame: str) -> list[str]:
    """The visible text of one rendered frame, one entry per drawn line."""
    return [_ANSI.sub("", chunk) for chunk in _MOVE.split(frame)[1:]]


def _focused_name(frame: str) -> str | None:
    """The name on the expanded (focused) block of *frame*, if one is drawn.

    The expanded block is the only one carrying a ``cwd`` line, and its header
    is the line above it.
    """
    lines = _frame_lines(frame)
    for index, line in enumerate(lines):
        if line.strip().startswith("cwd ") and index:
            return lines[index - 1].strip().split()[0]
    return None


def _selection_path(terminal: FakeTerminal) -> list[str]:
    """The focused row's name in each frame the screen drew."""
    return [
        name
        for name in (_focused_name(frame) for frame in terminal.output)
        if name is not None
    ]


def _rows(terminal: FakeTerminal) -> list[str]:
    """Every drawn line of the last frame."""
    return _frame_lines(terminal.output[-1])


class SnapshotTests(unittest.TestCase):
    def test_every_parseable_record_is_a_row_live_or_not(self) -> None:
        with _registry([_record(1), _record(2, live=False)]):
            snapshot = so.take_snapshot(CONFIG_DIR, clock=_Clock())
        self.assertEqual([row.record.pid for row in snapshot.rows], [1, 2])

    def test_memory_is_measured_once_and_only_for_live_pids(self) -> None:
        with _registry(
            [_record(1), _record(2, live=False), _record(3)], memory={1: 2048}
        ) as seams:
            snapshot = so.take_snapshot(CONFIG_DIR, clock=_Clock())
        seams["resident_memory"].assert_called_once_with([1, 3])
        self.assertEqual([row.rss_kib for row in snapshot.rows], [2048, None, None])

    def test_the_snapshot_carries_the_clock_it_was_read_at(self) -> None:
        clock = _Clock()
        with _registry([_record(1)]):
            snapshot = so.take_snapshot(CONFIG_DIR, clock=clock)
        self.assertEqual(snapshot.now_ms, NOW_MS)
        self.assertEqual(clock.calls, 1)

    def test_the_overview_asks_for_no_selector_column(self) -> None:
        with _registry([_record(1)]):
            snapshot = so.take_snapshot(CONFIG_DIR, clock=_Clock())
        self.assertIsNone(snapshot.rows[0].selected)
        self.assertIsNone(snapshot.rows[0].selector)


class RefocusTests(unittest.TestCase):
    def _rows_for(self, *pids: int) -> list[Any]:
        with _registry([_record(pid) for pid in pids]):
            return list(so.take_snapshot(CONFIG_DIR, clock=_Clock()).rows)

    def test_the_focus_follows_its_pid_when_the_list_shifts(self) -> None:
        before = self._rows_for(1, 2, 3)
        after = self._rows_for(2, 3)
        self.assertEqual(so.refocus(before, 2, after), 1)

    def test_a_vanished_pid_clamps_the_index(self) -> None:
        before = self._rows_for(1, 2, 3)
        after = self._rows_for(1)
        self.assertEqual(so.refocus(before, 2, after), 0)

    def test_an_empty_list_has_no_focus(self) -> None:
        self.assertEqual(so.refocus(self._rows_for(1), 0, []), -1)


class KeyLoopTests(unittest.TestCase):
    """The scripted key sequences, and where they leave the selection."""

    def _run(
        self,
        keys: list[str],
        *readings: Sequence[SessionRecord],
        identity: SessionIdentity | None = None,
        rows: int = 40,
        memory: dict[int, int] | None = None,
    ) -> tuple[so.OverviewOutcome, FakeTerminal, dict[str, Any]]:
        terminal = FakeTerminal(keys)
        terminal.rows = rows
        with _registry(*readings, memory=memory) as seams:
            outcome = so.run_overview(
                CONFIG_DIR,
                profile_name="work",
                theme=_theme(),
                terminal=terminal,
                clock=_Clock(),
                identity=identity,
            )
        return outcome, terminal, seams

    def test_a_scripted_sequence_walks_the_expected_selection_path(self) -> None:
        outcome, terminal, _ = self._run(
            ["DOWN", "DOWN", "UP", "ESC"], [_record(1), _record(2), _record(3)]
        )
        # One frame when the screen opens, one per key that did not close it.
        self.assertEqual(
            _selection_path(terminal),
            ["row-1", "row-2", "row-3", "row-2"],
        )
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 2)

    def test_the_focus_clamps_at_both_ends(self) -> None:
        outcome, terminal, _ = self._run(["UP", "UP", "ESC"], [_record(1), _record(2)])
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 1)

        outcome, terminal, _ = self._run(
            ["DOWN", "DOWN", "DOWN", "ESC"], [_record(1), _record(2)]
        )
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 2)

    def test_moving_the_focus_never_re_reads_the_registry(self) -> None:
        """Snapshot semantics: only the refresh key reads again."""
        _, _, seams = self._run(["DOWN", "UP", "DOWN", "ESC"], [_record(1), _record(2)])
        self.assertEqual(seams["read_records"].call_count, 1)
        self.assertEqual(seams["resident_memory"].call_count, 1)

    def test_a_new_session_appears_only_after_the_refresh_key(self) -> None:
        keys = ["DOWN", "r", "DOWN", "ESC"]
        outcome, terminal, seams = self._run(
            keys, [_record(1)], [_record(1), _record(2)]
        )
        self.assertEqual(seams["read_records"].call_count, 2)
        self.assertEqual(outcome.refreshes, 1)
        # The second DOWN reaches the row the refresh brought in.
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 2)

    def test_a_refresh_keeps_the_focus_on_the_same_session(self) -> None:
        outcome, _, _ = self._run(
            ["DOWN", "DOWN", "r", "ESC"],
            [_record(1), _record(2), _record(3)],
            [_record(2), _record(3)],
        )
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 3)

    def test_uptime_is_frozen_between_refreshes(self) -> None:
        """The clock is read per snapshot, not per frame."""
        terminal = FakeTerminal(["DOWN", "DOWN", "r", "ESC"])
        clock = _Clock()
        with _registry([_record(1), _record(2)]):
            so.run_overview(
                CONFIG_DIR,
                profile_name="work",
                theme=_theme(),
                terminal=terminal,
                clock=clock,
            )
        self.assertEqual(clock.calls, 2)

    def test_an_unbound_key_does_nothing(self) -> None:
        outcome, _, seams = self._run(["z", "ESC"], [_record(1), _record(2)])
        self.assertEqual(seams["read_records"].call_count, 1)
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 1)

    def test_every_close_key_leaves(self) -> None:
        for key in sorted(so.CLOSE_KEYS):
            with self.subTest(key=key):
                # A second key would only be read if the first did not close.
                outcome, _, seams = self._run([key, "DOWN"], [_record(1), _record(2)])
                self.assertEqual(outcome.focused.pid if outcome.focused else None, 1)
                self.assertEqual(seams["read_records"].call_count, 1)

    def test_ctrl_c_at_the_terminal_leaves_too(self) -> None:
        terminal = FakeTerminal([])
        terminal.read_key = mock.Mock(side_effect=KeyboardInterrupt)  # type: ignore[method-assign]
        with _registry([_record(1)]):
            outcome = so.run_overview(
                CONFIG_DIR,
                profile_name="work",
                theme=_theme(),
                terminal=terminal,
                clock=_Clock(),
            )
        self.assertEqual(outcome.focused.pid if outcome.focused else None, 1)

    def test_an_empty_registry_draws_its_own_line(self) -> None:
        outcome, terminal, _ = self._run(["DOWN", "ESC"], [])
        self.assertIsNone(outcome.focused)
        self.assertIn(so._EMPTY, "".join(terminal.output))

    def test_the_title_names_the_profile_and_the_hint_names_refresh(self) -> None:
        _, terminal, _ = self._run(["ESC"], [_record(1)])
        drawn = "".join(terminal.output)
        self.assertIn("Sessions under 'work'", drawn)
        self.assertIn("r: refresh", drawn)

    def test_a_stale_record_is_listed_and_marked(self) -> None:
        _, terminal, _ = self._run(["ESC"], [_record(1), _record(2, live=False)])
        self.assertIn("stale", "".join(terminal.output))

    def test_the_current_session_is_marked(self) -> None:
        identity = SessionIdentity(session_id="session-2", pid=2)
        _, terminal, _ = self._run(["ESC"], [_record(1), _record(2)], identity=identity)
        marked = [
            line for line in _rows(terminal) if line.strip().startswith(CURRENT_MARK)
        ]
        self.assertEqual(len(marked), 1)
        self.assertIn("row-2", marked[0])


class ShortTerminalTests(unittest.TestCase):
    """A window too short for the list, and even for the chrome."""

    def _draw(self, rows: int, count: int = 12) -> FakeTerminal:
        terminal = FakeTerminal(["DOWN", "DOWN", "ESC"])
        terminal.rows = rows
        terminal.cols = 80
        with _registry([_record(1000 + i) for i in range(count)]):
            so.run_overview(
                CONFIG_DIR,
                profile_name="work",
                theme=_theme(),
                terminal=terminal,
                clock=_Clock(),
            )
        return terminal

    def _max_row_written(self, terminal: FakeTerminal) -> int:
        rows = [
            int(m.group(1))
            for m in re.finditer(r"\x1b\[(\d+);\d+H", "".join(terminal.output))
        ]
        return max(rows) if rows else 0

    def test_a_short_terminal_neither_raises_nor_writes_past_its_last_row(
        self,
    ) -> None:
        for height in (1, 2, 4, 5, 10, 24):
            with self.subTest(height=height):
                terminal = self._draw(height)
                self.assertLessEqual(self._max_row_written(terminal), height)

    def test_a_zero_row_terminal_draws_nothing_at_all(self) -> None:
        terminal = self._draw(0)
        self.assertEqual(self._max_row_written(terminal), 0)


if __name__ == "__main__":
    unittest.main()
