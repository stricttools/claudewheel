"""Save selections, launch count, and recent directories to state.json."""

from __future__ import annotations

import json
import os

from .config import ConfigManager
from .constants import INODES_FILE, STATE_FILE

# state.json key remembering the browser chosen in the auth wizard's
# "Choose browser" form (a browser binary path, or "copy").
AUTH_BROWSER_KEY = "auth_browser"


def _write_json_atomic(path, data) -> None:
    """Atomic tmp+rename JSON write that preserves the target's file mode.

    The tmp file is created with umask-default perms and the rename replaces
    the target inode, so without the chmod any pre-existing restrictive mode
    on the target would be silently lost on every update.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        tmp.chmod(path.stat().st_mode & 0o777)
    except FileNotFoundError:
        pass  # fresh file: umask default is fine
    tmp.rename(path)


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
    _write_json_atomic(STATE_FILE, data)


def merge_out_of_band_keys(state: dict) -> None:
    """Re-read keys written straight to state.json by out-of-band writers.

    The auth wizard writes AUTH_BROWSER_KEY directly to disk while the TUI
    holds its own in-memory state (loaded at startup). Any wholesale
    ConfigManager.save_state() must call this first so it doesn't clobber
    those fresh on-disk values with stale in-memory ones.
    """
    browser = load_state_value(AUTH_BROWSER_KEY)
    if browser is not None:
        state[AUTH_BROWSER_KEY] = browser


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

    merge_out_of_band_keys(cfg.state)
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
    _write_json_atomic(INODES_FILE, data)
