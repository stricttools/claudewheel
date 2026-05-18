"""Terminal class for raw-mode input handling and screen management."""

from __future__ import annotations

import atexit
import fcntl
import os
import select
import shutil
import struct
import sys
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

    def get_size(self) -> tuple[int, int]:
        try:
            packed = fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("hhhh", packed)
            return rows, cols
        except OSError:
            size = shutil.get_terminal_size()
            return size.lines, size.columns

    def enter_raw(self) -> None:
        self.old_attrs = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)  # cbreak, not raw -- lets Ctrl-C generate SIGINT
        self._in_raw = True
        self.rows, self.cols = self.get_size()
        self._write_tty(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR_SCREEN)
        atexit.register(self.exit_raw)

    def exit_raw(self) -> None:
        if self._in_raw and self.old_attrs is not None:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self.old_attrs)
            self._write_tty(SHOW_CURSOR + ALT_SCREEN_OFF)
            self._in_raw = False

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
        return ch

    def _write_tty(self, text: str) -> None:
        """Write directly to the TTY device."""
        self._tty_file.write(text.encode())
        self._tty_file.flush()

    def write(self, text: str) -> None:
        self._write_tty(text)

    def flush(self) -> None:
        self._tty_file.flush()
