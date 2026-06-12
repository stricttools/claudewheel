"""Config loading, saving, and schema migration system."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from .constants import (
    CONFIG_DIR,
    CONFIG_FILE,
    SEGMENTS_FILE,
    OPTIONS_FILE,
    STATE_FILE,
    THEMES_DIR,
    HOOKS_DIR,
    SCRIPTS_DIR,
    SHARED_DIR,
    SHARED_SETTINGS_FILE,
)
from .defaults import (
    DEFAULT_CONFIG,
    DEFAULT_SEGMENTS,
    DEFAULT_OPTIONS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
    build_canonical_shared_settings,
)


# ---------------------------------------------------------------------------
# Versioned migrations
# ---------------------------------------------------------------------------
# Each migration targets a schema version. It runs exactly once: when the
# config's _schema_version is less than the migration's version number.
# The callable receives (config, segments_def, theme) and mutates in place.


def _migration_1_github_optional(
    config: dict, segments_def: list[dict], theme: dict, options_def: dict,
) -> None:
    """Make github segment optional (was incorrectly required)."""
    for seg in segments_def:
        if seg.get("key") == "github" and seg.get("required") is True:
            seg["required"] = False


def _migration_2_profile_paths(
    config: dict, segments_def: list[dict], theme: dict, options_def: dict,
) -> None:
    """Rewrite profile metadata config_dir from ~/.claude-<name> to ~/.claudewheel/profiles/<name>."""
    import re
    metadata = options_def.get("profile", {}).get("metadata", {})
    for name, meta in metadata.items():
        cd = meta.get("config_dir", "")
        # Match old-style paths like ~/.claude-work but not ~/.claude (bare default)
        # and not ~/.claudewheel/profiles/... (already migrated)
        m = re.match(r"^~/\.claude-(.+)$", cd)
        if m:
            meta["config_dir"] = f"~/.claudewheel/profiles/{m.group(1)}"


_MIGRATIONS: list[dict] = [
    {
        "version": 1,
        "description": "Make github segment optional",
        "apply": _migration_1_github_optional,
    },
    {
        "version": 2,
        "description": "Rewrite profile metadata paths to ~/.claudewheel/profiles/<name>/",
        "apply": _migration_2_profile_paths,
    },
]


@dataclass
class ConfigManager:
    """Manages the four JSON config files (config, segments, options, state) and runs migrations on init."""

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
        self._migrate(theme_file, theme_default)
        self._run_versioned_migrations(theme_file)
        self._ensure_shared_settings()
        self._warn_old_profile_dirs()

    @staticmethod
    def _warn_old_profile_dirs() -> None:
        """Print a warning if old-style ~/.claude-<name>/ profile dirs exist."""
        home = Path.home()
        skip = {".claude-shared", ".claude-common", ".claude"}
        old_dirs: list[str] = []
        try:
            for entry in sorted(home.iterdir()):
                if not entry.is_dir() or not entry.name.startswith(".claude-"):
                    continue
                if entry.name in skip:
                    continue
                old_dirs.append(f"~/{entry.name}")
        except OSError:
            return
        if old_dirs:
            dirs_str = ", ".join(old_dirs)
            print(
                f"Warning: Found old-style profile directories: {dirs_str}. "
                "Move them to ~/.claudewheel/profiles/<name>/ and delete the originals.",
                file=sys.stderr,
            )

    def _ensure_shared_settings(self) -> None:
        """Create shared-settings.json from canonical values if it doesn't exist."""
        if not SHARED_SETTINGS_FILE.exists():
            canonical = build_canonical_shared_settings(SCRIPTS_DIR)
            self._save_json(SHARED_SETTINGS_FILE, canonical)

    def _ensure_dir(self):
        """Create config directories and write default files on first run."""
        CONFIG_DIR.mkdir(exist_ok=True)
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

    def _migrate(self, theme_file: Path, theme_default: dict) -> None:
        """Add missing default keys to existing config files on startup.

        Only adds keys that are absent — never overwrites existing user values.
        Saves each file only when something actually changed, so running twice
        is a no-op (idempotent).
        """
        # 1. config.json — flat dict, add missing top-level keys
        changed = False
        for key, value in DEFAULT_CONFIG.items():
            if key not in self.config:
                self.config[key] = value
                changed = True
        if changed:
            self._save_json(CONFIG_FILE, self.config)

        # 2. segments.json — list of dicts matched by "key" field
        seg_by_key = {s["key"]: s for s in self.segments_def if "key" in s}
        changed = False
        for default_seg in DEFAULT_SEGMENTS:
            dk = default_seg.get("key")
            if dk is None or dk not in seg_by_key:
                continue  # skip segments the user intentionally removed
            user_seg = seg_by_key[dk]
            for attr, value in default_seg.items():
                if attr not in user_seg:
                    user_seg[attr] = value
                    changed = True
        if changed:
            self._save_json(SEGMENTS_FILE, self.segments_def)

        # 3. theme file — nested dict, recursively merge missing keys
        changed = self._deep_merge_missing(self.theme, theme_default)
        if changed:
            self._save_json(theme_file, self.theme)

        # 4. options.json -- sync default model values into user's list
        default_models = DEFAULT_OPTIONS.get("model", {}).get("values", [])
        user_models = self.options_def.get("model", {}).get("values", [])
        new_models = [m for m in default_models if m not in user_models]
        if new_models:
            user_models.extend(new_models)
            self._save_json(OPTIONS_FILE, self.options_def)

    def _run_versioned_migrations(self, theme_file: Path) -> None:
        """Run schema-versioned migrations that change existing values.

        Complements _migrate() which only adds missing keys. Versioned
        migrations can mutate values and run exactly once per version bump.
        """
        current_version = self.config.get("_schema_version", 0)
        highest_applied = current_version
        config_changed = False
        segments_changed = False
        theme_changed = False
        options_changed = False

        for migration in _MIGRATIONS:
            if migration["version"] > current_version:
                # Snapshot segments/theme/options to detect mutations
                seg_before = json.dumps(self.segments_def, sort_keys=True)
                theme_before = json.dumps(self.theme, sort_keys=True)
                options_before = json.dumps(self.options_def, sort_keys=True)

                migration["apply"](self.config, self.segments_def, self.theme, self.options_def)

                if json.dumps(self.segments_def, sort_keys=True) != seg_before:
                    segments_changed = True
                if json.dumps(self.theme, sort_keys=True) != theme_before:
                    theme_changed = True
                if json.dumps(self.options_def, sort_keys=True) != options_before:
                    options_changed = True

                highest_applied = max(highest_applied, migration["version"])

        if highest_applied > current_version:
            self.config["_schema_version"] = highest_applied
            config_changed = True

        if config_changed:
            self._save_json(CONFIG_FILE, self.config)
        if segments_changed:
            self._save_json(SEGMENTS_FILE, self.segments_def)
        if theme_changed:
            self._save_json(theme_file, self.theme)
        if options_changed:
            self._save_json(OPTIONS_FILE, self.options_def)

    @staticmethod
    def _deep_merge_missing(target: dict, defaults: dict) -> bool:
        """Recursively add keys from *defaults* that are absent in *target*.

        Returns True if any key was added (i.e. the target was mutated).
        """
        changed = False
        for key, default_value in defaults.items():
            if key not in target:
                target[key] = copy.deepcopy(default_value)
                changed = True
            elif isinstance(target[key], dict) and isinstance(default_value, dict):
                if ConfigManager._deep_merge_missing(target[key], default_value):
                    changed = True
        return changed

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
