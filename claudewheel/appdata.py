"""Atomic single-owner accessors for options.json and state.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .fsutil import write_json_atomic


@dataclass
class OptionsFile:
    """Single-owner accessor for options.json (read-modify-write, atomic)."""

    path: Path

    def load(self, default: dict) -> dict:
        """Read options.json fresh from disk; return *default* if missing/corrupt."""
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def add_pinned(self, segment_key: str, value: str, default: dict) -> dict:
        """Append *value* to a segment's pinned list, write, and return the fresh dict.

        Fresh read (falling back to *default*), ensures the segment dict and its
        pinned list exist, and appends only when *value* is not already pinned.
        The file is written only when a new value is actually appended.
        """
        options = self.load(default)
        if segment_key not in options:
            options[segment_key] = {"values": [], "pinned": []}
        pinned = options[segment_key].setdefault("pinned", [])
        if value not in pinned:
            pinned.append(value)
            write_json_atomic(self.path, options)
        return options

    def set_metadata(self, segment_key: str, value: str, meta: dict, default: dict) -> dict:
        """Set metadata for a segment value, write, and return the fresh dict."""
        options = self.load(default)
        seg = options.setdefault(segment_key, {"values": []})
        seg.setdefault("metadata", {})[value] = meta
        write_json_atomic(self.path, options)
        return options

    def write(self, data: dict) -> None:
        """Bare atomic write of the full options dict (used by config migrations)."""
        write_json_atomic(self.path, data)


@dataclass
class StateFile:
    """Single-owner accessor for state.json (read-modify-write, atomic)."""

    path: Path

    def load(self, default: dict) -> dict:
        """Read state.json fresh from disk; return *default* if missing/corrupt."""
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def save(self, state: dict, out_of_band_keys: tuple[str, ...] = ("auth_browser",)) -> None:
        """Write *state* to disk, letting fresh on-disk out-of-band keys win.

        Re-reads the disk copy and, for each name in *out_of_band_keys*, copies a
        non-None disk value into *state* before writing. This prevents a wholesale
        save from clobbering values written straight to disk by out-of-band writers
        (e.g. the auth wizard's auth_browser) with stale in-memory state.
        """
        try:
            on_disk = json.loads(self.path.read_text())
            if isinstance(on_disk, dict):
                for key in out_of_band_keys:
                    disk_val = on_disk.get(key)
                    if disk_val is not None:
                        state[key] = disk_val
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        write_json_atomic(self.path, state)

    def get_value(self, key: str, default=None):
        """Read a single key fresh from disk; return *default* if unavailable."""
        if not self.path.exists():
            return default
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return default
        if not isinstance(data, dict):
            return default
        return data.get(key, default)

    def set_value(self, key: str, value) -> None:
        """Read-modify-write a single key, preserving all other keys on disk."""
        data: dict = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                pass
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, data)
