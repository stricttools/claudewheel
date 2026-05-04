"""Tests for ConfigManager migration logic: _deep_merge_missing, _migrate, _run_versioned_migrations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.config import ConfigManager
from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_OPTIONS,
    DEFAULT_SEGMENTS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
)


def _write_json(path: Path, data: dict | list) -> None:
    """Write JSON to *path*, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _read_json(path: Path) -> dict | list:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. DeepMergeTests
# ---------------------------------------------------------------------------


class DeepMergeTests(unittest.TestCase):
    """Test ConfigManager._deep_merge_missing() in isolation."""

    def test_adds_missing_keys_returns_true(self) -> None:
        """Missing top-level keys are added and the method returns True."""
        target: dict = {"a": 1}
        defaults = {"a": 1, "b": 2}
        result = ConfigManager._deep_merge_missing(target, defaults)
        self.assertTrue(result)
        self.assertEqual(target["b"], 2)

    def test_does_not_overwrite_existing_returns_false(self) -> None:
        """Existing keys are left untouched and False is returned when nothing is missing."""
        target = {"a": 99, "b": 42}
        defaults = {"a": 1, "b": 2}
        result = ConfigManager._deep_merge_missing(target, defaults)
        self.assertFalse(result)
        self.assertEqual(target["a"], 99)
        self.assertEqual(target["b"], 42)

    def test_recursive_merge_into_nested_dicts(self) -> None:
        """Missing keys inside nested dicts are added recursively."""
        target: dict = {"outer": {"existing": "keep"}}
        defaults = {"outer": {"existing": "default", "new_key": "added"}}
        result = ConfigManager._deep_merge_missing(target, defaults)
        self.assertTrue(result)
        self.assertEqual(target["outer"]["existing"], "keep")
        self.assertEqual(target["outer"]["new_key"], "added")

    def test_adds_entire_missing_sections(self) -> None:
        """A completely absent nested dict is deep-copied from defaults."""
        target: dict = {}
        defaults = {"section": {"key1": "val1", "key2": {"nested": True}}}
        result = ConfigManager._deep_merge_missing(target, defaults)
        self.assertTrue(result)
        self.assertEqual(target["section"]["key1"], "val1")
        self.assertTrue(target["section"]["key2"]["nested"])
        # Verify deep copy (mutating target should not affect defaults)
        target["section"]["key2"]["nested"] = False
        self.assertTrue(defaults["section"]["key2"]["nested"])

    def test_idempotent_second_run_returns_false(self) -> None:
        """Running merge twice with the same defaults returns False the second time."""
        target: dict = {"a": 1}
        defaults = {"a": 1, "b": 2, "c": {"d": 3}}
        ConfigManager._deep_merge_missing(target, defaults)
        result = ConfigManager._deep_merge_missing(target, defaults)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Helper: set up a temp dir that mimics ~/.claudelauncher
# ---------------------------------------------------------------------------


def _setup_temp_config_dir(
    tmp: Path,
    *,
    config: dict | None = None,
    segments: list[dict] | None = None,
    options: dict | None = None,
    state: dict | None = None,
    theme: dict | None = None,
) -> dict[str, Path]:
    """Create config files in *tmp* and return a dict of path constants.

    Any parameter left as None gets a sensible default that won't cause
    ConfigManager.__post_init__ to error.
    """
    launcher_dir = tmp / "claudelauncher"
    themes_dir = launcher_dir / "themes"
    hooks_dir = launcher_dir / "hooks"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    themes_dir.mkdir(exist_ok=True)
    hooks_dir.mkdir(exist_ok=True)

    config_file = launcher_dir / "config.json"
    segments_file = launcher_dir / "segments.json"
    options_file = launcher_dir / "options.json"
    state_file = launcher_dir / "state.json"
    theme_file = themes_dir / "dark.json"

    _write_json(config_file, config if config is not None else DEFAULT_CONFIG)
    _write_json(segments_file, segments if segments is not None else DEFAULT_SEGMENTS)
    _write_json(options_file, options if options is not None else DEFAULT_OPTIONS)
    _write_json(state_file, state if state is not None else DEFAULT_STATE)
    _write_json(theme_file, theme if theme is not None else DEFAULT_THEME_DARK)

    return {
        "LAUNCHER_DIR": launcher_dir,
        "CONFIG_FILE": config_file,
        "SEGMENTS_FILE": segments_file,
        "OPTIONS_FILE": options_file,
        "STATE_FILE": state_file,
        "THEMES_DIR": themes_dir,
        "HOOKS_DIR": hooks_dir,
    }


def _patch_constants(paths: dict[str, Path]):
    """Return a stack of unittest.mock.patch.object contexts for config module constants."""
    import claudewheel.config as cfg_mod

    return [
        patch.object(cfg_mod, name, value)
        for name, value in paths.items()
    ]


# ---------------------------------------------------------------------------
# 2. KeyMigrationTests
# ---------------------------------------------------------------------------


class KeyMigrationTests(unittest.TestCase):
    """Test _migrate() key-adding on partial config files."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def _make_cm(self, paths: dict[str, Path]) -> ConfigManager:
        """Create a ConfigManager with patched path constants."""
        patches = _patch_constants(paths)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ConfigManager()

    def test_missing_config_keys_added(self) -> None:
        """Keys absent from config.json are filled in from defaults."""
        # Write a config with only "theme" -- other keys should be added
        partial_config = {"theme": "dark"}
        paths = _setup_temp_config_dir(self.tmp, config=partial_config)
        cm = self._make_cm(paths)

        # All default keys should now be present
        for key in DEFAULT_CONFIG:
            self.assertIn(key, cm.config, f"missing config key: {key}")

    def test_existing_config_values_preserved(self) -> None:
        """User-customised values in config.json are NOT overwritten."""
        custom_config = {
            "theme": "dark",
            "minimap": "always",  # user changed from default "auto"
        }
        paths = _setup_temp_config_dir(self.tmp, config=custom_config)
        cm = self._make_cm(paths)

        self.assertEqual(cm.config["minimap"], "always")

    def test_missing_segment_attrs_added(self) -> None:
        """Attributes missing from a segment in segments.json are added from defaults."""
        # A segment with only key and label -- everything else should be merged
        partial_segments = [{"key": "profile", "label": "Profile"}]
        paths = _setup_temp_config_dir(self.tmp, segments=partial_segments)
        cm = self._make_cm(paths)

        profile_seg = next(s for s in cm.segments_def if s["key"] == "profile")
        # Check that default attrs were added
        default_profile = next(s for s in DEFAULT_SEGMENTS if s["key"] == "profile")
        for attr in default_profile:
            self.assertIn(attr, profile_seg, f"missing segment attr: {attr}")

    def test_removed_segments_not_re_added(self) -> None:
        """Segments intentionally removed by the user are NOT re-added by _migrate()."""
        # Only keep "profile", deliberately omit "github" and others
        partial_segments = [
            {"key": "profile", "label": "Profile", "required": True,
             "show_options": True, "wrap": True, "min_width": 8,
             "max_width": 16, "print_mode": True, "searchable": False,
             "tab_advances": True, "creatable": True},
        ]
        paths = _setup_temp_config_dir(self.tmp, segments=partial_segments)
        cm = self._make_cm(paths)

        keys = [s["key"] for s in cm.segments_def]
        self.assertIn("profile", keys)
        self.assertNotIn("github", keys)
        self.assertNotIn("version", keys)


# ---------------------------------------------------------------------------
# 3. VersionedMigrationTests
# ---------------------------------------------------------------------------


class VersionedMigrationTests(unittest.TestCase):
    """Test _run_versioned_migrations() for schema-versioned value changes."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def _make_cm(self, paths: dict[str, Path]) -> ConfigManager:
        patches = _patch_constants(paths)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ConfigManager()

    def test_schema_v0_github_required_migrated_to_false(self) -> None:
        """With _schema_version 0 and github required=true, migration sets it to false."""
        config = {**DEFAULT_CONFIG, "_schema_version": 0}
        segments = [
            {"key": "github", "label": "GH", "required": True,
             "show_options": True, "wrap": True, "min_width": 4,
             "max_width": 12, "print_mode": False, "searchable": False,
             "tab_advances": True, "creatable": True},
        ]
        paths = _setup_temp_config_dir(self.tmp, config=config, segments=segments)
        cm = self._make_cm(paths)

        github_seg = next(s for s in cm.segments_def if s["key"] == "github")
        self.assertFalse(github_seg["required"])
        # Schema version bumped
        self.assertGreaterEqual(cm.config["_schema_version"], 1)

    def test_schema_v1_github_required_stays_true(self) -> None:
        """With _schema_version 1, migration 1 is skipped; required stays true if user set it."""
        config = {**DEFAULT_CONFIG, "_schema_version": 1}
        segments = [
            {"key": "github", "label": "GH", "required": True,
             "show_options": True, "wrap": True, "min_width": 4,
             "max_width": 12, "print_mode": False, "searchable": False,
             "tab_advances": True, "creatable": True},
        ]
        paths = _setup_temp_config_dir(self.tmp, config=config, segments=segments)
        cm = self._make_cm(paths)

        github_seg = next(s for s in cm.segments_def if s["key"] == "github")
        # Migration was NOT applied, so required stays True
        self.assertTrue(github_seg["required"])

    def test_schema_version_persisted_to_disk(self) -> None:
        """After migration, config.json on disk has the updated _schema_version."""
        config = {**DEFAULT_CONFIG, "_schema_version": 0}
        segments = [
            {"key": "github", "label": "GH", "required": True,
             "show_options": True, "wrap": True, "min_width": 4,
             "max_width": 12, "print_mode": False, "searchable": False,
             "tab_advances": True, "creatable": True},
        ]
        paths = _setup_temp_config_dir(self.tmp, config=config, segments=segments)
        self._make_cm(paths)

        # Read config.json from disk directly
        on_disk = _read_json(paths["CONFIG_FILE"])
        self.assertGreaterEqual(on_disk["_schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
