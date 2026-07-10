"""The app-config store: the TUI's config/segments/options/state hub.

This module owns :class:`AppConfigStore`, the workspace-backed store that
loads and migrates the four JSON config files (config, segments, options,
state) plus the theme files. Construction is eager (it ensures directories,
runs schema migrations, recovers interrupted renames, and materializes
shared-settings.json) but performs **zero terminal I/O** -- theme "auto"
resolution lives in the module-level :func:`resolve_theme_name`, called at the
UI boundaries, never during construction.
"""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .defaults import (
    DEFAULT_CONFIG,
    DEFAULT_SEGMENTS,
    DEFAULT_OPTIONS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
    build_canonical_shared_settings,
)
from .appdata import OptionsFile, StateFile
from .fsutil import write_json_atomic
from .terminal import detect_terminal_background

if TYPE_CHECKING:
    from .workspace import Workspace


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
        "claude-opus-4-8",
        "claude-opus-4-8[1m]",
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
    """Rewrite profile metadata config_dir from ~/.claude-<name> to ~/.claudewheel/profiles/<name>.

    Knowingly vestigial post-strip: profile locations are no longer persisted
    (derived from the profile directory instead), and migration 4 deletes the
    entire profile metadata block this migration rewrites. It is kept solely so
    the versioned-migration replay order stays stable for configs that migrate
    forward from an old ``_schema_version`` -- migration 2 still runs, then
    migration 4 removes its output in the same forward pass.
    """
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


def _migration_4_drop_profile_metadata(
    config: dict, segments_def: list[dict], theme: dict, options_def: dict,
) -> None:
    """Remove the legacy ``metadata`` block from the ``profile`` segment only.

    Profile locations are no longer stored -- they are always derived from the
    profile directory via ``ProfileStore.path_for``. This deletes only the
    ``profile`` segment's ``metadata`` dict; every other segment's metadata
    (e.g. the model segment's ``model_id`` entries) and every segment's
    ``values``/``pinned`` lists are left untouched.
    """
    profile_seg = options_def.get("profile")
    if isinstance(profile_seg, dict):
        profile_seg.pop("metadata", None)


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
    {
        "version": 4,
        "description": "Drop the legacy profile-metadata block (locations derived from dir)",
        "apply": _migration_4_drop_profile_metadata,
    },
]


def resolve_theme_name(theme_name: str) -> str:
    """Resolve 'auto' theme to 'light' or 'dark' via terminal detection.

    Explicit 'light' or 'dark' (or any other custom name) are returned as-is.
    'auto' queries the terminal background color; detection failure falls back
    to 'dark'. This performs terminal I/O and therefore lives OUTSIDE store
    construction -- callers invoke it at the UI boundary, never during
    :class:`AppConfigStore` init.
    """
    if theme_name != "auto":
        return theme_name
    detected = detect_terminal_background()
    return detected if detected in ("light", "dark") else "dark"


@dataclass
class AppConfigStore:
    """Workspace-backed store for the four JSON config files plus themes.

    Construct it via :meth:`claudewheel.workspace.Workspace.appconfig`; all
    paths are derived from the workspace. Construction is eager (ensure dirs,
    load, migrate, recover renames, materialize shared-settings) but does ZERO
    terminal I/O -- theme "auto" resolution is deferred to
    :func:`resolve_theme_name` at the UI boundary.
    """

    workspace: "Workspace"
    config: dict = field(default_factory=dict)
    segments_def: list[dict] = field(default_factory=list)
    options_def: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    # Written by the runtime theme-switch handler; never populated at
    # construction time (the store performs no theme resolution).
    theme: dict = field(default_factory=dict)

    def __post_init__(self):
        ws = self.workspace
        self._root = ws.root
        self._config_file = ws.config_file
        self._segments_file = ws.segments_file
        self._options_file = ws.options_file
        self._state_file = ws.state_file
        self._themes_dir = ws.themes_dir
        self._hooks_dir = ws.hooks_dir
        self._scripts_dir = ws.scripts_dir
        self._shared_settings_file = ws.shared_settings_file

        self._ensure_dir()
        self.config = self._load_json(self._config_file, DEFAULT_CONFIG)
        self.segments_def = self._load_json(self._segments_file, DEFAULT_SEGMENTS)
        self.options_def = self._load_json(self._options_file, DEFAULT_OPTIONS)
        self.state = self._load_json(self._state_file, DEFAULT_STATE)
        self._migrate()
        self._run_versioned_migrations()
        self._recover_incomplete_renames()
        self._ensure_shared_settings()
        self._warn_old_profile_dirs()

    # --- Theme access (no terminal I/O) ----------------------------------

    def load_theme(self, name: str) -> dict:
        """Read ``themes/<name>.json`` and return a complete theme dict.

        Uses the same default-fallback + deep-merge-missing semantics the theme
        files get during migration, so a partial or missing file still yields a
        fully populated theme. Pure read -- performs no writes and no terminal
        I/O. Callers resolve *name* via :func:`resolve_theme_name` first.
        """
        theme_default = DEFAULT_THEME_LIGHT if name == "light" else DEFAULT_THEME_DARK
        theme = self._load_json(self._themes_dir / f"{name}.json", theme_default)
        self._deep_merge_missing(theme, theme_default)
        return theme

    def _theme_specs(self) -> list[tuple[Path, dict]]:
        """The (path, default) pairs for the built-in theme files.

        Migrations run against BOTH files uniformly (not just a
        terminal-resolved one), so schema fixes are deterministic and
        mount-agnostic regardless of which theme the user ends up rendering.
        """
        return [
            (self._themes_dir / "dark.json", DEFAULT_THEME_DARK),
            (self._themes_dir / "light.json", DEFAULT_THEME_LIGHT),
        ]

    def _recover_incomplete_renames(self) -> None:
        """Finish any interrupted profile renames (crash recovery)."""
        self.workspace.profiles.recover_incomplete_renames()

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
        if not self._shared_settings_file.exists():
            canonical = build_canonical_shared_settings(self._scripts_dir)
            write_json_atomic(self._shared_settings_file, canonical)

    def _ensure_dir(self):
        """Create config directories and write default files on first run."""
        self._root.mkdir(exist_ok=True)
        self._themes_dir.mkdir(exist_ok=True)
        self._hooks_dir.mkdir(exist_ok=True)
        for path, default in [
            (self._config_file, DEFAULT_CONFIG),
            (self._segments_file, DEFAULT_SEGMENTS),
            (self._options_file, DEFAULT_OPTIONS),
            (self._state_file, DEFAULT_STATE),
            (self._themes_dir / "dark.json", DEFAULT_THEME_DARK),
            (self._themes_dir / "light.json", DEFAULT_THEME_LIGHT),
        ]:
            if not path.exists():
                write_json_atomic(path, default)

    def _load_json(self, path: Path, default: dict | list) -> dict | list:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _migrate(self) -> None:
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
            write_json_atomic(self._config_file, self.config)

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
            write_json_atomic(self._segments_file, self.segments_def)

        # 3. theme files — merge missing keys into BOTH dark.json and light.json
        for theme_file, theme_default in self._theme_specs():
            theme = self._load_json(theme_file, theme_default)
            if self._deep_merge_missing(theme, theme_default):
                write_json_atomic(theme_file, theme)

        # 4. options.json -- sync default model values into user's list
        default_models = DEFAULT_OPTIONS.get("model", {}).get("values", [])
        user_models = self.options_def.get("model", {}).get("values", [])
        new_models = [m for m in default_models if m not in user_models]
        if new_models:
            user_models.extend(new_models)
            OptionsFile(self._options_file).write(self.options_def)

    def _run_versioned_migrations(self) -> None:
        """Run schema-versioned migrations that change existing values.

        Complements _migrate() which only adds missing keys. Versioned
        migrations can mutate values and run exactly once per version bump.
        Theme migrations run against BOTH theme files uniformly: the primary
        pass mutates config/segments/options plus the first theme file, and
        secondary passes apply only theme changes to the remaining files (using
        throwaway copies of config/segments/options so they are not mutated
        twice).
        """
        current_version = self.config.get("_schema_version", 0)
        highest_applied = current_version
        config_changed = False
        segments_changed = False
        options_changed = False

        themes = [
            (theme_file, theme_default, self._load_json(theme_file, theme_default))
            for theme_file, theme_default in self._theme_specs()
        ]
        theme_before = [json.dumps(t[2], sort_keys=True) for t in themes]

        for migration in _MIGRATIONS:
            if migration["version"] > current_version:
                # Snapshot segments/options to detect mutations
                seg_before = json.dumps(self.segments_def, sort_keys=True)
                options_before = json.dumps(self.options_def, sort_keys=True)

                # Primary pass: config/segments/options + the first theme file.
                migration["apply"](
                    self.config, self.segments_def, themes[0][2], self.options_def
                )
                # Secondary passes: remaining theme files only. Copies keep
                # config/segments/options from being mutated more than once.
                for _tf, _td, tdict in themes[1:]:
                    migration["apply"](
                        copy.deepcopy(self.config),
                        copy.deepcopy(self.segments_def),
                        tdict,
                        copy.deepcopy(self.options_def),
                    )

                if json.dumps(self.segments_def, sort_keys=True) != seg_before:
                    segments_changed = True
                if json.dumps(self.options_def, sort_keys=True) != options_before:
                    options_changed = True

                highest_applied = max(highest_applied, migration["version"])

        if highest_applied > current_version:
            self.config["_schema_version"] = highest_applied
            config_changed = True

        if config_changed:
            write_json_atomic(self._config_file, self.config)
        if segments_changed:
            write_json_atomic(self._segments_file, self.segments_def)
        if options_changed:
            OptionsFile(self._options_file).write(self.options_def)
        for (theme_file, _td, tdict), before in zip(themes, theme_before):
            if json.dumps(tdict, sort_keys=True) != before:
                write_json_atomic(theme_file, tdict)

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
                if AppConfigStore._deep_merge_missing(target[key], default_value):
                    changed = True
        return changed

    def add_option(self, segment_key: str, value: str) -> None:
        """Add a new option value to the pinned list in options.json for the given segment."""
        self.options_def = OptionsFile(self._options_file).add_pinned(
            segment_key, value, self.options_def
        )

    def save_state(self):
        """Save in-memory state to disk.

        Merges out-of-band keys (auth_browser) from disk before writing to
        prevent clobber by stale in-memory state. The auth wizard writes
        auth_browser directly to disk while the TUI holds its own in-memory
        state loaded at startup; this merge ensures that value survives.
        """
        StateFile(self._state_file).save(self.state)
