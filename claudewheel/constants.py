"""ANSI escape sequences and terminal color helpers.

Filesystem paths live on the workspace/store layer (``Workspace`` and its
``ProfileStore``/``TokenStore``/``SharedStore``/``OptionsFile``/``StateFile``/
``BinaryLocator`` members); the path-encoding codec lives on ``SharedStore``.
This module is now purely the terminal/ANSI primitives shared across the
renderer, terminal, and UI layers.
"""

# ANSI escape helpers

ESC = "\033"


def csi(code: str) -> str:
    """Build a CSI escape sequence from the given parameter string."""
    return f"{ESC}[{code}"


def move_to(row: int, col: int) -> str:
    """Return an ANSI escape sequence that moves the cursor to the given row and column."""
    return csi(f"{row};{col}H")


def fg_rgb(r: int, g: int, b: int) -> str:
    """Return an ANSI escape sequence for setting the foreground to an RGB color."""
    return csi(f"38;2;{r};{g};{b}m")


def bg_rgb(r: int, g: int, b: int) -> str:
    """Return an ANSI escape sequence for setting the background to an RGB color."""
    return csi(f"48;2;{r};{g};{b}m")


RESET = csi("0m")
BOLD = csi("1m")
DIM = csi("2m")
INVERSE = csi("7m")
HIDE_CURSOR = csi("?25l")
SHOW_CURSOR = csi("?25h")
ALT_SCREEN_ON = csi("?1049h")
ALT_SCREEN_OFF = csi("?1049l")
CLEAR_SCREEN = csi("2J")
CLEAR_LINE = csi("2K")
