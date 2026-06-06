"""Save selections, launch count, and recent directories to state.json."""

from __future__ import annotations

from .config import ConfigManager


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
