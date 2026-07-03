"""Tests for run_under_pty: capture, exit codes, scripted input, tty restore."""

from __future__ import annotations

import os
import signal
import sys
import termios
import unittest
from unittest import mock

from claudewheel.pty_runner import run_under_pty


def _dev_tty_available() -> bool:
    try:
        with open("/dev/tty", "r+b", buffering=0):
            return True
    except OSError:
        return False


class HeadlessCaptureTests(unittest.TestCase):
    """proxy_terminal=False: pty.fork-based capture that runs on any CI."""

    def test_captures_child_stdout(self) -> None:
        code, captured = run_under_pty(
            [sys.executable, "-c",
             "print('hello'); import sys; sys.stdout.flush()"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(b"hello", captured)

    def test_exit_code_propagates(self) -> None:
        code, captured = run_under_pty(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 3)

    def test_env_is_passed_to_child(self) -> None:
        env = dict(os.environ)
        env["PTY_RUNNER_TEST_VAR"] = "marker-value-42"
        code, captured = run_under_pty(
            [sys.executable, "-c",
             "import os; print(os.environ['PTY_RUNNER_TEST_VAR'])"],
            env,
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(b"marker-value-42", captured)

    def test_scripted_input_drives_interactive_child(self) -> None:
        code, captured = run_under_pty(
            [sys.executable, "-c",
             "x = input(); print('GOT:' + x)"],
            dict(os.environ),
            input_bytes=b"ping\n",
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(b"GOT:ping", captured)

    def test_missing_binary_exits_127(self) -> None:
        code, _captured = run_under_pty(
            ["/nonexistent/definitely-not-a-binary"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 127)

    def test_captures_stderr_too(self) -> None:
        # Under a PTY the child's stderr shares the slave with stdout.
        code, captured = run_under_pty(
            [sys.executable, "-c",
             "import sys; print('oops', file=sys.stderr)"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(b"oops", captured)


class DevTtyErrorTests(unittest.TestCase):
    def test_raises_clear_error_when_dev_tty_unavailable(self) -> None:
        with mock.patch("builtins.open", side_effect=OSError("no tty")):
            with self.assertRaises(RuntimeError) as ctx:
                run_under_pty(
                    [sys.executable, "-c", "print('x')"],
                    dict(os.environ),
                )
        self.assertIn("/dev/tty", str(ctx.exception))


@unittest.skipUnless(_dev_tty_available(), "requires an openable /dev/tty")
class RealTtyProxyTests(unittest.TestCase):
    """Full-proxy mode: only runs where /dev/tty can actually be opened."""

    def test_capture_and_termios_restored(self) -> None:
        with open("/dev/tty", "r+b", buffering=0) as f:
            before = termios.tcgetattr(f.fileno())
        prev_winch = signal.getsignal(signal.SIGWINCH)

        code, captured = run_under_pty(
            [sys.executable, "-c",
             "print('proxied-marker'); import sys; sys.stdout.flush()"],
            dict(os.environ),
        )

        self.assertEqual(code, 0)
        self.assertIn(b"proxied-marker", captured)
        with open("/dev/tty", "r+b", buffering=0) as f:
            after = termios.tcgetattr(f.fileno())
        self.assertEqual(before, after)
        self.assertEqual(signal.getsignal(signal.SIGWINCH), prev_winch)

    def test_termios_restored_even_when_child_fails(self) -> None:
        with open("/dev/tty", "r+b", buffering=0) as f:
            before = termios.tcgetattr(f.fileno())

        code, _captured = run_under_pty(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            dict(os.environ),
        )

        self.assertEqual(code, 7)
        with open("/dev/tty", "r+b", buffering=0) as f:
            after = termios.tcgetattr(f.fileno())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
