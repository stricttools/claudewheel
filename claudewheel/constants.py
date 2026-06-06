"""ANSI escape helpers and path constants for claudewheel."""

from pathlib import Path


# --- Path Constants ---

LAUNCHER_DIR = Path.home() / ".claudewheel"
CONFIG_FILE = LAUNCHER_DIR / "config.json"
SEGMENTS_FILE = LAUNCHER_DIR / "segments.json"
OPTIONS_FILE = LAUNCHER_DIR / "options.json"
STATE_FILE = LAUNCHER_DIR / "state.json"
THEMES_DIR = LAUNCHER_DIR / "themes"
HOOKS_DIR = LAUNCHER_DIR / "hooks"
TOKENS_FILE = LAUNCHER_DIR / "tokens.json"

VERSIONS_DIR = Path.home() / ".local/share/claude/versions"
CLAUDE_SYMLINK = Path.home() / ".local/bin/claude"

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
