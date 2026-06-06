"""Scan the filesystem for Claude Code profiles and their credentials."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .constants import COMMON_DIR, SHARED_DIR, TOKENS_FILE


@dataclass
class ProfileInfo:
    """A profile's name, path, and credential/token presence."""

    name: str
    path: Path
    has_credentials: bool
    has_token: bool


def discover_profiles() -> list[ProfileInfo]:
    """Discover all Claude Code profiles on this machine.

    Scans ~/ for .claude-*/ directories, skipping .claude-shared and
    .claude-common. Also checks bare ~/.claude/ as the "default" profile.
    A directory qualifies as a profile if it has .credentials.json or has
    a matching entry in tokens.json.

    Returns a sorted list of ProfileInfo.
    """
    home = Path.home()
    skip_names = {SHARED_DIR.name, COMMON_DIR.name}
    profiles: list[ProfileInfo] = []
    found_names: set[str] = set()

    # Check bare ~/.claude/ as "default" profile
    default_dir = home / ".claude"
    if default_dir.is_dir() and (default_dir / ".credentials.json").exists():
        profiles.append(ProfileInfo(
            name="default", path=default_dir,
            has_credentials=True, has_token=False,
        ))
        found_names.add("default")

    # Scan ~/.claude-* directories
    for entry in sorted(home.iterdir()):
        if not entry.is_dir() or not entry.name.startswith(".claude-"):
            continue
        if entry.name in skip_names:
            continue
        name = entry.name[len(".claude-"):]
        if not name:
            continue
        has_credentials = (entry / ".credentials.json").exists()
        if has_credentials:
            profiles.append(ProfileInfo(
                name=name, path=entry,
                has_credentials=True, has_token=False,
            ))
            found_names.add(name)

    # Check tokens.json for profiles with dirs but no .credentials.json
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
        for key in tokens:
            if key not in found_names:
                if key == "default":
                    pdir = home / ".claude"
                else:
                    pdir = home / f".claude-{key}"
                if pdir.is_dir():
                    profiles.append(ProfileInfo(
                        name=key, path=pdir,
                        has_credentials=False, has_token=True,
                    ))
                    found_names.add(key)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # Mark token presence on credential-discovered profiles
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
        for p in profiles:
            if p.name in tokens and not p.has_token:
                p.has_token = True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    profiles.sort(key=lambda p: p.name)
    return profiles
