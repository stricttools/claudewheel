"""Save selections, launch count, and recent directories to state.json."""

from __future__ import annotations

import json
import os

from .config import ConfigManager
from .constants import INODES_FILE


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
