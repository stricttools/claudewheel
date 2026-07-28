"""Persist launch state (selections, counts, recent dirs) and project inodes."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from .fsutil import write_json_atomic

if TYPE_CHECKING:
    from .appdata import StateFile
    from .config import AppConfigStore
    from .shared_store import SharedStore

# state.json key remembering the browser chosen in the auth wizard's
# "Choose browser" form (a browser binary path, or "copy").
AUTH_BROWSER_KEY = "auth_browser"

# state.json top-level key mapping a canonical project key (see project_key) to
# that project's recorded hook-approval decisions.
PROJECT_HOOK_APPROVALS_KEY = "project_hook_approvals"

# state.json top-level key mapping a canonical project key to whether the user
# opted that project into vanilla (unguarded) guardrails.
VANILLA_GUARDRAILS_OPT_IN_KEY = "vanilla_guardrails_opt_in"

# state.json top-level key holding the ISO-8601 timestamp before which the
# scratchpad-cleanup preflight step stays silent (a declined-prompt snooze).
SCRATCHPAD_SNOOZE_UNTIL_KEY = "scratchpad_snooze_until"

# The authoritative list of state.json keys written out-of-band (straight to
# disk via StateFile.set_value by writers that do NOT hold the TUI's in-memory
# state). StateFile.save re-reads these from disk and lets the disk copy win, so
# a wholesale save with stale in-memory state cannot clobber them. This tuple is
# the single source of truth -- StateFile.save imports it as its default.
OUT_OF_BAND_STATE_KEYS = (
    AUTH_BROWSER_KEY,
    PROJECT_HOOK_APPROVALS_KEY,
    VANILLA_GUARDRAILS_OPT_IN_KEY,
    SCRATCHPAD_SNOOZE_UNTIL_KEY,
)


def project_key(directory: str) -> str:
    """Return the canonical per-project state key for *directory*.

    This is THE rule for keying per-project state: the symlink-resolved
    (``os.path.realpath``) absolute path. A symlink and its target, and a
    relative path and its absolute form, all map to the same key so per-project
    state is never split across aliases of the same directory.
    """
    return os.path.realpath(directory)


def _get_project_value(
    sf: "StateFile", top_key: str, directory: str, default: Any = None
) -> Any:
    """Read a per-project sub-value from the *top_key* mapping in state.json."""
    mapping = sf.get_value(top_key, {})
    if not isinstance(mapping, dict):
        return default
    return mapping.get(project_key(directory), default)


def _set_project_value(
    sf: "StateFile", top_key: str, directory: str, value: Any
) -> None:
    """Write a per-project sub-value under *top_key*, via set_value (single-key,
    multi-session-safe read-modify-write of the top-level key)."""
    mapping = sf.get_value(top_key, {})
    if not isinstance(mapping, dict):
        mapping = {}
    mapping[project_key(directory)] = value
    sf.set_value(top_key, mapping)


def get_project_hook_approvals(
    sf: "StateFile", directory: str, default: Any = None
) -> Any:
    """Read the hook-approval record for *directory* (canonical key)."""
    return _get_project_value(sf, PROJECT_HOOK_APPROVALS_KEY, directory, default)


def set_project_hook_approvals(sf: "StateFile", directory: str, value: Any) -> None:
    """Persist the hook-approval record for *directory* (canonical key)."""
    _set_project_value(sf, PROJECT_HOOK_APPROVALS_KEY, directory, value)


def get_vanilla_guardrails_opt_in(
    sf: "StateFile", directory: str, default: Any = None
) -> Any:
    """Read the vanilla-guardrails opt-in flag for *directory* (canonical key)."""
    return _get_project_value(sf, VANILLA_GUARDRAILS_OPT_IN_KEY, directory, default)


def set_vanilla_guardrails_opt_in(
    sf: "StateFile", directory: str, value: Any
) -> None:
    """Persist the vanilla-guardrails opt-in flag for *directory* (canonical key)."""
    _set_project_value(sf, VANILLA_GUARDRAILS_OPT_IN_KEY, directory, value)


def get_scratchpad_snooze_until(sf: "StateFile", default: Any = None) -> Any:
    """Read the scratchpad-cleanup snooze deadline (ISO-8601 string) from state."""
    return sf.get_value(SCRATCHPAD_SNOOZE_UNTIL_KEY, default)


def set_scratchpad_snooze_until(sf: "StateFile", value: str) -> None:
    """Persist the scratchpad-cleanup snooze deadline (ISO-8601 string)."""
    sf.set_value(SCRATCHPAD_SNOOZE_UNTIL_KEY, value)


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
