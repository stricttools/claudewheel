"""Filesystem paths, ANSI escape sequences, and terminal color helpers."""

from pathlib import Path


# --- Path Constants ---

CONFIG_DIR = Path.home() / ".claudewheel"
CONFIG_FILE = CONFIG_DIR / "config.json"
SEGMENTS_FILE = CONFIG_DIR / "segments.json"
OPTIONS_FILE = CONFIG_DIR / "options.json"
STATE_FILE = CONFIG_DIR / "state.json"
THEMES_DIR = CONFIG_DIR / "themes"
HOOKS_DIR = CONFIG_DIR / "hooks"
TOKENS_FILE = CONFIG_DIR / "tokens.json"

PROFILES_DIR = CONFIG_DIR / "profiles"

SHARED_SETTINGS_FILE = CONFIG_DIR / "shared-settings.json"

SCRIPTS_DIR = CONFIG_DIR / "scripts"

ORIGINS_FILE = CONFIG_DIR / "profile-origins.jsonl"

COMMON_DIR = Path.home() / ".claude-common"
SHARED_DIR = CONFIG_DIR / "shared"
SKILLS_DIR = CONFIG_DIR / "skills"
SENTINELS_DIR = SHARED_DIR / "sentinels"

# Directories inside each profile that are symlinked to the shared store.
PROFILE_SHARED_DIRS = ["projects", "session-env", "file-history", "tasks", "todos", "paste-cache"]

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
