"""Persist launch state (selections, counts, recent dirs) and project inodes."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from .fsutil import write_json_atomic

if TYPE_CHECKING:
    from .config import AppConfigStore
    from .shared_store import SharedStore

# state.json key remembering the browser chosen in the auth wizard's
# "Choose browser" form (a browser binary path, or "copy").
AUTH_BROWSER_KEY = "auth_browser"


def save_launch_state(cfg: "AppConfigStore", selections: dict[str, str | None]) -> None:
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


def record_inode(shared: "SharedStore", directory: str) -> None:
    """Record the inode of a project directory for rename detection."""
    path = os.path.abspath(directory)
    try:
        inode = os.stat(path).st_ino
    except OSError:
        return

    inodes_file = shared.inodes_file

    # Load existing inode map
    data: dict[str, int] = {}
    if inodes_file.exists():
        try:
            data = json.loads(inodes_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # If this path already has this inode, nothing to do
    if data.get(path) == inode:
        return

    # Record the new path -> inode mapping
    data[path] = inode

    # Atomic write: tmp + rename
    inodes_file.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(inodes_file, data)
