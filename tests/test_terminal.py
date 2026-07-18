"""Tests for Terminal raw-mode enter/exit and the alt_screen flag."""

from __future__ import annotations

import os
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


class CookedContextManagerTests(TerminalRawModeTestBase):
    """Terminal.cooked() temporarily leaves raw mode and restores it."""

    def test_cooked_from_raw_alt_screen_true(self) -> None:
        self.term.enter_raw(alt_screen=True)
        self._reset_output()

        with self.term.cooked():
            # Body runs in cooked mode: alt screen was turned off.
            self.assertFalse(self.term._in_raw)
            self.assertIn(ALT_SCREEN_OFF, self._output())

        # Re-entered raw with the SAME alt_screen flag (True).
        self.assertTrue(self.term._in_raw)
        self.assertTrue(self.term._alt_screen)
        out = self._output()
        self.assertIn(ALT_SCREEN_ON, out)
        self.assertIn(CLEAR_SCREEN, out)

    def test_cooked_from_raw_alt_screen_false(self) -> None:
        self.term.enter_raw(alt_screen=False)
        self._reset_output()

        with self.term.cooked():
            self.assertFalse(self.term._in_raw)

        # Re-entered raw with the SAME alt_screen flag (False):
        # no alt-screen codes anywhere in the cycle.
        self.assertTrue(self.term._in_raw)
        self.assertFalse(self.term._alt_screen)
        out = self._output()
        self.assertNotIn(ALT_SCREEN_ON, out)
        self.assertNotIn(ALT_SCREEN_OFF, out)
        self.assertIn(SHOW_CURSOR, out)
        self.assertIn(HIDE_CURSOR, out)

    def test_cooked_from_cooked_state_is_noop(self) -> None:
        # Never entered raw: cooked() must emit nothing and change nothing.
        with self.term.cooked():
            self.assertFalse(self.term._in_raw)
        self.assertFalse(self.term._in_raw)
        self.assertEqual(self._output(), "")

    def test_exception_in_body_still_restores_raw(self) -> None:
        self.term.enter_raw(alt_screen=True)
        self._reset_output()

        with self.assertRaises(ValueError):
            with self.term.cooked():
                raise ValueError("boom")

        self.assertTrue(self.term._in_raw)
        self.assertTrue(self.term._alt_screen)
        self.assertIn(ALT_SCREEN_ON, self._output())

    def test_nested_cooked_inner_is_noop(self) -> None:
        self.term.enter_raw(alt_screen=True)
        self._reset_output()

        with self.term.cooked():
            after_outer = self._output()
            self._reset_output()
            with self.term.cooked():
                # Inner is a no-op: nothing emitted, still cooked.
                self.assertFalse(self.term._in_raw)
                self.assertEqual(self._output(), "")
            # Inner exit must NOT re-enter raw -- outer owns the restore.
            self.assertFalse(self.term._in_raw)
            self.assertEqual(self._output(), "")
            self.assertIn(ALT_SCREEN_OFF, after_outer)

        self.assertTrue(self.term._in_raw)
        self.assertIn(ALT_SCREEN_ON, self._output())


class ReadKeyTests(TerminalRawModeTestBase):
    """read_key() decodes escape sequences without leaking residue bytes."""

    def _feed(self, data: bytes):
        """Patch os.read/select.select in terminal module to serve `data`.

        Returns the live buffer so tests can assert full consumption.
        """
        buf = bytearray(data)

        def fake_read(fd, n):
            self.assertEqual(n, 1)
            if not buf:
                self.fail("read_key tried to read past the fed bytes")
            byte = bytes(buf[:1])
            del buf[:1]
            return byte

        def fake_select(rlist, wlist, xlist, timeout=None):
            return (list(rlist) if buf else [], [], [])

        p1 = mock.patch("claudewheel.terminal.os.read", side_effect=fake_read)
        p2 = mock.patch("claudewheel.terminal.select.select",
                        side_effect=fake_select)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return buf

    # --- CSI [digit~ keys (the stray-~ bug) ---

    def test_delete_key_decodes(self) -> None:
        buf = self._feed(b"\x1b[3~")
        self.assertEqual(self.term.read_key(), "DELETE")
        self.assertEqual(len(buf), 0, "trailing ~ leaked into the buffer")

    def test_insert_key_decodes(self) -> None:
        buf = self._feed(b"\x1b[2~")
        self.assertEqual(self.term.read_key(), "INSERT")
        self.assertEqual(len(buf), 0)

    def test_pgup_key_decodes(self) -> None:
        buf = self._feed(b"\x1b[5~")
        self.assertEqual(self.term.read_key(), "PGUP")
        self.assertEqual(len(buf), 0)

    def test_pgdn_key_decodes(self) -> None:
        buf = self._feed(b"\x1b[6~")
        self.assertEqual(self.term.read_key(), "PGDN")
        self.assertEqual(len(buf), 0)

    def test_no_residue_after_delete(self) -> None:
        """The read AFTER a Delete keypress gets the next key, not '~'."""
        self._feed(b"\x1b[3~x")
        self.assertEqual(self.term.read_key(), "DELETE")
        self.assertEqual(self.term.read_key(), "x")

    def test_no_residue_after_pgdn_then_enter(self) -> None:
        self._feed(b"\x1b[6~\r")
        self.assertEqual(self.term.read_key(), "PGDN")
        self.assertEqual(self.term.read_key(), "ENTER")

    def test_unknown_tilde_sequence_consumed_cleanly(self) -> None:
        """F5 (ESC[15~): unmapped, but must not leak bytes."""
        self._feed(b"\x1b[15~a")
        self.assertEqual(self.term.read_key(), "CSI15~")
        self.assertEqual(self.term.read_key(), "a")

    def test_modifier_sequence_consumed_cleanly(self) -> None:
        """Parametric Shift-Tab ESC[1;2Z: decoded, no residue."""
        self._feed(b"\x1b[1;2Zq")
        self.assertEqual(self.term.read_key(), "SHIFT_TAB")
        self.assertEqual(self.term.read_key(), "q")

    def test_unknown_modifier_sequence_consumed_cleanly(self) -> None:
        """Ctrl-Right ESC[1;5C: unmapped params, consumed without leaking."""
        self._feed(b"\x1b[1;5Cz")
        self.assertEqual(self.term.read_key(), "CSI1;5C")
        self.assertEqual(self.term.read_key(), "z")

    # --- CTRL_D ---

    def test_ctrl_d_decodes(self) -> None:
        self._feed(b"\x04")
        self.assertEqual(self.term.read_key(), "CTRL_D")

    # --- regression: existing keys still decode ---

    def test_arrow_keys_decode(self) -> None:
        self._feed(b"\x1b[A\x1b[B\x1b[C\x1b[D")
        self.assertEqual(self.term.read_key(), "UP")
        self.assertEqual(self.term.read_key(), "DOWN")
        self.assertEqual(self.term.read_key(), "RIGHT")
        self.assertEqual(self.term.read_key(), "LEFT")

    def test_home_end_decode(self) -> None:
        self._feed(b"\x1b[H\x1b[F")
        self.assertEqual(self.term.read_key(), "HOME")
        self.assertEqual(self.term.read_key(), "END")

    def test_shift_tab_decodes(self) -> None:
        self._feed(b"\x1b[Z")
        self.assertEqual(self.term.read_key(), "SHIFT_TAB")

    def test_enter_tab_backspace_ctrl_c_decode(self) -> None:
        self._feed(b"\r\n\t\x7f\x08\x03")
        self.assertEqual(self.term.read_key(), "ENTER")
        self.assertEqual(self.term.read_key(), "ENTER")
        self.assertEqual(self.term.read_key(), "TAB")
        self.assertEqual(self.term.read_key(), "BACKSPACE")
        self.assertEqual(self.term.read_key(), "BACKSPACE")
        self.assertEqual(self.term.read_key(), "CTRL_C")

    def test_bare_escape_decodes(self) -> None:
        self._feed(b"\x1b")
        self.assertEqual(self.term.read_key(), "ESC")

    def test_plain_character_passthrough(self) -> None:
        self._feed(b"g")
        self.assertEqual(self.term.read_key(), "g")


class CSIPrivateModeTests(ReadKeyTests):
    """CSI sequences starting with '?' (private modes) must be fully consumed.

    The entry gate (ch3.isdigit()) previously rejected '?' so sequences like
    ESC[?997;1n leaked bytes into subsequent reads.
    """

    def test_mode2031_dark_notification_returns_theme_dark(self) -> None:
        """ESC[?997;1n (Mode 2031 dark) returns THEME_DARK synthetic key."""
        buf = self._feed(b"\x1b[?997;1nx")
        self.assertEqual(self.term.read_key(), "THEME_DARK")
        self.assertEqual(len(buf), 1, "trailing 'x' should remain")
        self.assertEqual(self.term.read_key(), "x")

    def test_mode2031_light_notification_returns_theme_light(self) -> None:
        """ESC[?997;2n (Mode 2031 light) returns THEME_LIGHT synthetic key."""
        buf = self._feed(b"\x1b[?997;2nx")
        self.assertEqual(self.term.read_key(), "THEME_LIGHT")
        self.assertEqual(len(buf), 1, "trailing 'x' should remain")
        self.assertEqual(self.term.read_key(), "x")

    def test_mode2031_no_byte_leak(self) -> None:
        """After consuming ESC[?997;1n, the next read_key sees the byte after."""
        self._feed(b"\x1b[?997;1nA")
        self.term.read_key()  # consume the CSI sequence
        self.assertEqual(self.term.read_key(), "A")

    def test_simple_private_mode_query(self) -> None:
        """ESC[?996n (a private mode query) is consumed cleanly."""
        self._feed(b"\x1b[?996nz")
        self.assertEqual(self.term.read_key(), "CSI?996n")
        self.assertEqual(self.term.read_key(), "z")

    def test_private_set_mode(self) -> None:
        """ESC[?2031h (DECSET-style) is consumed cleanly."""
        self._feed(b"\x1b[?2031hq")
        self.assertEqual(self.term.read_key(), "CSI?2031h")
        self.assertEqual(self.term.read_key(), "q")

    def test_private_reset_mode(self) -> None:
        """ESC[?2031l (DECRST-style) is consumed cleanly."""
        self._feed(b"\x1b[?2031lw")
        self.assertEqual(self.term.read_key(), "CSI?2031l")
        self.assertEqual(self.term.read_key(), "w")

    def test_digit_gate_still_works_for_delete(self) -> None:
        """Regression: DELETE (ESC[3~) still decodes after the gate change."""
        self._feed(b"\x1b[3~m")
        self.assertEqual(self.term.read_key(), "DELETE")
        self.assertEqual(self.term.read_key(), "m")

    def test_digit_gate_still_works_for_pgup(self) -> None:
        """Regression: PGUP (ESC[5~) still decodes after the gate change."""
        self._feed(b"\x1b[5~")
        self.assertEqual(self.term.read_key(), "PGUP")

    def test_digit_gate_still_works_for_pgdn(self) -> None:
        """Regression: PGDN (ESC[6~) still decodes after the gate change."""
        self._feed(b"\x1b[6~")
        self.assertEqual(self.term.read_key(), "PGDN")

    def test_digit_gate_still_works_for_insert(self) -> None:
        """Regression: INSERT (ESC[2~) still decodes after the gate change."""
        self._feed(b"\x1b[2~")
        self.assertEqual(self.term.read_key(), "INSERT")

    def test_semicolon_gate_entry(self) -> None:
        """Semicolon (0x3B) is in the 0x30-0x3F range and should enter the loop."""
        self._feed(b"\x1b[;2Ry")
        self.assertEqual(self.term.read_key(), "CSI;2R")
        self.assertEqual(self.term.read_key(), "y")

    def test_less_than_gate_entry(self) -> None:
        """'<' (0x3C) is in the 0x30-0x3F range and should enter the loop."""
        self._feed(b"\x1b[<35;10;1Mv")
        self.assertEqual(self.term.read_key(), "CSI<35;10;1M")
        self.assertEqual(self.term.read_key(), "v")

    def test_equals_gate_entry(self) -> None:
        """'=' (0x3D) is in the 0x30-0x3F range and should enter the loop."""
        self._feed(b"\x1b[=1ck")
        self.assertEqual(self.term.read_key(), "CSI=1c")
        self.assertEqual(self.term.read_key(), "k")

    def test_greater_than_gate_entry(self) -> None:
        """'>' (0x3E) is in the 0x30-0x3F range and should enter the loop."""
        self._feed(b"\x1b[>1cj")
        self.assertEqual(self.term.read_key(), "CSI>1c")
        self.assertEqual(self.term.read_key(), "j")


class SubscribeMode2031Tests(TerminalRawModeTestBase):
    """subscribe_mode2031() writes the correct escape and sets the flag."""

    def test_subscribe_writes_escape_and_sets_flag(self) -> None:
        self.assertFalse(self.term._mode2031_subscribed)
        self.term.subscribe_mode2031()
        self.assertTrue(self.term._mode2031_subscribed)
        self.assertIn("\x1b[?2031h", self._output())


class ExitRawMode2031Tests(TerminalRawModeTestBase):
    """exit_raw unsubscribes from Mode 2031 when subscribed."""

    def test_exit_raw_with_subscription_writes_unsubscribe(self) -> None:
        self.term.enter_raw()
        self.term.subscribe_mode2031()
        self._reset_output()
        self.term.exit_raw()
        out = self._output()
        self.assertIn("\x1b[?2031l", out)
        self.assertFalse(self.term._mode2031_subscribed)

    def test_exit_raw_unsubscribe_before_alt_screen_off(self) -> None:
        """Unsubscribe must come BEFORE ALT_SCREEN_OFF in the output."""
        self.term.enter_raw()
        self.term.subscribe_mode2031()
        self._reset_output()
        self.term.exit_raw()
        out = self._output()
        unsub_pos = out.index("\x1b[?2031l")
        alt_off_pos = out.index(ALT_SCREEN_OFF)
        self.assertLess(unsub_pos, alt_off_pos)

    def test_exit_raw_without_subscription_no_unsubscribe(self) -> None:
        self.term.enter_raw()
        self._reset_output()
        self.term.exit_raw()
        out = self._output()
        self.assertNotIn("\x1b[?2031l", out)

    def test_exit_raw_clears_subscription_flag(self) -> None:
        self.term.enter_raw()
        self.term.subscribe_mode2031()
        self.assertTrue(self.term._mode2031_subscribed)
        self.term.exit_raw()
        self.assertFalse(self.term._mode2031_subscribed)


class DetectMode2031SupportTests(unittest.TestCase):
    """detect_mode2031_support queries the terminal for Mode 2031."""

    def _mock_fd_response(self, data: bytes):
        """Set up mocks for a detect_mode2031_support call with given fd response."""
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99
        buf = bytearray(data)

        def fake_select(rlist, wlist, xlist, timeout=None):
            return (list(rlist) if buf else [], [], [])

        def fake_read(fd_arg, n):
            chunk = bytes(buf[:n])
            del buf[:n]
            return chunk

        return fake_tty, fake_select, fake_read

    def test_dumb_term_returns_none(self) -> None:
        from claudewheel.terminal import detect_mode2031_support
        with mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertIsNone(detect_mode2031_support())

    def test_screen_term_returns_none(self) -> None:
        from claudewheel.terminal import detect_mode2031_support
        with mock.patch.dict(os.environ, {"TERM": "screen"}):
            self.assertIsNone(detect_mode2031_support())

    def test_eterm_returns_none(self) -> None:
        from claudewheel.terminal import detect_mode2031_support
        with mock.patch.dict(os.environ, {"TERM": "Eterm"}):
            self.assertIsNone(detect_mode2031_support())

    def test_dark_response(self) -> None:
        """CSI ?997;1n response -> 'dark'."""
        from claudewheel.terminal import detect_mode2031_support
        fake_tty, fake_select, fake_read = self._mock_fd_response(
            b"\x1b[?997;1n\x1b[?62;c")
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch("claudewheel.terminal.select.select",
                                            side_effect=fake_select):
                                with mock.patch("claudewheel.terminal.os.read",
                                                side_effect=fake_read):
                                    result = detect_mode2031_support()
        self.assertEqual(result, "dark")

    def test_light_response(self) -> None:
        """CSI ?997;2n response -> 'light'."""
        from claudewheel.terminal import detect_mode2031_support
        fake_tty, fake_select, fake_read = self._mock_fd_response(
            b"\x1b[?997;2n\x1b[?62;c")
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch("claudewheel.terminal.select.select",
                                            side_effect=fake_select):
                                with mock.patch("claudewheel.terminal.os.read",
                                                side_effect=fake_read):
                                    result = detect_mode2031_support()
        self.assertEqual(result, "light")

    def test_unsupported_da1_only(self) -> None:
        """DA1 response only (no ?997) -> None."""
        from claudewheel.terminal import detect_mode2031_support
        fake_tty, fake_select, fake_read = self._mock_fd_response(
            b"\x1b[?62;c")
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch("claudewheel.terminal.select.select",
                                            side_effect=fake_select):
                                with mock.patch("claudewheel.terminal.os.read",
                                                side_effect=fake_read):
                                    result = detect_mode2031_support()
        self.assertIsNone(result)

    def test_timeout_returns_none(self) -> None:
        """No response at all -> timeout -> None."""
        from claudewheel.terminal import detect_mode2031_support
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch("claudewheel.terminal.select.select",
                                            return_value=([], [], [])):
                                result = detect_mode2031_support()
        self.assertIsNone(result)

    def test_tty_open_failure_returns_none(self) -> None:
        from claudewheel.terminal import detect_mode2031_support
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", side_effect=OSError("no tty")):
                self.assertIsNone(detect_mode2031_support())

    def test_tcgetattr_failure_returns_none(self) -> None:
        from claudewheel.terminal import detect_mode2031_support
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                side_effect=__import__("termios").error("bad")):
                    self.assertIsNone(detect_mode2031_support())
        fake_tty.close.assert_called()


class TerminalQueryTimeoutConstantTests(unittest.TestCase):
    """Verify the _TERMINAL_QUERY_TIMEOUT module constant."""

    def test_constant_exists_and_value(self) -> None:
        from claudewheel.terminal import _TERMINAL_QUERY_TIMEOUT
        self.assertEqual(_TERMINAL_QUERY_TIMEOUT, 0.5)


if __name__ == "__main__":
    unittest.main()
