"""ConfigManager class for claudewheel."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .constants import (
    LAUNCHER_DIR,
    CONFIG_FILE,
    SEGMENTS_FILE,
    OPTIONS_FILE,
    STATE_FILE,
    THEMES_DIR,
    HOOKS_DIR,
)
from .defaults import (
    DEFAULT_CONFIG,
    DEFAULT_SEGMENTS,
    DEFAULT_OPTIONS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
)


@dataclass
class ConfigManager:
    config: dict = field(default_factory=dict)
    segments_def: list[dict] = field(default_factory=list)
    options_def: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    theme: dict = field(default_factory=dict)

    def __post_init__(self):
        self._ensure_dir()
        self.config = self._load_json(CONFIG_FILE, DEFAULT_CONFIG)
        self.segments_def = self._load_json(SEGMENTS_FILE, DEFAULT_SEGMENTS)
        self.options_def = self._load_json(OPTIONS_FILE, DEFAULT_OPTIONS)
        self.state = self._load_json(STATE_FILE, DEFAULT_STATE)
        theme_name = self.config.get("theme", "dark")
        theme_file = THEMES_DIR / f"{theme_name}.json"
        theme_default = DEFAULT_THEME_LIGHT if theme_name == "light" else DEFAULT_THEME_DARK
        self.theme = self._load_json(theme_file, theme_default)

    def _ensure_dir(self):
        """Create config directories and write default files on first run."""
        LAUNCHER_DIR.mkdir(exist_ok=True)
        THEMES_DIR.mkdir(exist_ok=True)
        HOOKS_DIR.mkdir(exist_ok=True)
        for path, default in [
            (CONFIG_FILE, DEFAULT_CONFIG),
            (SEGMENTS_FILE, DEFAULT_SEGMENTS),
            (OPTIONS_FILE, DEFAULT_OPTIONS),
            (STATE_FILE, DEFAULT_STATE),
            (THEMES_DIR / "dark.json", DEFAULT_THEME_DARK),
            (THEMES_DIR / "light.json", DEFAULT_THEME_LIGHT),
        ]:
            if not path.exists():
                self._save_json(path, default)

    def _load_json(self, path: Path, default: dict | list) -> dict | list:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _save_json(self, path: Path, data: dict | list) -> None:
        """Atomic write via tmp-file rename."""
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        tmp.rename(path)

    def add_option(self, segment_key: str, value: str) -> None:
        """Add a new option value to options.json for the given segment."""
        options = self._load_json(OPTIONS_FILE, self.options_def)
        if segment_key not in options:
            options[segment_key] = {"values": []}
        values = options[segment_key].get("values", [])
        if value not in values:
            values.append(value)
            options[segment_key]["values"] = values
            self._save_json(OPTIONS_FILE, options)
            # Also update in-memory copy
            self.options_def = options

    def set_option_metadata(self, segment_key: str, value: str, meta: dict) -> None:
        """Set metadata for a specific option value in options.json."""
        options = self._load_json(OPTIONS_FILE, self.options_def)
        seg = options.setdefault(segment_key, {"values": []})
        seg.setdefault("metadata", {})[value] = meta
        self._save_json(OPTIONS_FILE, options)
        self.options_def = options

    def save_state(self):
        self._save_json(STATE_FILE, self.state)
