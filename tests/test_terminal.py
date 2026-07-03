"""Tests for Terminal raw-mode enter/exit and the alt_screen flag."""

from __future__ import annotations

import unittest
from unittest import mock

from claudewheel.constants import (
    ALT_SCREEN_ON,
    ALT_SCREEN_OFF,
    CLEAR_SCREEN,
    HIDE_CURSOR,
    SHOW_CURSOR,
)
from claudewheel.terminal import Terminal


class TerminalRawModeTestBase(unittest.TestCase):
    """Base class that constructs a Terminal against a mocked /dev/tty."""

    def setUp(self) -> None:
        self.fake_tty = mock.MagicMock()
        self.fake_tty.fileno.return_value = 99
        self.fake_tty.closed = False

        patches = [
            mock.patch("builtins.open", return_value=self.fake_tty),
            mock.patch("claudewheel.terminal.termios.tcgetattr",
                       return_value=["fake-attrs"]),
            mock.patch("claudewheel.terminal.termios.tcsetattr"),
            mock.patch("claudewheel.terminal.tty.setcbreak"),
            mock.patch("claudewheel.terminal.atexit.register"),
            mock.patch.object(Terminal, "get_size", return_value=(24, 80)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.term = Terminal()

    def _output(self) -> str:
        """Return everything written to the fake tty, decoded."""
        return b"".join(
            call.args[0] for call in self.fake_tty.write.call_args_list
        ).decode()

    def _reset_output(self) -> None:
        self.fake_tty.write.reset_mock()


class DefaultAltScreenTests(TerminalRawModeTestBase):
    """enter_raw() with no arguments keeps the original alt-screen behavior."""

    def test_enter_emits_alt_screen_on_and_clear(self) -> None:
        self.term.enter_raw()
        out = self._output()
        self.assertIn(ALT_SCREEN_ON, out)
        self.assertIn(HIDE_CURSOR, out)
        self.assertIn(CLEAR_SCREEN, out)

    def test_exit_emits_alt_screen_off(self) -> None:
        self.term.enter_raw()
        self._reset_output()
        self.term.exit_raw()
        out = self._output()
        self.assertIn(SHOW_CURSOR, out)
        self.assertIn(ALT_SCREEN_OFF, out)

    def test_explicit_true_matches_default(self) -> None:
        self.term.enter_raw(alt_screen=True)
        self.term.exit_raw()
        out = self._output()
        self.assertIn(ALT_SCREEN_ON, out)
        self.assertIn(ALT_SCREEN_OFF, out)


class NoAltScreenTests(TerminalRawModeTestBase):
    """enter_raw(alt_screen=False) must not touch the alternate screen."""

    def test_enter_emits_no_alt_screen_codes(self) -> None:
        self.term.enter_raw(alt_screen=False)
        out = self._output()
        self.assertNotIn(ALT_SCREEN_ON, out)
        self.assertNotIn(CLEAR_SCREEN, out)
        self.assertIn(HIDE_CURSOR, out)

    def test_exit_emits_no_alt_screen_off(self) -> None:
        self.term.enter_raw(alt_screen=False)
        self._reset_output()
        self.term.exit_raw()
        out = self._output()
        self.assertIn(SHOW_CURSOR, out)
        self.assertNotIn(ALT_SCREEN_OFF, out)

    def test_full_cycle_emits_no_alt_screen_codes(self) -> None:
        self.term.enter_raw(alt_screen=False)
        self.term.exit_raw()
        out = self._output()
        self.assertNotIn(ALT_SCREEN_ON, out)
        self.assertNotIn(ALT_SCREEN_OFF, out)
        self.assertNotIn(CLEAR_SCREEN, out)


class FlagResetBetweenCyclesTests(TerminalRawModeTestBase):
    """The alt_screen flag is re-set on each enter_raw, not sticky."""

    def test_false_then_default_restores_alt_screen(self) -> None:
        self.term.enter_raw(alt_screen=False)
        self.term.exit_raw()
        self._reset_output()

        self.term.enter_raw()
        self.term.exit_raw()
        out = self._output()
        self.assertIn(ALT_SCREEN_ON, out)
        self.assertIn(ALT_SCREEN_OFF, out)

    def test_default_then_false_disables_alt_screen(self) -> None:
        self.term.enter_raw()
        self.term.exit_raw()
        self._reset_output()

        self.term.enter_raw(alt_screen=False)
        self.term.exit_raw()
        out = self._output()
        self.assertNotIn(ALT_SCREEN_ON, out)
        self.assertNotIn(ALT_SCREEN_OFF, out)

    def test_exit_raw_is_idempotent(self) -> None:
        """A second exit_raw (e.g. via atexit) writes nothing further."""
        self.term.enter_raw()
        self.term.exit_raw()
        self._reset_output()
        self.term.exit_raw()
        self.assertEqual(self._output(), "")


if __name__ == "__main__":
    unittest.main()
