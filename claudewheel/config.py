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
    SHARED_DIR,  # noqa: F401 -- re-exported; tests patch claudewheel.config.SHARED_DIR
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
from .fsutil import write_json_atomic
from .terminal import detect_terminal_background


# ---------------------------------------------------------------------------
# Historical defaults -- every value that ever appeared in DEFAULT_OPTIONS
# ---------------------------------------------------------------------------
# Built by auditing every commit that touched defaults.py. Used by migration 3
# to clean up stale hardcoded values from user options.json files.

HISTORICAL_DEFAULTS: dict[str, set[str]] = {
    "model": {
        "opus",
        "sonnet",
        "haiku",
        "claude-opus-4-6",
        "claude-opus-4-6[1m]",
        "claude-opus-4-7",
        "claude-opus-4-7[1m]",
        "claude-sonnet-4-6",
        "claude-sonnet-4-6[1m]",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5-20241022",
        "claude-fable-5",
        "claude-fable-5[1m]",
    },
    "mcp": {"default", "strict"},
    "permissions": {"bypass", "default", "plan", "auto"},
}

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


def _migration_3_classify_pinned(
    config: dict, segments_def: list[dict], theme: dict, options_def: dict,
) -> None:
    """Classify existing 'values' into 'pinned' vs discard.

    Discovery-backed segments: values with metadata -> pinned (wizard-created),
    values without metadata -> discard (from discovery, will be re-discovered).

    Static segments (no discovery): values in HISTORICAL_DEFAULTS that are
    still in DEFAULT_OPTIONS -> discard (they come from defaults now). Values
    in HISTORICAL_DEFAULTS but NOT in current defaults -> pinned (conservative).
    Values not in any defaults -> pinned (user-added).
    """
    for key, seg_entry in options_def.items():
        if "values" not in seg_entry:
            continue
        seg_entry.setdefault("pinned", [])
        has_discovery = "discovery" in seg_entry
        values = seg_entry["values"]
        metadata = seg_entry.get("metadata", {})

        if has_discovery:
            # Discovery-backed segment: values with metadata are wizard-created
            for val in values:
                if val in metadata and val not in seg_entry["pinned"]:
                    seg_entry["pinned"].append(val)
        else:
            # Static segment: classify against historical and current defaults
            historical = HISTORICAL_DEFAULTS.get(key, set())
            current_defaults = set(DEFAULT_OPTIONS.get(key, {}).get("values", []))
            for val in values:
                if val in historical and val in current_defaults:
                    # Still a current default -- discard (defaults handle it)
                    continue
                if val in historical and val not in current_defaults:
                    # Was a default, removed from current -- keep as pinned (conservative)
                    if val not in seg_entry["pinned"]:
                        seg_entry["pinned"].append(val)
                else:
                    # Not in any historical defaults -- user-added, keep as pinned
                    if val not in seg_entry["pinned"]:
                        seg_entry["pinned"].append(val)
        # Keep "values" as-is for backward compat with code that still reads it


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
    {
        "version": 3,
        "description": "Classify option values into pinned vs defaults",
        "apply": _migration_3_classify_pinned,
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
        theme_name = self._resolve_theme_name(self.config.get("theme", "auto"))
        theme_file = THEMES_DIR / f"{theme_name}.json"
        theme_default = DEFAULT_THEME_LIGHT if theme_name == "light" else DEFAULT_THEME_DARK
        self.theme = self._load_json(theme_file, theme_default)
        self._migrate(theme_file, theme_default)
        self._run_versioned_migrations(theme_file)
        self._ensure_shared_settings()
        self._warn_old_profile_dirs()

    @staticmethod
    def _resolve_theme_name(theme_name: str) -> str:
        """Resolve 'auto' theme to 'light' or 'dark' via terminal detection.

        Explicit 'light' or 'dark' are returned as-is. 'auto' queries the
        terminal background color; detection failure falls back to 'dark'.
        """
        if theme_name != "auto":
            return theme_name
        detected = detect_terminal_background()
        return detected if detected in ("light", "dark") else "dark"

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
            write_json_atomic(SHARED_SETTINGS_FILE, canonical)

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
                write_json_atomic(path, default)

    def _load_json(self, path: Path, default: dict | list) -> dict | list:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

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
            write_json_atomic(CONFIG_FILE, self.config)

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
            write_json_atomic(SEGMENTS_FILE, self.segments_def)

        # 3. theme file — nested dict, recursively merge missing keys
        changed = self._deep_merge_missing(self.theme, theme_default)
        if changed:
            write_json_atomic(theme_file, self.theme)

        # 4. options.json -- sync default model values into user's list
        default_models = DEFAULT_OPTIONS.get("model", {}).get("values", [])
        user_models = self.options_def.get("model", {}).get("values", [])
        new_models = [m for m in default_models if m not in user_models]
        if new_models:
            user_models.extend(new_models)
            write_json_atomic(OPTIONS_FILE, self.options_def)

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
            write_json_atomic(CONFIG_FILE, self.config)
        if segments_changed:
            write_json_atomic(SEGMENTS_FILE, self.segments_def)
        if theme_changed:
            write_json_atomic(theme_file, self.theme)
        if options_changed:
            write_json_atomic(OPTIONS_FILE, self.options_def)

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
        """Add a new option value to the pinned list in options.json for the given segment."""
        options = self._load_json(OPTIONS_FILE, self.options_def)
        if segment_key not in options:
            options[segment_key] = {"values": [], "pinned": []}
        pinned = options[segment_key].setdefault("pinned", [])
        if value not in pinned:
            pinned.append(value)
            write_json_atomic(OPTIONS_FILE, options)
            # Also update in-memory copy
            self.options_def = options

    def set_option_metadata(self, segment_key: str, value: str, meta: dict) -> None:
        """Set metadata for a specific option value in options.json."""
        options = self._load_json(OPTIONS_FILE, self.options_def)
        seg = options.setdefault(segment_key, {"values": []})
        seg.setdefault("metadata", {})[value] = meta
        write_json_atomic(OPTIONS_FILE, options)
        self.options_def = options

    def save_state(self):
        write_json_atomic(STATE_FILE, self.state)
