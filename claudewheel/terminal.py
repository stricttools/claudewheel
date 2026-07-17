"""Raw terminal I/O: cbreak mode, escape sequence decoding, and alt screen."""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import os
import select
import shutil
import struct
import termios
import tty
from collections.abc import Iterator
from typing import Any

from .constants import (ALT_SCREEN_ON, ALT_SCREEN_OFF, HIDE_CURSOR, SHOW_CURSOR, CLEAR_SCREEN)

# Timeout (seconds) for the initial select() in terminal query responses
# (OSC 11 background color, Mode 2031 dark/light). Shorter than the follow-up
# chunk-read timeout (0.1s) because these queries either respond quickly or not
# at all -- waiting a full second delays startup for no benefit.
_TERMINAL_QUERY_TIMEOUT = 0.5


class Terminal:
    """Low-level terminal I/O: raw mode, key reading, alt screen, and size detection."""

    def __init__(self) -> None:
        # Open /dev/tty directly so we work even when stdin is piped
        self._tty_file = open("/dev/tty", "r+b", buffering=0)
        self.fd = self._tty_file.fileno()
        self.old_attrs: list[Any] | None = None
        self.rows = 24
        self.cols = 80
        self._in_raw = False
        self._alt_screen = True
        self._mode2031_subscribed = False

    def get_size(self) -> tuple[int, int]:
        try:
            packed = fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("hhhh", packed)
            return rows, cols
        except OSError:
            size = shutil.get_terminal_size()
            return size.lines, size.columns

    def enter_raw(self, alt_screen: bool = True) -> None:
        self.old_attrs = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)  # cbreak, not raw -- lets Ctrl-C generate SIGINT
        self._in_raw = True
        self._alt_screen = alt_screen  # remembered so exit_raw restores symmetrically
        self.rows, self.cols = self.get_size()
        if alt_screen:
            self._write_tty(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR_SCREEN)
        else:
            self._write_tty(HIDE_CURSOR)
        atexit.register(self.exit_raw)

    def subscribe_mode2031(self) -> None:
        """Subscribe to Mode 2031 theme-change notifications."""
        self._write_tty("\x1b[?2031h")
        self._mode2031_subscribed = True

    def exit_raw(self) -> None:
        if self._in_raw and self.old_attrs is not None:
            # Unsubscribe from Mode 2031 before restoring terminal state
            if self._mode2031_subscribed:
                self._write_tty("\x1b[?2031l")
                self._mode2031_subscribed = False
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old_attrs)
            if self._alt_screen:
                self._write_tty(SHOW_CURSOR + ALT_SCREEN_OFF)
            else:
                self._write_tty(SHOW_CURSOR)
            self._in_raw = False

    @contextlib.contextmanager
    def cooked(self) -> Iterator[Terminal]:
        """Temporarily leave raw mode for the duration of the with-block.

        If the terminal is currently raw, exits raw mode on entry and
        re-enters it on exit with the same alt_screen flag it had before.
        If already cooked, this is a no-op passthrough (so nesting is safe).
        Raw mode is restored even if the body raises.
        """
        was_raw = self._in_raw
        alt_screen = self._alt_screen
        if was_raw:
            self.exit_raw()
        try:
            yield self
        finally:
            if was_raw:
                self.enter_raw(alt_screen=alt_screen)

    def read_key(self) -> str:
        """Read a single keypress, decoding escape sequences for arrow keys etc."""
        ch = os.read(self.fd, 1).decode("utf-8", errors="replace")
        if ch == "\x1b":
            # Check if more bytes follow (escape sequence vs bare Esc)
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                ch2 = os.read(self.fd, 1).decode("utf-8", errors="replace")
                if ch2 == "[":
                    ch3 = os.read(self.fd, 1).decode("utf-8", errors="replace")
                    if "\x30" <= ch3 <= "\x3f":
                        # Multi-byte CSI: consume parameter/intermediate bytes
                        # (0x20-0x3F: digits, ';', etc.) through the final
                        # byte (0x40-0x7E) so nothing leaks into the next read.
                        params = ch3
                        while True:
                            nxt = os.read(self.fd, 1).decode(
                                "utf-8", errors="replace")
                            if "\x20" <= nxt <= "\x3f":
                                params += nxt
                                continue
                            final = nxt
                            break
                        if final == "~":
                            match params:
                                case "2":
                                    return "INSERT"
                                case "3":
                                    return "DELETE"
                                case "5":
                                    return "PGUP"
                                case "6":
                                    return "PGDN"
                                case _:
                                    return f"CSI{params}~"
                        if final == "Z":
                            # Parametric Shift-Tab, e.g. ESC[1;2Z
                            return "SHIFT_TAB"
                        # Mode 2031 theme-change notifications:
                        # CSI ?997;1n = dark, CSI ?997;2n = light
                        if final == "n":
                            if params == "?997;1":
                                return "THEME_DARK"
                            if params == "?997;2":
                                return "THEME_LIGHT"
                        return f"CSI{params}{final}"
                    match ch3:
                        case "A":
                            return "UP"
                        case "B":
                            return "DOWN"
                        case "C":
                            return "RIGHT"
                        case "D":
                            return "LEFT"
                        case "H":
                            return "HOME"
                        case "F":
                            return "END"
                        case "Z":
                            return "SHIFT_TAB"
                        case _:
                            return f"ESC[{ch3}"
                return "ESC"
            return "ESC"
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\t":
            return "TAB"
        if ch in ("\x7f", "\x08"):
            return "BACKSPACE"
        if ch == "\x03":
            return "CTRL_C"
        if ch == "\x04":
            return "CTRL_D"
        return ch

    def _write_tty(self, text: str) -> None:
        """Write directly to the TTY device."""
        self._tty_file.write(text.encode())
        self._tty_file.flush()

    def write(self, text: str) -> None:
        self._write_tty(text)

    def flush(self) -> None:
        self._tty_file.flush()

    def close(self) -> None:
        """Close the /dev/tty file handle."""
        if self._tty_file is not None and not self._tty_file.closed:
            self._tty_file.close()


def detect_terminal_background() -> str | None:
    """Detect whether the terminal has a light or dark background.

    Uses the OSC 11 query (background color request) with a DA1 sentinel
    to detect terminals that do not support OSC 11. Returns "light",
    "dark", or None (unsupported / timeout / error).

    Must be called BEFORE entering the TUI (before raw mode, before
    user input can race with the response).
    """
    term_env = os.environ.get("TERM", "")
    # Terminals known not to support OSC 11
    if term_env in ("dumb", "Eterm") or term_env.startswith("screen"):
        return None

    try:
        tty_file = open("/dev/tty", "r+b", buffering=0)
    except OSError:
        return None

    fd = tty_file.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        tty_file.close()
        return None

    try:
        tty.setcbreak(fd)

        # OSC 11 query (BEL terminator) + DA1 sentinel
        tty_file.write(b"\x1b]11;?\x07\x1b[c")
        tty_file.flush()

        result = _parse_osc11_response(fd)
        return result
    except OSError:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old_attrs)
        except termios.error:
            pass
        tty_file.close()


def detect_mode2031_support() -> str | None:
    """Detect whether the terminal supports Mode 2031 (theme-change notifications).

    Sends CSI ?996n (Mode 2031 query) + DA1 sentinel. If the terminal
    responds with CSI ?997;Xn (where X=1 for dark, X=2 for light),
    Mode 2031 is supported. Returns "dark", "light", or None.

    Must be called BEFORE entering the TUI.
    """
    term_env = os.environ.get("TERM", "")
    if term_env in ("dumb", "Eterm") or term_env.startswith("screen"):
        return None

    try:
        tty_file = open("/dev/tty", "r+b", buffering=0)
    except OSError:
        return None

    fd = tty_file.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        tty_file.close()
        return None

    try:
        tty.setcbreak(fd)

        # Mode 2031 query + DA1 sentinel
        tty_file.write(b"\x1b[?996n\x1b[c")
        tty_file.flush()

        result = _parse_mode2031_response(fd)
        return result
    except OSError:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old_attrs)
        except termios.error:
            pass
        tty_file.close()


def _parse_mode2031_response(fd: int) -> str | None:
    """Read and parse the Mode 2031 query response from the terminal.

    Returns "dark" (mode=1), "light" (mode=2), or None (unsupported/timeout).
    """
    r, _, _ = select.select([fd], [], [], _TERMINAL_QUERY_TIMEOUT)
    if not r:
        return None  # timeout

    # Read available bytes
    response = b""
    while True:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            break
        chunk = os.read(fd, 256)
        if not chunk:
            break
        response += chunk

    if not response:
        return None

    # Look for CSI ?997;Xn where X is 1 (dark) or 2 (light)
    # The sequence is: ESC [ ? 9 9 7 ; X n
    marker = b"\x1b[?997;"
    idx = response.find(marker)
    if idx < 0:
        return None

    # Parse the mode digit(s) after the marker, up to 'n'
    rest = response[idx + len(marker):]
    digits = b""
    for byte in rest:
        if byte == ord("n"):
            break
        digits += bytes([byte])
    else:
        return None  # no 'n' terminator found

    try:
        mode = int(digits)
    except ValueError:
        return None

    if mode == 1:
        return "dark"
    if mode == 2:
        return "light"
    return None


def _parse_osc11_response(fd: int) -> str | None:
    """Read and parse the OSC 11 response from the terminal.

    Returns "light", "dark", or None.
    """
    r, _, _ = select.select([fd], [], [], _TERMINAL_QUERY_TIMEOUT)
    if not r:
        return None  # timeout

    # Read available bytes (up to 256 is plenty for OSC 11 + DA1)
    response = b""
    while True:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            break
        chunk = os.read(fd, 256)
        if not chunk:
            break
        response += chunk

    if not response:
        return None

    # DA1 response starts with ESC[? -- if that's the first thing,
    # the terminal doesn't support OSC 11.
    if response.startswith(b"\x1b[?"):
        return None

    # Look for OSC 11 response: ESC]11;rgb:RRRR/GGGG/BBBB (terminated by BEL or ST)
    # The ESC] prefix may also be \x9d (8-bit OSC introducer).
    osc_start = -1
    for i, byte in enumerate(response):
        if byte == 0x1b and i + 1 < len(response) and response[i + 1] == ord("]"):
            osc_start = i + 2
            break
        if byte == 0x9d:
            osc_start = i + 1
            break

    if osc_start < 0:
        return None

    # Extract the payload up to BEL (\x07), ST (\x1b\\), or 8-bit ST (\x9c)
    payload = b""
    j = osc_start
    while j < len(response):
        if response[j] == 0x07:
            break
        if response[j] == 0x9c:
            break
        if response[j] == 0x1b and j + 1 < len(response) and response[j + 1] == ord("\\"):
            break
        payload += bytes([response[j]])
        j += 1

    # Payload should be like: 11;rgb:RRRR/GGGG/BBBB
    payload_str = payload.decode("ascii", errors="replace")
    if not payload_str.startswith("11;rgb:"):
        return None

    rgb_part = payload_str[7:]  # after "11;rgb:"
    return _classify_rgb(rgb_part)


def _classify_rgb(rgb_str: str) -> str | None:
    """Parse an rgb:RR/GG/BB (1-4 hex digits per channel) string and classify as light or dark.

    Returns "light" if perceived luminance > 0.5, "dark" otherwise, or None on parse error.
    """
    parts = rgb_str.split("/")
    if len(parts) != 3:
        return None

    channels: list[float] = []
    for part in parts:
        part = part.strip()
        if not part or len(part) > 4:
            return None
        try:
            val = int(part, 16)
        except ValueError:
            return None
        # Normalize to 0.0-1.0: the max value depends on the digit count
        max_val = (16 ** len(part)) - 1
        if max_val == 0:
            return None
        channels.append(val / max_val)

    r, g, b = channels

    # Linearize sRGB
    def linearize(c: float) -> float:
        if c <= 0.04045:
            return c / 12.92
        return float(((c + 0.055) / 1.055) ** 2.4)

    r_lin = linearize(r)
    g_lin = linearize(g)
    b_lin = linearize(b)

    # Perceived luminance (ITU-R BT.709)
    luminance = 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin

    return "light" if luminance > 0.5 else "dark"
