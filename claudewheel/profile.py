"""Standalone profile resolution for programmatic callers."""

from __future__ import annotations

import json

from .config import ConfigManager
from .constants import TOKENS_FILE


def resolve_profile(name: str) -> dict[str, str]:
    """Return env vars (CLAUDE_CONFIG_DIR, CLAUDE_CODE_OAUTH_TOKEN) for a profile."""
    mgr = ConfigManager()

    meta = mgr.options_def.get("profile", {}).get("metadata", {})
    if name not in meta:
        available = sorted(meta.keys())
        raise ValueError(
            f"Profile {name!r} not found in options.json metadata. "
            f"Available profiles: {available}"
        )

    profile_meta = meta[name]
    if "config_dir" not in profile_meta:
        raise ValueError(
            f"Profile {name!r} has no config_dir in its metadata."
        )

    from pathlib import Path

    env: dict[str, str] = {
        "CLAUDE_CONFIG_DIR": str(Path(profile_meta["config_dir"]).expanduser()),
    }

    # Token lookup -- supports both {name: "token"} and {name: {token, created}}.
    if TOKENS_FILE.is_file():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            entry = tokens.get(name)
            if isinstance(entry, str):
                env["CLAUDE_CODE_OAUTH_TOKEN"] = entry
            elif isinstance(entry, dict) and entry.get("token"):
                env["CLAUDE_CODE_OAUTH_TOKEN"] = entry["token"]
        except (json.JSONDecodeError, OSError):
            pass

    return env
