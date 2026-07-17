"""Detect installed web browsers across native, flatpak, and snap sources.

Profile enumeration and shared-store classification now live on
``ProfileStore`` (workspace layer); this module is the browser/binary
detection module used by the profile-creation wizard.
"""

from __future__ import annotations

import shutil
from pathlib import Path


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
            candidate = export_dir / app_id
            if candidate.exists():
                browsers.append((str(candidate), name))
                seen_names.add(name)
                break

    for binary, name in _SNAP_BROWSERS:
        if name in seen_names:
            continue
        candidate = _SNAP_BIN_DIR / binary
        if candidate.exists():
            browsers.append((str(candidate), name))
            seen_names.add(name)

    return browsers
