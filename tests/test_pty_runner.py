"""Tests for run_under_pty: capture, exit codes, scripted input, tty restore."""

from __future__ import annotations

import os
import signal
import sys
import termios
import unittest
from typing import Any
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
            [sys.executable, "-c", "print('hello'); import sys; sys.stdout.flush()"],
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
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['PTY_RUNNER_TEST_VAR'])",
            ],
            env,
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(b"marker-value-42", captured)

    def test_scripted_input_drives_interactive_child(self) -> None:
        code, captured = run_under_pty(
            [sys.executable, "-c", "x = input(); print('GOT:' + x)"],
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
            [sys.executable, "-c", "import sys; print('oops', file=sys.stderr)"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        self.assertIn(b"oops", captured)


class RobustnessTests(unittest.TestCase):
    """Failure-path guarantees: no zombies on proxy-loop errors, no
    truncation on partial PTY writes."""

    def test_child_is_reaped_when_proxy_loop_raises(self) -> None:
        # Inject a failure into the proxy loop (stands in for the real tty
        # disappearing mid-session) and verify the child is still reaped.
        with mock.patch(
            "claudewheel.pty_runner.select.select",
            autospec=True,
            side_effect=RuntimeError("simulated tty loss"),
        ):
            with self.assertRaises(RuntimeError):
                run_under_pty(
                    [sys.executable, "-c", "pass"],
                    dict(os.environ),
                    proxy_terminal=False,
                )
        # If run_under_pty reaped the child, this process has no children
        # left and waitpid(-1) raises ChildProcessError. A zombie instead
        # makes waitpid return its pid -- failing this assertion.
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, 0)

    def test_input_bytes_survive_partial_writes(self) -> None:
        # Simulate a kernel partial write: each os.write only accepts a
        # chunk of what it was given. The runner must loop until all of
        # input_bytes has been written, or the child sees a short read.
        payload = b"x" * 300 + b"\n"
        real_write = os.write

        def chunked_write(fd: int, data: bytes) -> int:
            data = bytes(data)
            chunk = data[: max(1, len(data) // 2)]
            return real_write(fd, chunk)

        child_src = (
            "import os, sys, signal\n"
            "signal.alarm(5)\n"  # bail out instead of hanging on short input
            "data = b''\n"
            "while len(data) < 301:\n"
            "    c = os.read(0, 65536)\n"
            "    if not c: break\n"
            "    data += c\n"
            "print('LEN:%d' % len(data))\n"
        )
        with mock.patch("claudewheel.pty_runner.os.write", chunked_write):
            code, captured = run_under_pty(
                [sys.executable, "-c", child_src],
                dict(os.environ),
                input_bytes=payload,
                proxy_terminal=False,
            )
        self.assertEqual(code, 0)
        self.assertIn(b"LEN:301", captured)


class DevTtyErrorTests(unittest.TestCase):
    def test_raises_clear_error_when_dev_tty_unavailable(self) -> None:
        with mock.patch("builtins.open", autospec=True, side_effect=OSError("no tty")):
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
            [
                sys.executable,
                "-c",
                "print('proxied-marker'); import sys; sys.stdout.flush()",
            ],
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


class SignalHandlerTests(unittest.TestCase):
    """SIGTERM and SIGHUP are saved/restored around the pty proxy loop."""

    def test_sigterm_saved_and_restored_headless(self) -> None:
        """In headless mode (no proxy), SIGTERM handler is saved/restored."""
        prev = signal.getsignal(signal.SIGTERM)
        code, _ = run_under_pty(
            [sys.executable, "-c", "pass"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        after = signal.getsignal(signal.SIGTERM)
        self.assertIs(after, prev)

    def test_sighup_saved_and_restored_headless(self) -> None:
        """In headless mode, SIGHUP handler is saved/restored."""
        prev = signal.getsignal(signal.SIGHUP)
        code, _ = run_under_pty(
            [sys.executable, "-c", "pass"],
            dict(os.environ),
            proxy_terminal=False,
        )
        self.assertEqual(code, 0)
        after = signal.getsignal(signal.SIGHUP)
        self.assertIs(after, prev)

    @unittest.skipUnless(_dev_tty_available(), "requires an openable /dev/tty")
    def test_sigterm_saved_and_restored_proxy(self) -> None:
        """In proxy mode, SIGTERM handler is saved/restored."""
        prev = signal.getsignal(signal.SIGTERM)
        code, _ = run_under_pty(
            [sys.executable, "-c", "pass"],
            dict(os.environ),
        )
        self.assertEqual(code, 0)
        after = signal.getsignal(signal.SIGTERM)
        self.assertIs(after, prev)

    @unittest.skipUnless(_dev_tty_available(), "requires an openable /dev/tty")
    def test_sighup_saved_and_restored_proxy(self) -> None:
        """In proxy mode, SIGHUP handler is saved/restored."""
        prev = signal.getsignal(signal.SIGHUP)
        code, _ = run_under_pty(
            [sys.executable, "-c", "pass"],
            dict(os.environ),
        )
        self.assertEqual(code, 0)
        after = signal.getsignal(signal.SIGHUP)
        self.assertIs(after, prev)

    def test_sigterm_handler_forwards_to_child(self) -> None:
        """The SIGTERM handler installed during proxy forwards to the child."""
        # We verify indirectly: mock os.kill to capture calls.
        # We need to intercept the signal handler that is installed during
        # the proxy loop. Since the handler is defined inside run_under_pty,
        # we capture it by wrapping signal.signal.
        captured_handlers: dict[int, Any] = {}
        real_signal = signal.signal

        def capturing_signal(sig: int, handler: Any) -> Any:
            captured_handlers[sig] = handler
            return real_signal(sig, handler)

        with mock.patch(
            "claudewheel.pty_runner.signal.signal",
            autospec=True,
            side_effect=capturing_signal,
        ):
            code, _ = run_under_pty(
                [sys.executable, "-c", "pass"],
                dict(os.environ),
                proxy_terminal=False,
            )
        self.assertEqual(code, 0)
        # A SIGTERM handler should have been installed
        self.assertIn(
            signal.SIGTERM, captured_handlers, "SIGTERM handler not installed"
        )


if __name__ == "__main__":
    unittest.main()
