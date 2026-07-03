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

from .constants import (ALT_SCREEN_ON, ALT_SCREEN_OFF, HIDE_CURSOR, SHOW_CURSOR, CLEAR_SCREEN)


class Terminal:
    """Low-level terminal I/O: raw mode, key reading, alt screen, and size detection."""

    def __init__(self):
        # Open /dev/tty directly so we work even when stdin is piped
        self._tty_file = open("/dev/tty", "r+b", buffering=0)
        self.fd = self._tty_file.fileno()
        self.old_attrs = None
        self.rows = 24
        self.cols = 80
        self._in_raw = False
        self._alt_screen = True

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

    def exit_raw(self) -> None:
        if self._in_raw and self.old_attrs is not None:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old_attrs)
            if self._alt_screen:
                self._write_tty(SHOW_CURSOR + ALT_SCREEN_OFF)
            else:
                self._write_tty(SHOW_CURSOR)
            self._in_raw = False

    @contextlib.contextmanager
    def cooked(self):
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
                    if ch3.isdigit():
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
