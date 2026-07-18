"""Tests for AppConfigStore migration logic: _deep_merge_missing, _migrate, _run_versioned_migrations."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.config import AppConfigStore
from claudewheel.workspace import Workspace
from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_OPTIONS,
    DEFAULT_SEGMENTS,
    DEFAULT_THEME_DARK,
)
from tests.wheelhelpers import (
    SandboxHomeTestCase,
    setup_temp_config_dir as _setup_temp_config_dir,
    snapshot_tree as _snapshot,
    write_json,
    write_json as _write_json,
)


def _appconfig(paths: dict[str, Path]) -> AppConfigStore:
    """Construct an AppConfigStore over the sandbox launcher dir in *paths*.

    ``setup_temp_config_dir`` returns a mapping whose ``CONFIG_DIR`` is the
    ``~/.claudewheel``-shaped root; the workspace derives every other path from
    it, so no per-module constant patching is required.
    """
    return Workspace.open(paths["CONFIG_DIR"]).appconfig()


def _read_json(path: Path) -> dict | list:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1. DeepMergeTests
# ---------------------------------------------------------------------------


class DeepMergeTests(unittest.TestCase):
    """Test AppConfigStore._deep_merge_missing() in isolation."""

    def test_adds_missing_keys_returns_true(self) -> None:
        """Missing top-level keys are added and the method returns True."""
        target: dict = {"a": 1}
        defaults = {"a": 1, "b": 2}
        result = AppConfigStore._deep_merge_missing(target, defaults)
        self.assertTrue(result)
        self.assertEqual(target["b"], 2)

    def test_does_not_overwrite_existing_returns_false(self) -> None:
        """Existing keys are left untouched and False is returned when nothing is missing."""
        target = {"a": 99, "b": 42}
        defaults = {"a": 1, "b": 2}
        result = AppConfigStore._deep_merge_missing(target, defaults)
        self.assertFalse(result)
        self.assertEqual(target["a"], 99)
        self.assertEqual(target["b"], 42)

    def test_recursive_merge_into_nested_dicts(self) -> None:
        """Missing keys inside nested dicts are added recursively."""
        target: dict = {"outer": {"existing": "keep"}}
        defaults = {"outer": {"existing": "default", "new_key": "added"}}
        result = AppConfigStore._deep_merge_missing(target, defaults)
        self.assertTrue(result)
        self.assertEqual(target["outer"]["existing"], "keep")
        self.assertEqual(target["outer"]["new_key"], "added")

    def test_adds_entire_missing_sections(self) -> None:
        """A completely absent nested dict is deep-copied from defaults."""
        target: dict = {}
        defaults = {"section": {"key1": "val1", "key2": {"nested": True}}}
        result = AppConfigStore._deep_merge_missing(target, defaults)
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
        AppConfigStore._deep_merge_missing(target, defaults)
        result = AppConfigStore._deep_merge_missing(target, defaults)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Config-dir setup is provided by tests.wheelhelpers (imported above as
# _setup_temp_config_dir); the store is built by the module-level _appconfig
# helper over a Workspace rooted at the sandbox launcher dir.
# ---------------------------------------------------------------------------


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

    def _make_cm(self, paths: dict[str, Path]) -> AppConfigStore:
        """Create an AppConfigStore over the sandbox launcher dir."""
        return _appconfig(paths)

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

    def test_theme_without_forms_section_gains_it(self) -> None:
        """A theme file on disk lacking the "forms" section gains it via _migrate()."""
        theme_without_forms = {
            k: v for k, v in DEFAULT_THEME_DARK.items() if k != "forms"
        }
        self.assertNotIn("forms", theme_without_forms)
        paths = _setup_temp_config_dir(self.tmp, theme=theme_without_forms)
        cm = self._make_cm(paths)

        # load_theme now returns the full forms section (merged from defaults)
        loaded = cm.load_theme("dark")
        self.assertIn("forms", loaded)
        for key in ("title_fg", "focus_bg", "focus_fg", "field_fg",
                    "error_fg", "hint_fg", "cursor_fg"):
            self.assertIn(key, loaded["forms"], f"missing forms key: {key}")
        self.assertEqual(loaded["forms"], DEFAULT_THEME_DARK["forms"])

        # Persisted to disk too (migration wrote the merged dark.json)
        on_disk = _read_json(paths["THEMES_DIR"] / "dark.json")
        self.assertEqual(on_disk["forms"], DEFAULT_THEME_DARK["forms"])

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

    def _make_cm(self, paths: dict[str, Path]) -> AppConfigStore:
        return _appconfig(paths)

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

# NOTE: the former ``test_migration_v2_*`` tests asserted that migration 2
# rewrote the profile-metadata config_dir strings. Migration 4 now deletes the
# entire profile-metadata block, so migration 2's output is unobservable after
# a full forward migration. Those tests are superseded by Migration4Tests
# below, which pins the stronger contract: profile metadata is removed while
# every other segment's metadata and all values/pinned survive.


# ---------------------------------------------------------------------------
# 3b. Migration4Tests -- drop the legacy profile-metadata block
# ---------------------------------------------------------------------------


class Migration4Tests(unittest.TestCase):
    """Test migration 4: the profile ``metadata`` block is dropped, nothing else."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def _make_cm(self, paths: dict[str, Path]) -> AppConfigStore:
        return _appconfig(paths)

    def _old_shape_options(self) -> dict:
        """Options with legacy profile metadata AND surviving model metadata."""
        return {
            **DEFAULT_OPTIONS,
            "profile": {
                "values": ["work", "personal"],
                "pinned": ["work"],
                "discovery": {"type": "claude_config_scan", "base_dir": "~"},
                "metadata": {
                    "work": {"config_dir": "~/.claude-work"},
                    "personal": {"config_dir": "~/.claudewheel/profiles/personal"},
                },
            },
            "model": {
                "values": ["claude-opus-4-8", "my-custom"],
                "pinned": ["my-custom"],
                "metadata": {"my-custom": {"model_id": "custom-123"}},
            },
        }

    def test_profile_metadata_removed_others_intact(self) -> None:
        """Old-shape fixture: profile metadata gone; model metadata + values/pinned intact."""
        config = {**DEFAULT_CONFIG, "_schema_version": 3}
        options = self._old_shape_options()
        paths = _setup_temp_config_dir(self.tmp, config=config, options=options)
        cm = self._make_cm(paths)

        # Profile metadata is gone; profile values/pinned survive.
        self.assertNotIn("metadata", cm.options_def["profile"])
        self.assertEqual(cm.options_def["profile"]["values"], ["work", "personal"])
        self.assertEqual(cm.options_def["profile"]["pinned"], ["work"])

        # Model metadata (model_id) survives untouched.
        self.assertEqual(
            cm.options_def["model"]["metadata"]["my-custom"]["model_id"], "custom-123"
        )
        self.assertIn("my-custom", cm.options_def["model"]["values"])
        self.assertIn("my-custom", cm.options_def["model"]["pinned"])

        self.assertGreaterEqual(cm.config["_schema_version"], 4)

    def test_profile_metadata_removed_on_disk(self) -> None:
        """The metadata deletion is persisted to options.json on disk."""
        config = {**DEFAULT_CONFIG, "_schema_version": 3}
        options = self._old_shape_options()
        paths = _setup_temp_config_dir(self.tmp, config=config, options=options)
        self._make_cm(paths)

        on_disk = _read_json(paths["OPTIONS_FILE"])
        self.assertNotIn("metadata", on_disk["profile"])
        self.assertIn("metadata", on_disk["model"])  # model metadata survives

    def test_forward_from_each_historical_version(self) -> None:
        """Fixtures at _schema_version 0..3 all migrate forward to 4 with metadata dropped."""
        for start in (0, 1, 2, 3):
            with self.subTest(start=start):
                tmp_obj = tempfile.TemporaryDirectory()
                self.addCleanup(tmp_obj.cleanup)
                tmp = Path(tmp_obj.name)
                config = {**DEFAULT_CONFIG, "_schema_version": start}
                options = self._old_shape_options()
                paths = _setup_temp_config_dir(tmp, config=config, options=options)
                cm = _appconfig(paths)
                self.assertNotIn("metadata", cm.options_def["profile"])
                self.assertEqual(cm.config["_schema_version"], 4)
                # Model metadata survives regardless of start version.
                self.assertEqual(
                    cm.options_def["model"]["metadata"]["my-custom"]["model_id"],
                    "custom-123",
                )

    def test_reconstruction_after_migration_is_noop(self) -> None:
        """A second appconfig() over the migrated root writes nothing new (mtime snapshot)."""
        config = {**DEFAULT_CONFIG, "_schema_version": 3}
        options = self._old_shape_options()
        paths = _setup_temp_config_dir(self.tmp, config=config, options=options)
        ws = Workspace.open(paths["CONFIG_DIR"])
        ws.appconfig()  # first construction migrates + drops profile metadata

        before = _snapshot(paths["CONFIG_DIR"])
        ws.appconfig()  # second construction must be a pure no-op
        after = _snapshot(paths["CONFIG_DIR"])
        self.assertEqual(after, before, "reopen mutated files after migration 4")

    def test_no_profile_metadata_key_is_safe(self) -> None:
        """A profile segment without a metadata block migrates without error."""
        config = {**DEFAULT_CONFIG, "_schema_version": 3}
        options = {
            **DEFAULT_OPTIONS,
            "profile": {"values": ["work"], "pinned": []},
        }
        paths = _setup_temp_config_dir(self.tmp, config=config, options=options)
        cm = self._make_cm(paths)
        self.assertNotIn("metadata", cm.options_def["profile"])
        self.assertEqual(cm.options_def["profile"]["values"], ["work"])


class Migration4LaunchResolutionTests(SandboxHomeTestCase):
    """End-to-end: a pre-migration workspace still launch-resolves after migration 4."""

    def test_launch_resolves_after_metadata_dropped(self) -> None:
        """Profile identity comes from the dir; the deleted metadata is irrelevant.

        Seed a sandbox workspace whose options.json carries the legacy profile
        metadata block and a stale config_dir. Construct the app config (runs
        migration 4, which deletes the metadata), then resolve the profile's
        launch env -- it must point at the directory-derived config dir, not the
        deleted metadata's config_dir.
        """
        # A real profile dir on disk with credentials + a token entry.
        pdir = self.make_profile("work", credentials=True)
        write_json(self.sandbox_paths["TOKENS_FILE"], {"work": "tok-work"})

        # Legacy options.json: profile metadata present with a stale config_dir.
        write_json(
            self.sandbox_paths["OPTIONS_FILE"],
            {
                **DEFAULT_OPTIONS,
                "profile": {
                    "values": ["work"],
                    "pinned": ["work"],
                    "discovery": {"type": "claude_config_scan", "base_dir": "~"},
                    "metadata": {"work": {"config_dir": "~/.claude-STALE-WRONG"}},
                },
            },
        )
        # Pin an old schema version so the versioned migrations run.
        cfg = _read_json(self.sandbox_paths["CONFIG_FILE"])
        cfg["_schema_version"] = 1
        write_json(self.sandbox_paths["CONFIG_FILE"], cfg)

        ws = Workspace.open(self.launcher_dir, claude_dir=self.home / ".claude")
        store = ws.appconfig()

        # Migration 4 dropped the profile metadata block.
        self.assertNotIn("metadata", store.options_def["profile"])

        # Launch env resolves the profile from the directory, ignoring the
        # (now deleted) stale metadata config_dir.
        env = ws.profiles.env("work")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-work")


# ---------------------------------------------------------------------------
# 4. ModelSyncTests
# ---------------------------------------------------------------------------


class ModelSyncTests(unittest.TestCase):
    """Test _migrate() step 4: syncing default model values into options.json."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def _make_cm(self, paths: dict[str, Path]) -> AppConfigStore:
        return _appconfig(paths)

    def test_migrate_adds_new_model_to_existing_options(self) -> None:
        """New default models (e.g. fable) are appended to the user's model list."""
        old_models = [
            "claude-opus-4-7",
            "claude-opus-4-7[1m]",
            "claude-opus-4-6",
            "claude-opus-4-6[1m]",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6[1m]",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5-20241022",
        ]
        options = {**DEFAULT_OPTIONS, "model": {"values": old_models[:]}}
        paths = _setup_temp_config_dir(self.tmp, options=options)
        cm = self._make_cm(paths)

        user_models = cm.options_def["model"]["values"]
        self.assertIn("claude-fable-5", user_models)
        self.assertIn("claude-fable-5[1m]", user_models)
        # Verify on disk too
        on_disk = _read_json(paths["OPTIONS_FILE"])
        self.assertIn("claude-fable-5", on_disk["model"]["values"])
        self.assertIn("claude-fable-5[1m]", on_disk["model"]["values"])

    def test_migrate_does_not_duplicate_existing_models(self) -> None:
        """Models already in the user's list are not added again."""
        from claudewheel.defaults import DEFAULT_OPTIONS as DO
        options = {**DEFAULT_OPTIONS, "model": {"values": DO["model"]["values"][:]}}
        paths = _setup_temp_config_dir(self.tmp, options=options)
        cm = self._make_cm(paths)

        user_models = cm.options_def["model"]["values"]
        # Count occurrences -- each should appear exactly once
        for model in DO["model"]["values"]:
            count = user_models.count(model)
            self.assertEqual(count, 1, f"{model} appears {count} times, expected 1")

    def test_migrate_preserves_custom_models(self) -> None:
        """Custom models added by the user are preserved after migration."""
        custom_models = [
            "my-custom-model",
            "claude-opus-4-7",
            "claude-opus-4-7[1m]",
        ]
        options = {**DEFAULT_OPTIONS, "model": {"values": custom_models[:]}}
        paths = _setup_temp_config_dir(self.tmp, options=options)
        cm = self._make_cm(paths)

        user_models = cm.options_def["model"]["values"]
        # Custom model is preserved
        self.assertIn("my-custom-model", user_models)
        # Default models that were missing are added
        self.assertIn("claude-fable-5", user_models)
        self.assertIn("claude-fable-5[1m]", user_models)
        # Original models still there
        self.assertIn("claude-opus-4-7", user_models)
        self.assertIn("claude-opus-4-7[1m]", user_models)


# ---------------------------------------------------------------------------
# 5. Rename recovery at startup
# ---------------------------------------------------------------------------


class RenameRecoveryOnStartupTests(unittest.TestCase):
    """AppConfigStore.__post_init__ calls recover_incomplete_renames to auto-repair."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.cw_dir = self.home / ".claudewheel"
        self.cw_dir.mkdir()
        self.profiles_dir = self.cw_dir / "profiles"
        self.profiles_dir.mkdir()

    def test_startup_recovers_rename(self) -> None:
        """A .rename_pending breadcrumb triggers store repair during init."""
        # Simulate: dir renamed to "repaired", JSON still has "broken"
        new_dir = self.profiles_dir / "repaired"
        new_dir.mkdir()
        (new_dir / ".credentials.json").write_text("{}")
        (new_dir / ".rename_pending").write_text(
            json.dumps({"from": "broken", "to": "repaired"})
        )

        tokens_file = self.cw_dir / "tokens.json"
        tokens_file.write_text(json.dumps({"broken": "tok-b"}))
        options_file = self.cw_dir / "options.json"
        _write_json(options_file, {"profile": {"values": ["broken"]}})
        state_file = self.cw_dir / "state.json"
        _write_json(state_file, {"last_config": {"profile": "broken"}})

        # The workspace derives profiles/tokens/options/state paths from the
        # launcher root, so construction alone drives recovery -- no per-module
        # path patching, and no terminal detection is attempted at construction.
        # A spy proves construction never queries the terminal.
        spy = mock.Mock(side_effect=AssertionError("terminal I/O attempted"))
        with mock.patch("claudewheel.config.detect_terminal_background", spy):
            Workspace.open(self.cw_dir).appconfig()
        self.assertFalse(spy.called)

        # Breadcrumb gone
        self.assertFalse((new_dir / ".rename_pending").exists())
        # Stores updated
        tokens = json.loads(tokens_file.read_text())
        self.assertNotIn("broken", tokens)
        self.assertIn("repaired", tokens)
        opts = json.loads(options_file.read_text())
        self.assertIn("repaired", opts["profile"]["values"])
        self.assertNotIn("broken", opts["profile"]["values"])


# ---------------------------------------------------------------------------
# 6. Phase 5.1 construction contract: lazy, idempotent, fail-loud
# ---------------------------------------------------------------------------


class ConstructionContractTests(unittest.TestCase):
    """Phase 5.1: Workspace.appconfig() is lazy-on-open, idempotent, fail-loud."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def test_open_and_store_accessors_do_not_touch_disk(self) -> None:
        """Workspace.open + .tokens + .profiles create/modify nothing on disk."""
        root = self.tmp / "cw"  # deliberately does not exist yet
        ws = Workspace.open(root)
        _ = ws.tokens
        _ = ws.profiles
        self.assertFalse(root.exists(), "accessors must not create the root")

    def test_reopen_is_a_no_op(self) -> None:
        """Constructing appconfig() twice on a migrated root writes nothing new."""
        paths = _setup_temp_config_dir(self.tmp)
        ws = Workspace.open(paths["CONFIG_DIR"])
        ws.appconfig()  # first construction migrates + materializes everything

        before = _snapshot(paths["CONFIG_DIR"])
        ws.appconfig()  # second construction must be a pure no-op
        after = _snapshot(paths["CONFIG_DIR"])

        self.assertEqual(after, before, "reopen mutated files on disk")

    def test_construction_emits_nothing_on_stderr(self) -> None:
        """Constructing appconfig() on a sandbox root prints nothing to stderr.

        Regression guard for the removed legacy-dir warning: construction must
        be silent (no ``~/.claude-<name>`` scan of the real home, no stray
        stderr output of any kind).
        """
        paths = _setup_temp_config_dir(self.tmp)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            Workspace.open(paths["CONFIG_DIR"]).appconfig()
        self.assertEqual(captured.getvalue(), "")

    def test_readonly_root_raises(self) -> None:
        """appconfig() on a read-only (0o555) root fails loudly (no silent skip)."""
        root = self.tmp / "cw"
        root.mkdir()
        # Empty root: construction must create themes/ + write default files,
        # which a read-only root forbids -> hard error.
        os.chmod(root, 0o555)
        try:
            with self.assertRaises((PermissionError, OSError)):
                Workspace.open(root).appconfig()
        finally:
            os.chmod(root, 0o755)


if __name__ == "__main__":
    unittest.main()
