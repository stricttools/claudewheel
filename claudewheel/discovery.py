"""Scan the filesystem for Claude Code profiles and their credentials."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .constants import PROFILES_DIR, TOKENS_FILE


@dataclass
class ProfileInfo:
    """A profile's name, path, and credential/token presence."""

    name: str
    path: Path
    has_credentials: bool
    has_token: bool


def discover_profiles() -> list[ProfileInfo]:
    """Discover all Claude Code profiles on this machine.

    Scans ~/.claudewheel/profiles/ for subdirectories. Also checks bare
    ~/.claude/ as the "default" profile (Claude Code's built-in default,
    not a claudewheel profile). A directory qualifies as a profile if it
    has .credentials.json, settings.json, or has a matching entry in
    tokens.json.

    Returns a sorted list of ProfileInfo.
    """
    home = Path.home()
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

    # Scan ~/.claudewheel/profiles/ subdirectories
    if PROFILES_DIR.is_dir():
        for entry in sorted(PROFILES_DIR.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if not name:
                continue
            has_credentials = (entry / ".credentials.json").exists()
            has_settings = (entry / "settings.json").exists()
            if has_credentials or has_settings:
                profiles.append(ProfileInfo(
                    name=name, path=entry,
                    has_credentials=has_credentials, has_token=False,
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
                    pdir = PROFILES_DIR / key
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


# Native browser binaries searched on PATH, in priority order.
_NATIVE_BROWSERS: list[tuple[str, str]] = [
    ("firefox", "Firefox"),
    ("chromium", "Chromium"),
    ("chromium-browser", "Chromium"),
    ("google-chrome", "Chrome"),
    ("google-chrome-stable", "Chrome"),
    ("brave-browser", "Brave"),
    ("brave", "Brave"),
    ("microsoft-edge", "Edge"),
    ("microsoft-edge-stable", "Edge"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
    ("vivaldi-stable", "Vivaldi"),
    ("epiphany", "GNOME Web"),
    ("midori", "Midori"),
    ("falkon", "Falkon"),
    ("qutebrowser", "Qutebrowser"),
]

# Flatpak export symlink directories (system, then user). Module-level so
# tests can patch them.
_FLATPAK_EXPORT_DIRS: list[Path] = [
    Path("/var/lib/flatpak/exports/bin"),
    Path.home() / ".local/share/flatpak/exports/bin",
]

_FLATPAK_BROWSERS: list[tuple[str, str]] = [
    ("org.mozilla.firefox", "Firefox"),
    ("com.google.Chrome", "Chrome"),
    ("com.brave.Browser", "Brave"),
    ("io.github.ungoogled_software.ungoogled_chromium", "Ungoogled Chromium"),
    ("com.microsoft.Edge", "Edge"),
    ("com.opera.Opera", "Opera"),
    ("com.vivaldi.Vivaldi", "Vivaldi"),
]

# Snap binary directory. Module-level so tests can patch it.
_SNAP_BIN_DIR: Path = Path("/snap/bin")

_SNAP_BROWSERS: list[tuple[str, str]] = [
    ("firefox", "Firefox"),
    ("chromium", "Chromium"),
    ("brave", "Brave"),
    ("opera", "Opera"),
]


def detect_browsers() -> list[tuple[str, str]]:
    """Detect installed web browsers.

    Returns (binary_path, display_name) pairs -- path first, because the
    results are concatenated with (key, label) selection-form options where
    the key is the path. Detection order is native (PATH), then flatpak
    exports, then snap. Deduplicated by display name: the first source that
    finds a browser wins.
    """
    browsers: list[tuple[str, str]] = []
    seen_names: set[str] = set()

    for binary, name in _NATIVE_BROWSERS:
        if name in seen_names:
            continue
        path = shutil.which(binary)
        if path:
            browsers.append((path, name))
            seen_names.add(name)

    for app_id, name in _FLATPAK_BROWSERS:
        if name in seen_names:
            continue
        for export_dir in _FLATPAK_EXPORT_DIRS:
            path = export_dir / app_id
            if path.exists():
                browsers.append((str(path), name))
                seen_names.add(name)
                break

    for binary, name in _SNAP_BROWSERS:
        if name in seen_names:
            continue
        path = _SNAP_BIN_DIR / binary
        if path.exists():
            browsers.append((str(path), name))
            seen_names.add(name)

    return browsers
