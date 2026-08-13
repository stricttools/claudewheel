"""Tests for the process seam the deletion checklist measures and stops through.

Nothing here starts, signals or waits on a real process: every subprocess call
is a fake standing in for :mod:`claudewheel.effects`, and the exit poll takes
its clock and its sleep as parameters.
"""

from __future__ import annotations

import signal
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from claudewheel import processes


def _completed(stdout: str, returncode: int = 0) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(["ps"], returncode, stdout, "")


class ResidentMemoryTests(unittest.TestCase):
    """One batched ``ps`` call, KiB per pid, missing row means gone."""

    def test_one_call_for_every_pid(self) -> None:
        """All pids are measured by a single ps invocation, not one each."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(" 101 4096\n 202 8192\n"),
        ) as run:
            memory = processes.resident_memory([101, 202])

        self.assertEqual(memory, {101: 4096, 202: 8192})
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["ps", "-o", "pid=,rss="])
        self.assertEqual(argv[3], "-p")
        self.assertEqual(argv[4], "101,202")

    def test_pid_is_always_requested(self) -> None:
        """``pid=`` is in the format because ps row order is not argument order."""
        self.assertIn("pid=,rss=", processes.RSS_FORMAT)

    def test_a_missing_row_means_the_process_is_gone(self) -> None:
        """A pid ps did not report is absent from the mapping, not zero."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(" 101 4096\n"),
        ):
            memory = processes.resident_memory([101, 202])
        self.assertEqual(memory, {101: 4096})

    def test_row_order_is_not_trusted(self) -> None:
        """ps may answer in any order; each row carries its own pid."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(" 202 8192\n 101 4096\n"),
        ):
            memory = processes.resident_memory([101, 202])
        self.assertEqual(memory, {101: 4096, 202: 8192})

    def test_no_pids_makes_no_call(self) -> None:
        """Measuring nothing spawns nothing."""
        with mock.patch("claudewheel.processes.effects.run", autospec=True) as run:
            self.assertEqual(processes.resident_memory([]), {})
        run.assert_not_called()

    def test_a_nonzero_exit_is_not_an_error(self) -> None:
        """ps exits non-zero when no pid matched; that is an empty answer."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed("", 1),
        ):
            self.assertEqual(processes.resident_memory([404]), {})

    def test_unparseable_rows_are_skipped(self) -> None:
        """A malformed line never takes down the measurement."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed("garbage\n 101 4096\n\n 202 x\n"),
        ):
            self.assertEqual(processes.resident_memory([101, 202]), {101: 4096})

    def test_a_missing_ps_is_no_memory_rather_than_a_crash(self) -> None:
        """A platform without ps loses the column; it does not lose the screen."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            side_effect=FileNotFoundError,
        ):
            self.assertEqual(processes.resident_memory([101]), {})

    def test_the_measurement_is_a_declared_read(self) -> None:
        """Measuring changes nothing, so it runs in every mode."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(""),
        ) as run:
            processes.resident_memory([101])
        self.assertTrue(run.call_args.kwargs["read"])

    def test_an_unsettled_carrier_is_never_parsed(self) -> None:
        """A recorded run has no stdout; the answer is 'nothing measured'."""
        carrier = object()
        with (
            mock.patch(
                "claudewheel.processes.effects.run", autospec=True, return_value=carrier
            ),
            mock.patch(
                "claudewheel.processes.effects.unsettled",
                autospec=True,
                return_value=True,
            ),
        ):
            self.assertEqual(processes.resident_memory([101]), {})


class DaemonStopTests(unittest.TestCase):
    """The daemon is stopped by Claude Code's own command, per config dir."""

    def test_the_config_dir_addresses_the_daemon(self) -> None:
        """CLAUDE_CONFIG_DIR selects which profile's daemon is stopped."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(""),
        ) as run:
            processes.stop_daemon(
                Path("/bin/claude"), Path("/profiles/work"), env={"PATH": "/bin"}
            )

        argv = run.call_args.args[0]
        self.assertEqual(
            argv, ["/bin/claude", "daemon", "stop", "--any", "--keep-workers"]
        )
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/profiles/work")
        self.assertEqual(env["PATH"], "/bin")

    def test_workers_are_kept(self) -> None:
        """The supervisor goes; detached sessions are only stopped when ticked."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(""),
        ) as run:
            processes.stop_daemon(Path("/bin/claude"), Path("/p"), env={})
        self.assertIn("--keep-workers", run.call_args.args[0])

    def test_stopping_is_not_a_declared_read(self) -> None:
        """A stop mutates, so a preview records it instead of running it."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed(""),
        ) as run:
            processes.stop_daemon(Path("/bin/claude"), Path("/p"), env={})
        self.assertFalse(run.call_args.kwargs.get("read", False))

    def test_a_missing_binary_is_reported_not_raised(self) -> None:
        """No claude on disk means the stop failed, not that the screen died."""
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            side_effect=FileNotFoundError,
        ):
            self.assertFalse(processes.stop_daemon(Path("/nope"), Path("/p"), env={}))

    def test_a_nonzero_exit_is_a_failed_stop(self) -> None:
        with mock.patch(
            "claudewheel.processes.effects.run",
            autospec=True,
            return_value=_completed("", 3),
        ):
            self.assertFalse(
                processes.stop_daemon(Path("/bin/claude"), Path("/p"), env={})
            )


class SignalTests(unittest.TestCase):
    def test_terminate_goes_through_the_effects_seam(self) -> None:
        with mock.patch("claudewheel.processes.effects.kill", autospec=True) as kill:
            self.assertTrue(processes.terminate(4242))
        kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_a_process_already_gone_is_not_a_failure(self) -> None:
        with mock.patch(
            "claudewheel.processes.effects.kill",
            autospec=True,
            side_effect=ProcessLookupError,
        ):
            self.assertTrue(processes.terminate(4242))

    def test_a_process_we_may_not_signal_is_a_failure(self) -> None:
        with mock.patch(
            "claudewheel.processes.effects.kill",
            autospec=True,
            side_effect=PermissionError,
        ):
            self.assertFalse(processes.terminate(4242))


class WaitForExitTests(unittest.TestCase):
    """The poll takes its clock and its sleep as parameters -- no real waiting."""

    def test_an_already_dead_process_returns_at_once(self) -> None:
        slept: list[float] = []
        gone = processes.wait_for_exit(
            1,
            timeout_s=5.0,
            alive=lambda pid: False,
            sleep=slept.append,
            now=lambda: 0.0,
        )
        self.assertTrue(gone)
        self.assertEqual(slept, [])

    def test_a_process_that_exits_mid_poll_is_seen(self) -> None:
        answers = [True, True, False]
        clock = iter([0.0, 0.1, 0.2, 0.3, 0.4])
        gone = processes.wait_for_exit(
            1,
            timeout_s=5.0,
            alive=lambda pid: answers.pop(0),
            sleep=lambda s: None,
            now=lambda: next(clock),
        )
        self.assertTrue(gone)

    def test_a_process_that_never_exits_times_out(self) -> None:
        ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        gone = processes.wait_for_exit(
            1,
            timeout_s=2.0,
            alive=lambda pid: True,
            sleep=lambda s: None,
            now=lambda: next(ticks),
        )
        self.assertFalse(gone)


class PidLivenessTests(unittest.TestCase):
    def test_the_registry_probe_is_the_one_liveness_answer(self) -> None:
        """processes.alive delegates rather than growing a second probe."""
        from claudewheel import session_registry

        self.assertIs(processes.alive, session_registry.pid_exists)


if __name__ == "__main__":
    unittest.main()
