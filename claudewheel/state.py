"""Save selections, launch count, and recent directories to state.json."""

from __future__ import annotations

import json
import os

from .config import ConfigManager
from .constants import INODES_FILE, STATE_FILE

# state.json key remembering the browser chosen in the auth wizard's
# "Choose browser" form (a browser binary path, or "copy").
AUTH_BROWSER_KEY = "auth_browser"


def load_state_value(key: str):
    """Read a single value fresh from state.json on disk.

    Returns None if the file is missing, unreadable, or lacks the key.
    Unlike ConfigManager.state, this never uses an in-memory copy -- it is
    for code paths (e.g., the auth wizard) that run outside the TUI's
    ConfigManager lifecycle.
    """
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get(key)


def save_state_value(key: str, value) -> None:
    """Read-modify-write a single key in state.json (atomic tmp + rename).

    Only *key* is touched; all other keys on disk are preserved. Counterpart
    of load_state_value() for writers that don't hold a ConfigManager.
    """
    data: dict = {}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass
    data[key] = value
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(STATE_FILE)


def save_launch_state(cfg: ConfigManager, selections: dict[str, str | None]) -> None:
    """Save current selections to state.json before launch."""
    # Save last_config (only non-None values)
    cfg.state["last_config"] = {k: v for k, v in selections.items() if v is not None}
    cfg.state["launch_count"] = cfg.state.get("launch_count", 0) + 1

    # Update recent_dirs (deduplicate, cap at 20)
    directory = selections.get("directory")
    if directory:
        recent = cfg.state.get("recent_dirs", [])
        if directory in recent:
            recent.remove(directory)
        recent.insert(0, directory)
        cfg.state["recent_dirs"] = recent[:20]

    # The auth wizard writes AUTH_BROWSER_KEY straight to state.json while
    # the TUI holds its own in-memory state (loaded at startup); re-read the
    # key from disk so this wholesale save doesn't clobber the wizard's write.
    browser = load_state_value(AUTH_BROWSER_KEY)
    if browser is not None:
        cfg.state[AUTH_BROWSER_KEY] = browser

    cfg.save_state()


def record_inode(directory: str) -> None:
    """Record the inode of a project directory for rename detection."""
    path = os.path.abspath(directory)
    try:
        inode = os.stat(path).st_ino
    except OSError:
        return

    # Load existing inode map
    data: dict[str, int] = {}
    if INODES_FILE.exists():
        try:
            data = json.loads(INODES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # If this path already has this inode, nothing to do
    if data.get(path) == inode:
        return

    # Record the new path -> inode mapping
    data[path] = inode

    # Atomic write: tmp + rename
    INODES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INODES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(INODES_FILE)
