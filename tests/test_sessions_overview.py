"""Tests for the machine-wide sessions screen: its gathering and its key loop.

The gathering runs against real temporary directories -- real registry files
under real profile directories, real lifecycle files in the shared store, the
real liveness probe -- because joining those two stores IS what the code under
test does, and a mocked registry would leave the join untested.  The one patched
seam is :func:`claudewheel.processes.resident_memory`, which would otherwise
spawn ``ps``.

The key loop is driven through the shared FakeTerminal's recorded keystrokes and
read back out of the frames it captured.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import lifecycle
from claudewheel import sessions_overview as so
from claudewheel import sessions_table
from claudewheel.constants import BOLD, DIM
from claudewheel.defaults import DEFAULT_THEME_DARK
from claudewheel.lifecycle import (
    STATES,
    SWEEP_GRACE_MS,
    MarkEvent,
    NamedEvent,
    StartedEvent,
    now_timestamp,
)
from claudewheel.session_rows import SessionIdentity
from claudewheel.theme import ThemeColors, parse_theme
from claudewheel.workspace import Workspace

from .wheelhelpers import FakeTerminal, dead_pid, live_record, stale_record

#: A fixed wall clock: 2026-09-13 01:02:03.456 UTC, in milliseconds.
NOW_MS = 1_789_261_323_456

LIVE_SESSION = "4d97ca01-9d56-4f49-8047-77f5160febde"
STALE_SESSION = "aaaaaaaa-1111-4111-8111-111111111111"
STORE_SESSION = "bbbbbbbb-2222-4222-8222-222222222222"
OTHER_SESSION = "cccccccc-3333-4333-8333-333333333333"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: The cursor move that opens every drawn line (``move_to(row, col)``).
_MOVE = re.compile(r"\x1b\[\d+;\d+H")


def _theme() -> ThemeColors:
    return parse_theme(DEFAULT_THEME_DARK)


def _frame_lines(frame: str) -> list[str]:
    """The visible text of one rendered frame, one entry per drawn line."""
    return [_ANSI.sub("", chunk) for chunk in _MOVE.split(frame)[1:]]


def _raw_lines(frame: str) -> list[str]:
    """The drawn lines of one frame, escape sequences included."""
    return _MOVE.split(frame)[1:]


class _Clock:
    """A clock that only moves when it is asked, so a test can see who asked."""

    def __init__(self, start: int = NOW_MS, step: int = 0) -> None:
        self.now = start
        self.step = step
        self.calls = 0

    def __call__(self) -> int:
        value = self.now + self.step * self.calls
        self.calls += 1
        return value


class StyleSequenceTests(unittest.TestCase):
    """The one place a style name from the table becomes colour."""

    def setUp(self) -> None:
        self.theme = _theme()

    def test_the_waiting_state_is_drawn_bold(self) -> None:
        for state in sorted(so.BOLD_STATES):
            with self.subTest(state=state):
                self.assertTrue(
                    so.style_sequence(self.theme, f"state:{state}").startswith(BOLD)
                )

    def test_a_parked_or_finished_state_is_drawn_dim(self) -> None:
        for state in sorted(so.DIM_STATES):
            with self.subTest(state=state):
                self.assertTrue(
                    so.style_sequence(self.theme, f"state:{state}").startswith(DIM)
                )

    def test_a_focused_state_opens_with_the_focus_background(self) -> None:
        for state in sorted(STATES):
            with self.subTest(state=state):
                sequence = so.style_sequence(self.theme, f"state_focus:{state}")
                self.assertTrue(sequence.startswith(self.theme.sessions_focus_bg))

    def test_the_empty_table_carries_the_detail_colour(self) -> None:
        self.assertEqual(
            so.style_sequence(self.theme, sessions_table.STYLE_EMPTY),
            self.theme.sessions_detail_fg,
        )

    def test_an_unknown_style_is_refused_rather_than_coloured(self) -> None:
        with self.assertRaisesRegex(ValueError, "row_focs"):
            so.style_sequence(self.theme, "row_focs")


class WorkspaceCase(unittest.TestCase):
    """A case owning one temporary workspace: profiles, registries, the store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.ws = Workspace.open(root / ".claudewheel", claude_dir=self.claude_dir)
        self.lifecycle_dir = self.ws.shared.lifecycle_dir
        self.addCleanup(self._tmp.cleanup)

    def profile(self, name: str) -> Path:
        """Create a discoverable profile directory and return its path."""
        path = self.ws.profiles.path_for(name)
        path.mkdir(parents=True, exist_ok=True)
        (path / "settings.json").write_text("{}")
        return path

    def sessions_dir(self, name: str) -> Path:
        return self.profile(name) / "sessions"

    def started(
        self, session: str, *, at_ms: int = NOW_MS, **overrides: Any
    ) -> StartedEvent:
        fields: dict[str, Any] = dict(
            at=now_timestamp(at_ms),
            session=session,
            source="hook",
            cwd="/home/m/Projects/claudewheel",
            config_dir=str(self.ws.profiles.path_for("work")),
            profile=None,
            claude_version="2.1.226",
            model="claude-opus-5",
            permissions="acceptEdits",
            entry="startup",
            transcript="/home/m/.claude/projects/p/s.jsonl",
            pid=4242,
        )
        fields.update(overrides)
        return lifecycle.append_event(self.lifecycle_dir, StartedEvent(**fields))

    def named(self, session: str, name: str, *, at_ms: int = NOW_MS) -> None:
        lifecycle.append_event(
            self.lifecycle_dir,
            NamedEvent(
                at=now_timestamp(at_ms),
                session=session,
                source="sweep",
                name=name,
                name_source="derived",
            ),
        )

    def mark(self, session: str, state: str | None, *, at_ms: int = NOW_MS) -> None:
        lifecycle.append_event(
            self.lifecycle_dir,
            MarkEvent(
                at=now_timestamp(at_ms),
                session=session,
                source="user",
                state=state,
                note=None,
            ),
        )

    def gather(self, *, now_ms: int = NOW_MS, identity: Any = None) -> Any:
        with mock.patch(
            "claudewheel.sessions_overview.processes.resident_memory",
            autospec=True,
            return_value={},
        ):
            return so.gather_rows(self.ws, now_ms=now_ms, identity=identity)

    def lifecycle_text(self, session: str) -> str:
        return lifecycle.session_file(self.lifecycle_dir, session).read_text()


class GatherTests(WorkspaceCase):
    def test_the_table_spans_every_profile_and_the_store(self) -> None:
        live_record(
            self.sessions_dir("work"),
            extra={"sessionId": LIVE_SESSION},
            status="busy",
            name="projects-9a",
        )
        stale_record(
            self.sessions_dir("other"),
            extra={"sessionId": STALE_SESSION},
            name="veliu-api",
        )
        self.started(STORE_SESSION)
        self.mark(STORE_SESSION, "on-hold")

        rows, swept, _named = self.gather()

        self.assertEqual(swept, 0)
        by_session = {row.session: row for row in rows}
        self.assertEqual(by_session[LIVE_SESSION].state, "working")
        self.assertEqual(by_session[LIVE_SESSION].profile, "work")
        self.assertEqual(by_session[LIVE_SESSION].kind, "interactive")
        self.assertEqual(by_session[STALE_SESSION].state, "crashed")
        self.assertEqual(by_session[STALE_SESSION].profile, "other")
        self.assertEqual(by_session[STORE_SESSION].state, "on-hold")
        self.assertEqual(by_session[STORE_SESSION].kind, "-")
        # Live first, then the loose ends in their own order.
        self.assertEqual([row.state for row in rows], ["working", "on-hold", "crashed"])

    def test_a_registry_kind_reads_as_its_label(self) -> None:
        stale_record(
            self.sessions_dir("work"), extra={"sessionId": STALE_SESSION}, kind="bg"
        )
        rows, _swept, _named = self.gather()
        self.assertEqual(rows[0].kind, "background")

    def test_a_session_past_the_grace_period_is_recorded_crashed(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 2 * SWEEP_GRACE_MS)
        rows, swept, _named = self.gather()
        self.assertEqual(swept, 1)
        self.assertIn('"outcome":"crashed"', self.lifecycle_text(STORE_SESSION))
        self.assertEqual([row.state for row in rows], ["crashed"])

    def test_a_session_inside_the_grace_period_is_left_alone(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        rows, swept, _named = self.gather()
        self.assertEqual(swept, 0)
        self.assertEqual([row.state for row in rows], ["starting"])

    def test_a_live_session_is_never_swept(self) -> None:
        live_record(self.sessions_dir("work"), extra={"sessionId": LIVE_SESSION})
        self.started(LIVE_SESSION, at_ms=NOW_MS - 2 * SWEEP_GRACE_MS)
        _rows, swept, _named = self.gather()
        self.assertEqual(swept, 0)
        self.assertNotIn('"kind":"ended"', self.lifecycle_text(LIVE_SESSION))

    def test_a_live_name_is_copied_into_the_store_once(self) -> None:
        live_record(
            self.sessions_dir("work"),
            extra={"sessionId": LIVE_SESSION},
            name="projects-9a",
        )
        rows, _swept, named = self.gather()
        self.assertEqual(named, 1)
        self.assertIn('"name":"projects-9a"', self.lifecycle_text(LIVE_SESSION))
        self.assertEqual(rows[0].name, "projects-9a")
        # Idempotent: the same name from the same source records nothing more.
        _rows, _swept, again = self.gather()
        self.assertEqual(again, 0)

    def test_the_default_profile_is_gathered_too(self) -> None:
        self.claude_dir.mkdir(parents=True)
        stale_record(self.claude_dir / "sessions", extra={"sessionId": STALE_SESSION})
        rows, _swept, _named = self.gather()
        self.assertEqual([row.profile for row in rows], ["default"])

    def test_a_store_only_session_is_attributed_to_its_config_dir(self) -> None:
        self.profile("work")
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        rows, _swept, _named = self.gather()
        self.assertEqual(rows[0].profile, "work")

    def test_an_unknown_config_dir_falls_back_to_its_basename(self) -> None:
        self.started(
            STORE_SESSION, at_ms=NOW_MS - 1000, config_dir="/elsewhere/retired"
        )
        rows, _swept, _named = self.gather()
        self.assertEqual(rows[0].profile, "retired")

    def test_a_recorded_profile_name_is_used_as_recorded(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000, profile="archived")
        rows, _swept, _named = self.gather()
        self.assertEqual(rows[0].profile, "archived")

    def test_the_registry_and_the_store_fill_each_other_in(self) -> None:
        """The model and the transcript exist only in the store, the pid only in
        the registry."""
        live_record(
            self.sessions_dir("work"),
            extra={"sessionId": LIVE_SESSION},
            name="projects-9a",
        )
        self.started(LIVE_SESSION, at_ms=NOW_MS - 1000)
        rows, _swept, _named = self.gather()
        row = rows[0]
        self.assertEqual(row.model, "claude-opus-5")
        self.assertEqual(row.transcript, "/home/m/.claude/projects/p/s.jsonl")
        self.assertEqual(row.pid, os.getpid())
        self.assertEqual(row.version, "2.1.226")

    def test_a_record_with_no_name_anywhere_reads_as_unnamed(self) -> None:
        stale_record(
            self.sessions_dir("work"), extra={"sessionId": STALE_SESSION}, name=None
        )
        rows, _swept, _named = self.gather()
        self.assertEqual(rows[0].name, so.UNNAMED)

    def test_a_session_id_that_is_not_a_uuid_is_read_but_never_written(self) -> None:
        live_record(
            self.sessions_dir("work"),
            extra={"sessionId": "not-a-uuid"},
            name="odd-one",
        )
        rows, _swept, named = self.gather()
        self.assertEqual(named, 0)
        self.assertEqual(rows[0].name, "odd-one")
        self.assertFalse(self.lifecycle_dir.exists())

    def test_the_current_session_is_marked(self) -> None:
        live_record(self.sessions_dir("work"), extra={"sessionId": LIVE_SESSION})
        identity = SessionIdentity(session_id=LIVE_SESSION, pid=os.getpid())
        rows, _swept, _named = self.gather(identity=identity)
        self.assertTrue(rows[0].current)
        plain, _swept, _named = self.gather()
        self.assertFalse(plain[0].current)

    def test_memory_is_measured_only_for_live_pids(self) -> None:
        live_record(self.sessions_dir("work"), extra={"sessionId": LIVE_SESSION})
        stale_record(self.sessions_dir("other"), extra={"sessionId": STALE_SESSION})
        with mock.patch(
            "claudewheel.sessions_overview.processes.resident_memory",
            autospec=True,
            return_value={os.getpid(): 412_000},
        ) as measured:
            rows, _swept, _named = so.gather_rows(self.ws, now_ms=NOW_MS, identity=None)
        measured.assert_called_once_with([os.getpid()])
        by_session = {row.session: row for row in rows}
        self.assertEqual(by_session[LIVE_SESSION].rss_kib, 412_000)
        self.assertIsNone(by_session[STALE_SESSION].rss_kib)

    def test_an_empty_machine_gathers_nothing(self) -> None:
        rows, swept, named = self.gather()
        self.assertEqual((rows, swept, named), ([], 0, 0))

    def test_a_torn_registry_file_is_skipped_rather_than_raising(self) -> None:
        sessions = self.sessions_dir("work")
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "424242.json").write_text('{"pid": 424242, "kin')
        rows, _swept, _named = self.gather()
        self.assertEqual(rows, [])


class KeyLoopCase(WorkspaceCase):
    """A case that runs the screen over its temporary workspace."""

    def run_screen(
        self,
        keys: list[str],
        *,
        rows: int = 24,
        cols: int = 120,
        identity: Any = None,
        terminal: FakeTerminal | None = None,
    ) -> tuple[so.OverviewOutcome, FakeTerminal]:
        term = terminal or FakeTerminal(keys)
        term.rows = rows
        term.cols = cols
        self.theme = _theme()
        with mock.patch(
            "claudewheel.sessions_overview.processes.resident_memory",
            autospec=True,
            return_value={},
        ):
            outcome = so.run_overview(
                self.ws,
                theme=self.theme,
                terminal=term,
                clock=_Clock(),
                identity=identity,
                home="/home/m",
            )
        return outcome, term

    def focused(self, frame: str) -> str | None:
        """The text of the focused line of *frame*, if one is drawn."""
        for chunk in _raw_lines(frame):
            if self.theme.sessions_focus_bg in chunk:
                return _ANSI.sub("", chunk)
        return None

    def focus_path(self, terminal: FakeTerminal) -> list[str]:
        """The focused row's name in each frame the screen drew."""
        names = []
        for frame in terminal.output:
            line = self.focused(frame)
            if line is not None:
                names.append(line.strip("│ ").split(" ")[0])
        return names


class NavigationTests(KeyLoopCase):
    def _three(self) -> None:
        for index, session in enumerate((STALE_SESSION, STORE_SESSION, OTHER_SESSION)):
            self.started(session, at_ms=NOW_MS - (index + 1) * 1000)
            self.named(session, f"row-{index}")
            self.mark(session, "blocked")

    def test_a_scripted_sequence_walks_the_expected_focus_path(self) -> None:
        self._three()
        _outcome, terminal = self.run_screen(["DOWN", "DOWN", "UP", "ESC"])
        # One frame when the screen opens, one per key that did not close it;
        # the rows are in age order, newest first.
        self.assertEqual(
            self.focus_path(terminal), ["row-0", "row-1", "row-2", "row-1"]
        )

    def test_the_focus_clamps_at_both_ends(self) -> None:
        self._three()
        _outcome, terminal = self.run_screen(["UP", "UP", "ESC"])
        self.assertEqual(len(set(self.focus_path(terminal))), 1)

        _outcome, terminal = self.run_screen(["END", "DOWN", "DOWN", "ESC"])
        path = self.focus_path(terminal)
        self.assertEqual(path[1], path[2])
        self.assertEqual(path[2], path[3])

    def test_home_and_end_jump_to_the_ends(self) -> None:
        self._three()
        _outcome, terminal = self.run_screen(["END", "HOME", "ESC"])
        path = self.focus_path(terminal)
        self.assertEqual(path[0], path[2])
        self.assertNotEqual(path[0], path[1])

    def test_a_page_key_moves_by_a_window(self) -> None:
        for index in range(12):
            session = f"dddddddd-4444-4444-8444-{index:012d}"
            self.started(session, at_ms=NOW_MS - (index + 1) * 1000)
            self.named(session, f"row-{index:02d}")
            self.mark(session, "blocked")
        # A window of four data lines, so a page is three rows.
        _outcome, terminal = self.run_screen(["PGDN", "PGUP", "ESC"], rows=9)
        path = self.focus_path(terminal)
        self.assertEqual(path[0], path[2])
        self.assertNotEqual(path[0], path[1])

    def test_an_unbound_key_does_nothing(self) -> None:
        self._three()
        _outcome, terminal = self.run_screen(["z", "ESC"])
        path = self.focus_path(terminal)
        self.assertEqual(path[0], path[1])

    def test_every_close_key_leaves(self) -> None:
        self._three()
        for key in sorted(so.CLOSE_KEYS):
            with self.subTest(key=key):
                # A second key would only be read if the first did not close.
                _outcome, terminal = self.run_screen([key, "DOWN"])
                self.assertEqual(len(terminal.output), 1)

    def test_ctrl_c_at_the_terminal_leaves_too(self) -> None:
        self._three()
        terminal = FakeTerminal([])
        terminal.read_key = mock.Mock(side_effect=KeyboardInterrupt)  # type: ignore[method-assign]
        outcome, _terminal = self.run_screen([], terminal=terminal)
        self.assertEqual(outcome.pruned, ())

    def test_an_empty_machine_says_so_inside_the_frame(self) -> None:
        _outcome, terminal = self.run_screen(["DOWN", "ESC"])
        drawn = _frame_lines(terminal.output[-1])
        self.assertTrue(any("No sessions." in line for line in drawn))
        self.assertTrue(drawn[0].startswith("╭"))

    def test_the_hint_names_the_keys(self) -> None:
        _outcome, terminal = self.run_screen(["ESC"])
        hint = _frame_lines(terminal.output[-1])[-1]
        for needle in ("enter: details", "m: mark", "p: prune crashed", "q: close"):
            self.assertIn(needle, hint)


class ExpandTests(KeyLoopCase):
    def _three(self) -> list[str]:
        """Three store-only rows, newest first, each named after its index."""
        sessions = [STALE_SESSION, STORE_SESSION, OTHER_SESSION]
        for index, session in enumerate(sessions):
            self.started(session, at_ms=NOW_MS - (index + 1) * 1000)
            self.named(session, f"row-{index}")
            self.mark(session, "blocked")
        return sessions

    def test_a_refresh_keeps_the_details_under_the_row_they_belong_to(self) -> None:
        # Expand the second row, move the focus off it, then refresh: the
        # details belong to the session they were opened on, not to whatever
        # the focus has since moved to.
        sessions = self._three()
        _outcome, terminal = self.run_screen(
            ["DOWN", "ENTER", "DOWN", "r", "ESC"], cols=200
        )
        final = _frame_lines(terminal.output[-1])
        self.assertTrue(any(f"session {sessions[1]}" in line for line in final))
        self.assertFalse(any(f"session {sessions[2]}" in line for line in final))

    def test_a_refresh_that_loses_the_expanded_row_collapses(self) -> None:
        sessions = self._three()
        terminal = FakeTerminal(["DOWN", "ENTER", "r", "ESC"])
        original = terminal.read_key

        def read_key() -> str:
            key = original()
            if key == "r":
                # Arrives between the two gathers, so the expanded row is gone
                # by the time the refresh looks for it again.
                lifecycle.session_file(self.lifecycle_dir, sessions[1]).unlink()
            return key

        terminal.read_key = read_key  # type: ignore[method-assign]
        _outcome, term = self.run_screen([], cols=200, terminal=terminal)
        final = _frame_lines(term.output[-1])
        for session in sessions:
            self.assertFalse(any(f"session {session}" in line for line in final))

    def test_enter_opens_and_closes_the_detail_lines(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        self.mark(STORE_SESSION, "blocked")
        _outcome, terminal = self.run_screen(["ENTER", "ENTER", "ESC"])
        opened = _frame_lines(terminal.output[1])
        closed = _frame_lines(terminal.output[2])
        self.assertTrue(any(f"session {STORE_SESSION}" in line for line in opened))
        self.assertFalse(any(f"session {STORE_SESSION}" in line for line in closed))

    def test_the_detail_lines_name_the_profile_and_the_transcript(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        self.mark(STORE_SESSION, "blocked")
        _outcome, terminal = self.run_screen(["ENTER", "ESC"], cols=200)
        opened = " ".join(_frame_lines(terminal.output[1]))
        self.assertIn("profile work", opened)
        self.assertIn("transcript ~/.claude/projects/p/s.jsonl", opened)


class ShowAllTests(KeyLoopCase):
    def test_a_finished_session_appears_only_after_the_show_all_key(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        self.mark(STORE_SESSION, "done")
        self.started(OTHER_SESSION, at_ms=NOW_MS - 1000)
        self.mark(OTHER_SESSION, "blocked")

        _outcome, terminal = self.run_screen(["a", "ESC"], cols=200)
        before = " ".join(_frame_lines(terminal.output[0]))
        after = " ".join(_frame_lines(terminal.output[1]))
        self.assertNotIn("done", before)
        self.assertIn("done", after)
        self.assertIn("a: loose ends", after)
        self.assertIn("a: show all", before)


class MarkTests(KeyLoopCase):
    def test_a_mark_is_written_and_changes_the_row(self) -> None:
        stale_record(
            self.sessions_dir("work"),
            extra={"sessionId": STALE_SESSION},
            name="veliu-api",
        )
        outcome, terminal = self.run_screen(["m", "h", "ESC"], cols=200)
        self.assertEqual(outcome.marked, 1)
        written = self.lifecycle_text(STALE_SESSION)
        self.assertIn('"kind":"mark"', written)
        self.assertIn('"state":"on-hold"', written)
        self.assertIn('"source":"user"', written)
        last = " ".join(_frame_lines(terminal.output[-1]))
        self.assertIn("on-hold", last)
        self.assertIn("Marked veliu-api on-hold", last)

    def test_mark_mode_says_what_its_keys_do(self) -> None:
        stale_record(self.sessions_dir("work"), extra={"sessionId": STALE_SESSION})
        _outcome, terminal = self.run_screen(["m", "ESC", "ESC"])
        self.assertIn("mark: h on-hold", _frame_lines(terminal.output[1])[-1])

    def test_escape_in_mark_mode_cancels_without_closing(self) -> None:
        stale_record(self.sessions_dir("work"), extra={"sessionId": STALE_SESSION})
        outcome, terminal = self.run_screen(["m", "ESC", "DOWN", "ESC"])
        self.assertEqual(outcome.marked, 0)
        # Opening frame, mark-mode frame, the cancel, then the DOWN.
        self.assertEqual(len(terminal.output), 4)

    def test_clearing_a_mark_writes_a_null_state(self) -> None:
        stale_record(
            self.sessions_dir("work"),
            extra={"sessionId": STALE_SESSION},
            name="veliu-api",
        )
        self.mark(STALE_SESSION, "on-hold", at_ms=NOW_MS - 5000)
        outcome, terminal = self.run_screen(["m", "c", "ESC"], cols=200)
        self.assertEqual(outcome.marked, 1)
        self.assertIn('"state":null', self.lifecycle_text(STALE_SESSION))
        last = " ".join(_frame_lines(terminal.output[-1]))
        self.assertIn("Cleared mark on veliu-api", last)
        self.assertIn("crashed", last)

    def test_a_row_with_no_session_id_cannot_be_marked(self) -> None:
        sessions = self.sessions_dir("work")
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{dead_pid()}.json").write_text(
            json.dumps({"pid": dead_pid(), "kind": "interactive", "procStart": "4242"})
        )
        outcome, terminal = self.run_screen(["m", "ESC"])
        self.assertEqual(outcome.marked, 0)
        self.assertIn(
            "No session id; cannot mark", _frame_lines(terminal.output[1])[-1]
        )


class PruneTests(KeyLoopCase):
    def test_the_prune_key_deletes_the_crashed_records_and_reports_them(self) -> None:
        sessions = self.sessions_dir("work")
        alive = live_record(sessions, extra={"sessionId": LIVE_SESSION})
        dead = stale_record(sessions, extra={"sessionId": STALE_SESSION})
        torn = sessions / "424242.json"
        torn.write_text('{"pid": 424242, "kin')

        outcome, terminal = self.run_screen(["p", "ESC"], cols=200)

        self.assertEqual([record.path for record in outcome.pruned], [dead])
        self.assertFalse(dead.exists())
        self.assertTrue(alive.exists())
        self.assertTrue(torn.exists())
        self.assertIn(
            "Pruned 1 crashed record(s)", " ".join(_frame_lines(terminal.output[-1]))
        )

    def test_pruning_nothing_says_so(self) -> None:
        live_record(self.sessions_dir("work"), extra={"sessionId": LIVE_SESSION})
        outcome, terminal = self.run_screen(["p", "ESC"], cols=200)
        self.assertEqual(outcome.pruned, ())
        self.assertIn(
            "Pruned 0 crashed record(s)", " ".join(_frame_lines(terminal.output[-1]))
        )

    def test_a_store_only_row_has_no_file_to_prune(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 2 * SWEEP_GRACE_MS)
        outcome, _terminal = self.run_screen(["p", "ESC"])
        self.assertEqual(outcome.pruned, ())


class RefreshTests(KeyLoopCase):
    def test_a_new_session_appears_only_after_the_refresh_key(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        self.mark(STORE_SESSION, "blocked")

        terminal = FakeTerminal(["r", "ESC"])
        original = terminal.read_key

        def read_key() -> str:
            key = original()
            if key == "r":
                # Arrives between the two gathers, so only the second sees it.
                self.started(OTHER_SESSION, at_ms=NOW_MS - 1000)
                self.mark(OTHER_SESSION, "blocked")
            return key

        terminal.read_key = read_key  # type: ignore[method-assign]
        _outcome, term = self.run_screen([], cols=200, terminal=terminal)
        self.assertNotIn(OTHER_SESSION[:8], " ".join(_frame_lines(term.output[0])))

        with mock.patch(
            "claudewheel.sessions_overview.processes.resident_memory",
            autospec=True,
            return_value={},
        ):
            rows, _swept, _named = so.gather_rows(self.ws, now_ms=NOW_MS, identity=None)
        self.assertEqual(len(rows), 2)

    def test_a_sweep_during_the_run_is_counted(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 2 * SWEEP_GRACE_MS)
        outcome, _terminal = self.run_screen(["r", "ESC"])
        # The opening gather records the end; the refresh finds it recorded.
        self.assertEqual(outcome.swept, 1)


class GeometryTests(KeyLoopCase):
    def test_the_arrow_keys_scroll_the_columns(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        self.mark(STORE_SESSION, "blocked")
        _outcome, terminal = self.run_screen(["RIGHT", "LEFT", "ESC"], cols=60)
        headers = [_frame_lines(frame)[1] for frame in terminal.output]
        self.assertNotEqual(headers[0], headers[1])
        self.assertEqual(headers[0], headers[2])
        self.assertTrue(headers[1].startswith("│"))
        self.assertEqual({len(header) for header in headers}, {60})

    def test_a_resize_reflows_the_frame(self) -> None:
        self.started(STORE_SESSION, at_ms=NOW_MS - 1000)
        self.mark(STORE_SESSION, "blocked")
        terminal = FakeTerminal(["DOWN", "ESC"])
        original = terminal.read_key

        def read_key() -> str:
            key = original()
            if key == "DOWN":
                terminal.cols = 60
            return key

        terminal.read_key = read_key  # type: ignore[method-assign]
        _outcome, term = self.run_screen([], cols=120, terminal=terminal)
        self.assertEqual(
            {len(line) for line in _frame_lines(term.output[0])[:-1]}, {120}
        )
        self.assertEqual(
            {len(line) for line in _frame_lines(term.output[-1])[:-1]}, {60}
        )

    def test_a_short_terminal_never_writes_past_its_last_row(self) -> None:
        for index in range(6):
            session = f"eeeeeeee-5555-4555-8555-{index:012d}"
            self.started(session, at_ms=NOW_MS - (index + 1) * 1000)
            self.mark(session, "blocked")
        for height in (0, 1, 2, 4, 6, 24):
            with self.subTest(height=height):
                _outcome, terminal = self.run_screen(["DOWN", "ESC"], rows=height)
                written = [
                    int(match.group(1))
                    for match in re.finditer(
                        r"\x1b\[(\d+);\d+H", "".join(terminal.output)
                    )
                ]
                self.assertLessEqual(max(written, default=0), height)


if __name__ == "__main__":
    unittest.main()
