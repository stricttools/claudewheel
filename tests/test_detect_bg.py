"""Tests for terminal background color auto-detection (OSC 11 + DA1)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from claudewheel.terminal import (
    _classify_rgb,
    _parse_osc11_response,
    detect_terminal_background,
)


class ClassifyRgbTests(unittest.TestCase):
    """_classify_rgb parses various RGB formats and classifies luminance."""

    def test_black_is_dark(self) -> None:
        self.assertEqual(_classify_rgb("0000/0000/0000"), "dark")

    def test_white_is_light(self) -> None:
        self.assertEqual(_classify_rgb("ffff/ffff/ffff"), "light")

    def test_short_hex_black(self) -> None:
        """Single-digit hex (some 8-bit terminals): rgb:0/0/0."""
        self.assertEqual(_classify_rgb("0/0/0"), "dark")

    def test_short_hex_white(self) -> None:
        self.assertEqual(_classify_rgb("f/f/f"), "light")

    def test_two_digit_hex(self) -> None:
        """Two-digit hex: rgb:00/00/00."""
        self.assertEqual(_classify_rgb("00/00/00"), "dark")
        self.assertEqual(_classify_rgb("ff/ff/ff"), "light")

    def test_three_digit_hex(self) -> None:
        """Three-digit hex: rgb:000/000/000."""
        self.assertEqual(_classify_rgb("000/000/000"), "dark")
        self.assertEqual(_classify_rgb("fff/fff/fff"), "light")

    def test_mid_range_dark(self) -> None:
        """A typical dark terminal bg (e.g. #1e1e1e) should classify as dark."""
        # #1e1e1e -> each channel = 0x1e1e in 4-digit form
        self.assertEqual(_classify_rgb("1e1e/1e1e/1e1e"), "dark")

    def test_mid_range_light(self) -> None:
        """A typical light terminal bg (e.g. #f0f0f0) should classify as light."""
        self.assertEqual(_classify_rgb("f0f0/f0f0/f0f0"), "light")

    def test_solarized_dark(self) -> None:
        """Solarized dark bg (#002b36) should classify as dark."""
        self.assertEqual(_classify_rgb("0000/2b2b/3636"), "dark")

    def test_solarized_light(self) -> None:
        """Solarized light bg (#fdf6e3) should classify as light."""
        self.assertEqual(_classify_rgb("fdfd/f6f6/e3e3"), "light")

    def test_luminance_boundary(self) -> None:
        """Gray at exactly sRGB ~73% (luminance ~0.5) is a boundary."""
        # sRGB 0.735 linearizes to ~0.5; 0xbc/0xff ~ 0.737
        self.assertEqual(_classify_rgb("bc/bc/bc"), "light")
        # 0xba/0xff ~ 0.729 -> luminance just under 0.5
        self.assertEqual(_classify_rgb("b9/b9/b9"), "dark")

    def test_invalid_format_returns_none(self) -> None:
        self.assertIsNone(_classify_rgb("not/valid/hex"))
        self.assertIsNone(_classify_rgb("ff/ff"))
        self.assertIsNone(_classify_rgb(""))
        self.assertIsNone(_classify_rgb("ff/ff/ff/ff"))

    def test_empty_channel_returns_none(self) -> None:
        self.assertIsNone(_classify_rgb("/ff/ff"))
        self.assertIsNone(_classify_rgb("ff//ff"))

    def test_too_long_channel_returns_none(self) -> None:
        self.assertIsNone(_classify_rgb("fffff/ffff/ffff"))


class ParseOsc11ResponseTests(unittest.TestCase):
    """_parse_osc11_response reads from fd and parses OSC 11 replies."""

    def _mock_fd(self, data: bytes) -> int:
        """Return a fake fd that yields *data* then times out."""
        fd = 42
        buf = bytearray(data)
        call_count = [0]

        def fake_select(rlist, wlist, xlist, timeout=None):
            if buf:
                return (rlist, [], [])
            return ([], [], [])

        def fake_read(fd_arg, n):
            chunk = bytes(buf[:n])
            del buf[:n]
            return chunk

        self._select_patch = mock.patch(
            "claudewheel.terminal.select.select", side_effect=fake_select)
        self._read_patch = mock.patch(
            "claudewheel.terminal.os.read", side_effect=fake_read)
        self._select_patch.start()
        self._read_patch.start()
        return fd

    def tearDown(self) -> None:
        for attr in ("_select_patch", "_read_patch"):
            p = getattr(self, attr, None)
            if p:
                p.stop()

    def test_dark_bg_response(self) -> None:
        """Standard dark bg response: ESC]11;rgb:0000/0000/0000 BEL, then DA1."""
        fd = self._mock_fd(b"\x1b]11;rgb:0000/0000/0000\x07\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "dark")

    def test_light_bg_response(self) -> None:
        fd = self._mock_fd(b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "light")

    def test_st_terminator(self) -> None:
        """OSC response terminated by ST (ESC \\) instead of BEL."""
        fd = self._mock_fd(b"\x1b]11;rgb:0000/0000/0000\x1b\\\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "dark")

    def test_8bit_st_terminator(self) -> None:
        """OSC response terminated by 8-bit ST (0x9c)."""
        fd = self._mock_fd(b"\x1b]11;rgb:ffff/ffff/ffff\x9c\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "light")

    def test_8bit_osc_introducer(self) -> None:
        """8-bit OSC introducer (0x9d) instead of ESC]."""
        fd = self._mock_fd(b"\x9d11;rgb:0000/0000/0000\x07\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "dark")

    def test_da1_only_response(self) -> None:
        """Terminal doesn't support OSC 11, sends DA1 only."""
        fd = self._mock_fd(b"\x1b[?62;c")
        self.assertIsNone(_parse_osc11_response(fd))

    def test_timeout_returns_none(self) -> None:
        """No response at all -> timeout -> None."""
        fd = 42
        with mock.patch("claudewheel.terminal.select.select", return_value=([], [], [])):
            self.assertIsNone(_parse_osc11_response(fd))

    def test_empty_response_returns_none(self) -> None:
        fd = self._mock_fd(b"")
        self.assertIsNone(_parse_osc11_response(fd))

    def test_short_hex_channels(self) -> None:
        """Some terminals return short hex (rgb:0/0/0)."""
        fd = self._mock_fd(b"\x1b]11;rgb:0/0/0\x07\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "dark")

    def test_two_digit_hex(self) -> None:
        fd = self._mock_fd(b"\x1b]11;rgb:ff/ff/ff\x07\x1b[?62;c")
        self.assertEqual(_parse_osc11_response(fd), "light")


class DetectTerminalBackgroundTests(unittest.TestCase):
    """detect_terminal_background orchestrates the full detection flow."""

    def test_dumb_term_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertIsNone(detect_terminal_background())

    def test_screen_term_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "screen"}):
            self.assertIsNone(detect_terminal_background())

    def test_screen_256color_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "screen.xterm-256color"}):
            self.assertIsNone(detect_terminal_background())

    def test_eterm_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "Eterm"}):
            self.assertIsNone(detect_terminal_background())

    def test_tty_open_failure_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", side_effect=OSError("no tty")):
                self.assertIsNone(detect_terminal_background())

    def test_tcgetattr_failure_returns_none(self) -> None:
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                side_effect=__import__("termios").error("bad")):
                    self.assertIsNone(detect_terminal_background())
        fake_tty.close.assert_called()

    def test_dark_detection_end_to_end(self) -> None:
        """Full flow: open tty, send query, parse dark response."""
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch(
                                "claudewheel.terminal._parse_osc11_response",
                                return_value="dark",
                            ):
                                result = detect_terminal_background()
        self.assertEqual(result, "dark")
        fake_tty.close.assert_called()

    def test_light_detection_end_to_end(self) -> None:
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch(
                                "claudewheel.terminal._parse_osc11_response",
                                return_value="light",
                            ):
                                result = detect_terminal_background()
        self.assertEqual(result, "light")

    def test_unsupported_terminal_returns_none(self) -> None:
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch(
                                "claudewheel.terminal._parse_osc11_response",
                                return_value=None,
                            ):
                                result = detect_terminal_background()
        self.assertIsNone(result)

    def test_oserror_during_query_returns_none(self) -> None:
        """OSError during the write/read phase is caught gracefully."""
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99
        fake_tty.write.side_effect = OSError("broken pipe")

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            result = detect_terminal_background()
        self.assertIsNone(result)

    def test_terminal_state_restored_on_success(self) -> None:
        """termios.tcsetattr is called to restore the original attrs."""
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99
        old_attrs = ["original"]

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=old_attrs):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        tcsetattr_mock = mock.MagicMock()
                        with mock.patch("claudewheel.terminal.termios.tcsetattr",
                                        tcsetattr_mock):
                            with mock.patch(
                                "claudewheel.terminal._parse_osc11_response",
                                return_value="dark",
                            ):
                                detect_terminal_background()

        tcsetattr_mock.assert_called_once_with(
            99, __import__("termios").TCSAFLUSH, old_attrs)

    def test_no_term_env_does_not_skip(self) -> None:
        """When $TERM is unset (empty), detection proceeds normally."""
        fake_tty = mock.MagicMock()
        fake_tty.fileno.return_value = 99

        with mock.patch.dict(os.environ, {"TERM": ""}, clear=False):
            with mock.patch("builtins.open", return_value=fake_tty):
                with mock.patch("claudewheel.terminal.termios.tcgetattr",
                                return_value=["old"]):
                    with mock.patch("claudewheel.terminal.tty.setcbreak"):
                        with mock.patch("claudewheel.terminal.termios.tcsetattr"):
                            with mock.patch(
                                "claudewheel.terminal._parse_osc11_response",
                                return_value="dark",
                            ):
                                result = detect_terminal_background()
        self.assertEqual(result, "dark")


if __name__ == "__main__":
    unittest.main()
