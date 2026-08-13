"""Tests for the checklist deletion presents of everything holding a profile.

No real process is ever signalled: the stop mechanisms are patched at the
:mod:`claudewheel.processes` seam, and the screen is driven through the shared
FakeTerminal's recorded keystrokes.
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import deletion_checklist as dc
from claudewheel.session_list import SELECTOR_OFF, SELECTOR_ON
from claudewheel.session_registry import SessionRecord
from claudewheel.defaults import DEFAULT_THEME_DARK
from claudewheel.theme import ThemeColors, parse_theme

from .wheelhelpers import FakeTerminal

STARTED_AT = 1_786_536_700_326
NOW_MS = STARTED_AT + 60_000


def _theme() -> ThemeColors:
    return parse_theme(DEFAULT_THEME_DARK)


def _record(pid: int, kind: str = "interactive", live: bool = True) -> SessionRecord:
    return SessionRecord(
        path=Path(f"/tmp/sessions/{pid}.json"),
        pid=pid,
        kind=kind,
        live=live,
        session_id=f"s-{pid}",
        cwd="/home/m/Projects",
        status="idle",
        name=f"row-{pid}",
        version="2.1.226",
        started_at=STARTED_AT,
        proc_start="1",
    )


#: The kernel start token every fixture record carries.
TOKEN = "1"


@contextmanager
def _identity_holds(tokens: dict[int, str] | None = None) -> Iterator[None]:
    """Every fixture pid exists and still carries the token its record recorded.

    *tokens* overrides the answer for specific pids, which is how a test says
    "this pid was recycled": a different token means the process the record
    describes is gone, whatever the pid number now names.
    """
    answers = tokens or {}
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch(
                "claudewheel.deletion_checklist.session_registry.pid_exists",
                autospec=True,
                return_value=True,
            )
        )
        stack.enter_context(
            mock.patch(
                "claudewheel.deletion_checklist.session_registry.process_start_token",
                autospec=True,
                side_effect=lambda pid: answers.get(pid, TOKEN),
            )
        )
        yield


def _holders(*specs: tuple[int, str]) -> list[dc.Holder]:
    return [
        dc.Holder(record=_record(pid, kind), ticked=kind in dc.DAEMON_KINDS)
        for pid, kind in specs
    ]


class GatherTests(unittest.TestCase):
    """What holds the profile, and what is ticked before the user touches it."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = Path(self.tmp.name)
        self.sessions = self.config_dir / "sessions"
        self.sessions.mkdir()

    def _write(self, pid: int, kind: str) -> None:
        (self.sessions / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "kind": kind,
                    "sessionId": f"s-{pid}",
                    "startedAt": STARTED_AT,
                    "status": "idle",
                }
            )
        )

    def test_the_daemon_and_its_workers_are_pre_ticked(self) -> None:
        for pid, kind in ((1, "daemon"), (2, "daemon-worker"), (3, "interactive")):
            self._write(pid, kind)
        with (
            mock.patch(
                "claudewheel.deletion_checklist.session_registry.pid_exists",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.resident_memory",
                autospec=True,
                return_value={},
            ),
        ):
            holders = dc.gather_holders(self.config_dir)

        ticks = {h.record.pid: h.ticked for h in holders}
        self.assertEqual(ticks, {1: True, 2: True, 3: False})

    def test_background_jobs_are_not_pre_ticked(self) -> None:
        self._write(9, "bg")
        with (
            mock.patch(
                "claudewheel.deletion_checklist.session_registry.pid_exists",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.resident_memory",
                autospec=True,
                return_value={},
            ),
        ):
            holders = dc.gather_holders(self.config_dir)
        self.assertEqual([h.ticked for h in holders], [False])

    def test_only_live_records_hold_the_profile(self) -> None:
        self._write(1, "interactive")
        self._write(2, "interactive")
        with (
            mock.patch(
                "claudewheel.deletion_checklist.session_registry.pid_exists",
                autospec=True,
                side_effect=lambda pid: pid == 1,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.resident_memory",
                autospec=True,
                return_value={},
            ),
        ):
            holders = dc.gather_holders(self.config_dir)
        self.assertEqual([h.record.pid for h in holders], [1])

    def test_memory_is_measured_once_for_every_holder(self) -> None:
        self._write(1, "interactive")
        self._write(2, "bg")
        with (
            mock.patch(
                "claudewheel.deletion_checklist.session_registry.pid_exists",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.resident_memory",
                autospec=True,
                return_value={1: 2048},
            ) as memory,
        ):
            holders = dc.gather_holders(self.config_dir)

        memory.assert_called_once_with([1, 2])
        self.assertEqual(holders[0].rss_kib, 2048)
        self.assertIsNone(holders[1].rss_kib)


class StopOrderTests(unittest.TestCase):
    def test_only_ticked_holders_are_stopped(self) -> None:
        holders = _holders((1, "daemon"), (2, "interactive"))
        self.assertEqual([h.record.pid for h in dc.stop_order(holders)], [1])

    def test_the_supervisor_goes_before_its_workers(self) -> None:
        """Stopping a worker first would let the supervisor put it back."""
        holders = _holders((5, "daemon-worker"), (6, "daemon"), (7, "bg"))
        for holder in holders:
            holder.ticked = True
        self.assertEqual([h.record.pid for h in dc.stop_order(holders)], [6, 5, 7])


class StopperTests(unittest.TestCase):
    """Which mechanism stops which kind of holder."""

    def setUp(self) -> None:
        self.stopper = dc.Stopper(
            binary=Path("/bin/claude"), config_dir=Path("/p/work"), env={}
        )

    def test_the_daemon_is_stopped_by_its_own_command(self) -> None:
        holder = dc.Holder(record=_record(1, "daemon"), ticked=True)
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.stop_daemon",
                autospec=True,
                return_value=True,
            ) as stop_daemon,
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate", autospec=True
            ) as terminate,
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            self.assertTrue(self.stopper.stop(holder))

        stop_daemon.assert_called_once_with(
            Path("/bin/claude"), Path("/p/work"), env={}
        )
        terminate.assert_not_called()

    def test_the_daemon_command_runs_once_however_many_rows_name_it(self) -> None:
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.stop_daemon",
                autospec=True,
                return_value=True,
            ) as stop_daemon,
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            self.stopper.stop(dc.Holder(record=_record(1, "daemon"), ticked=True))
            self.stopper.stop(dc.Holder(record=_record(2, "daemon"), ticked=True))
        self.assertEqual(stop_daemon.call_count, 1)

    def test_every_other_kind_gets_a_signal(self) -> None:
        for kind in ("daemon-worker", "bg", "interactive"):
            with self.subTest(kind=kind):
                holder = dc.Holder(record=_record(3, kind), ticked=True)
                with (
                    _identity_holds(),
                    mock.patch(
                        "claudewheel.deletion_checklist.processes.stop_daemon",
                        autospec=True,
                    ) as stop_daemon,
                    mock.patch(
                        "claudewheel.deletion_checklist.processes.terminate",
                        autospec=True,
                        return_value=True,
                    ) as terminate,
                    mock.patch(
                        "claudewheel.deletion_checklist.processes.wait_for_exit",
                        autospec=True,
                        return_value=True,
                    ),
                ):
                    self.assertTrue(dc.Stopper(Path("/b"), Path("/p"), {}).stop(holder))
                terminate.assert_called_once_with(3)
                stop_daemon.assert_not_called()

    def test_a_recycled_pid_is_never_signalled(self) -> None:
        """The gather snapshot is arbitrarily old by the time the user confirms;
        a pid the kernel has since handed to someone else must not get SIGTERM."""
        for kind in ("daemon-worker", "bg", "interactive"):
            with self.subTest(kind=kind):
                holder = dc.Holder(record=_record(3, kind), ticked=True)
                with (
                    _identity_holds({3: "999"}),
                    mock.patch(
                        "claudewheel.deletion_checklist.processes.terminate",
                        autospec=True,
                    ) as terminate,
                    mock.patch(
                        "claudewheel.deletion_checklist.processes.wait_for_exit",
                        autospec=True,
                    ) as wait,
                ):
                    # Already gone counts as stopped: the profile is not held.
                    self.assertTrue(self.stopper.stop(holder))
                terminate.assert_not_called()
                wait.assert_not_called()

    def test_a_recycled_daemon_pid_does_not_run_the_stop_command(self) -> None:
        holder = dc.Holder(record=_record(1, "daemon"), ticked=True)
        with (
            _identity_holds({1: "999"}),
            mock.patch(
                "claudewheel.deletion_checklist.processes.stop_daemon", autospec=True
            ) as stop_daemon,
        ):
            self.assertTrue(self.stopper.stop(holder))
        stop_daemon.assert_not_called()

    def test_the_wait_probe_counts_a_recycled_pid_as_exited(self) -> None:
        """A pid that dies and is reused mid-wait is an exit, not a survivor."""
        holder = dc.Holder(record=_record(3, "bg"), ticked=True)
        captured: dict[str, Any] = {}

        def fake_wait(pid: int, **kwargs: Any) -> bool:
            captured.update(kwargs)
            return True

        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                side_effect=fake_wait,
            ),
        ):
            self.stopper.stop(holder)
            probe = captured["alive"]
            self.assertTrue(probe(3))
        with _identity_holds({3: "999"}):
            self.assertFalse(probe(3))

    def test_a_process_that_will_not_die_is_a_failed_stop(self) -> None:
        holder = dc.Holder(record=_record(3, "bg"), ticked=True)
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=False,
            ),
        ):
            self.assertFalse(self.stopper.stop(holder))


class ScreenTests(unittest.TestCase):
    """The key loop: toggling, confirming, cancelling, and the stop replay."""

    def _run(
        self, keys: list[str], holders: list[dc.Holder], **kwargs: object
    ) -> dc.ChecklistOutcome:
        terminal = FakeTerminal(keys, in_raw=True)
        self.terminal = terminal
        return dc.run_checklist(
            holders,
            profile_name="work",
            config_dir=Path("/p/work"),
            binary=Path("/bin/claude"),
            env={},
            theme=_theme(),
            terminal=terminal,
            now_ms=NOW_MS,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_escape_cancels_and_stops_nothing(self) -> None:
        holders = _holders((1, "daemon"), (2, "interactive"))
        with (
            mock.patch(
                "claudewheel.deletion_checklist.processes.stop_daemon", autospec=True
            ) as stop_daemon,
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate", autospec=True
            ) as terminate,
        ):
            outcome = self._run(["ESC"], holders)

        self.assertFalse(outcome.confirmed)
        stop_daemon.assert_not_called()
        terminate.assert_not_called()

    def test_space_toggles_the_focused_row(self) -> None:
        holders = _holders((1, "interactive"), (2, "interactive"))
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ) as terminate,
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            outcome = self._run([" ", "ENTER", "ESC"], holders)

        self.assertTrue(outcome.confirmed)
        terminate.assert_called_once_with(1)
        self.assertEqual([r.pid for r in outcome.stopped], [1])
        self.assertEqual([r.pid for r in outcome.still_holding], [2])

    def test_moving_the_focus_before_toggling(self) -> None:
        holders = _holders((1, "interactive"), (2, "interactive"))
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ) as terminate,
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            self._run(["DOWN", " ", "ENTER", "ESC"], holders)
        terminate.assert_called_once_with(2)

    def test_with_every_holder_ticked_nothing_is_left_holding(self) -> None:
        holders = _holders((1, "daemon"), (2, "bg"))
        holders[1].ticked = True
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.stop_daemon",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            outcome = self._run(["ENTER", "ESC"], holders)

        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.still_holding, ())
        self.assertEqual(len(outcome.stopped), 2)

    def test_an_untouched_holder_is_reported_as_still_holding(self) -> None:
        holders = _holders((1, "daemon"), (2, "bg"))
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.stop_daemon",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            outcome = self._run(["ENTER", "ESC"], holders)
        self.assertEqual([r.pid for r in outcome.still_holding], [2])

    def test_a_failed_stop_leaves_the_holder_holding(self) -> None:
        holders = _holders((2, "bg"))
        holders[0].ticked = True
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=False,
            ),
        ):
            outcome = self._run(["ENTER", "ESC"], holders)
        self.assertEqual([r.pid for r in outcome.still_holding], [2])
        self.assertEqual(outcome.stopped, ())

    def test_a_holder_that_died_before_confirmation_is_reported_stopped(self) -> None:
        """No signal is sent for it, and the row still goes red: the process the
        screen listed is gone, which is exactly what the tick asked for."""
        holders = _holders((2, "bg"))
        holders[0].ticked = True
        with (
            _identity_holds({2: "999"}),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate", autospec=True
            ) as terminate,
        ):
            outcome = self._run(["ENTER", "ESC"], holders)

        terminate.assert_not_called()
        self.assertEqual([r.pid for r in outcome.stopped], [2])
        self.assertEqual(outcome.still_holding, ())
        self.assertIn(dc.STATE_STOPPED, "".join(self.terminal.output))

    def test_the_screen_stays_open_and_replays_the_stops(self) -> None:
        """Confirmation does not close the screen: the rows go red in place."""
        holders = _holders((1, "interactive"))
        holders[0].ticked = True
        with (
            _identity_holds(),
            mock.patch(
                "claudewheel.deletion_checklist.processes.terminate",
                autospec=True,
                return_value=True,
            ),
            mock.patch(
                "claudewheel.deletion_checklist.processes.wait_for_exit",
                autospec=True,
                return_value=True,
            ),
        ):
            self._run(["ENTER", "ESC"], holders)

        drawn = "".join(self.terminal.output)
        self.assertIn(dc.STATE_RUNNING, drawn)
        self.assertIn(dc.STATE_STOPPED, drawn)

    def test_the_selectors_are_drawn(self) -> None:
        holders = _holders((1, "daemon"), (2, "interactive"))
        self._run(["ESC"], holders)
        drawn = "".join(self.terminal.output)
        self.assertIn(SELECTOR_ON, drawn)
        self.assertIn(SELECTOR_OFF, drawn)


if __name__ == "__main__":
    unittest.main()
