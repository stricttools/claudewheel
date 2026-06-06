"""Tests for create_profile() in claudewheel.wizard."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.wizard import WizardResult, create_profile, _HOOKS_TEMPLATE
from claudewheel.constants import PROFILE_SHARED_DIRS
from claudewheel import wizard as wizard_mod
from claudewheel import config as config_mod
from claudewheel.config import ConfigManager
from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_SEGMENTS,
    DEFAULT_OPTIONS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
    DISALLOWED_TOOLS,
)


def _make_result(
    name: str = "test",
    clone_from: str | None = None,
    wire_hooks: bool = False,
    symlink_shared: bool = False,
    disable_recap: bool = False,
    cleanup_10y: bool = False,
    disable_memory: bool = False,
    disable_attribution: bool = False,
) -> WizardResult:
    """Build a WizardResult with sensible defaults for testing."""
    return WizardResult(
        name=name,
        config_dir=f"~/.claudewheel/profiles/{name}",
        clone_from=clone_from,
        wire_hooks=wire_hooks,
        symlink_shared=symlink_shared,
        disable_recap=disable_recap,
        cleanup_10y=cleanup_10y,
        disable_memory=disable_memory,
        disable_attribution=disable_attribution,
    )


def _init_launcher_dir(launcher_dir: Path) -> None:
    """Populate a temp launcher dir with the minimal files ConfigManager needs."""
    launcher_dir.mkdir(parents=True, exist_ok=True)
    themes_dir = launcher_dir / "themes"
    themes_dir.mkdir(exist_ok=True)
    (launcher_dir / "hooks").mkdir(exist_ok=True)
    for path, data in [
        (launcher_dir / "config.json", DEFAULT_CONFIG),
        (launcher_dir / "segments.json", DEFAULT_SEGMENTS),
        (launcher_dir / "options.json", DEFAULT_OPTIONS),
        (launcher_dir / "state.json", DEFAULT_STATE),
        (themes_dir / "dark.json", DEFAULT_THEME_DARK),
        (themes_dir / "light.json", DEFAULT_THEME_LIGHT),
    ]:
        path.write_text(json.dumps(data, indent=2) + "\n")


class CreateProfileTestBase(unittest.TestCase):
    """Base class that sets up an isolated home dir and ConfigManager."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake_home = Path(self._tmp.name)

        # Suppress create_profile() print output
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()
        self.addCleanup(self._stdout_trap.__exit__, None, None, None)

        # Patch Path.home() and HOME env var so expanduser("~") resolves here
        self._home_patch = mock.patch.object(Path, "home", return_value=self.fake_home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.fake_home)
        self.addCleanup(self._restore_home)

        # Patch the module-level path constants used by ConfigManager
        self.launcher_dir = self.fake_home / ".claudewheel"
        _init_launcher_dir(self.launcher_dir)

        self._patches = [
            mock.patch.object(config_mod, "CONFIG_DIR", self.launcher_dir),
            mock.patch.object(config_mod, "CONFIG_FILE", self.launcher_dir / "config.json"),
            mock.patch.object(config_mod, "SEGMENTS_FILE", self.launcher_dir / "segments.json"),
            mock.patch.object(config_mod, "OPTIONS_FILE", self.launcher_dir / "options.json"),
            mock.patch.object(config_mod, "STATE_FILE", self.launcher_dir / "state.json"),
            mock.patch.object(config_mod, "THEMES_DIR", self.launcher_dir / "themes"),
            mock.patch.object(config_mod, "HOOKS_DIR", self.launcher_dir / "hooks"),
            mock.patch.object(config_mod, "SHARED_DIR", self.fake_home / ".claude-shared"),
            mock.patch.object(config_mod, "COMMON_DIR", self.fake_home / ".claude-common"),
            # wizard.py imports CONFIG_DIR, PROFILES_DIR, SHARED_DIR, COMMON_DIR directly
            mock.patch.object(wizard_mod, "CONFIG_DIR", self.launcher_dir),
            mock.patch.object(wizard_mod, "PROFILES_DIR", self.launcher_dir / "profiles"),
            mock.patch.object(wizard_mod, "SHARED_DIR", self.fake_home / ".claude-shared"),
            mock.patch.object(wizard_mod, "COMMON_DIR", self.fake_home / ".claude-common"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

        self.cfg = ConfigManager()

    def _restore_home(self) -> None:
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home

    def _profile_dir(self, name: str = "test") -> Path:
        return self.fake_home / ".claudewheel" / "profiles" / name

    def _read_settings(self, name: str = "test") -> dict:
        return json.loads((self._profile_dir(name) / "settings.json").read_text())


class DirectoryCreationTests(CreateProfileTestBase):
    """Test 1: create_profile creates ~/.claudewheel/profiles/<name>/."""

    def test_creates_config_dir(self) -> None:
        result = _make_result(name="myprofile")
        self.assertFalse(self._profile_dir("myprofile").exists())
        create_profile(result, self.cfg)
        self.assertTrue(self._profile_dir("myprofile").is_dir())

    def test_creates_nested_parents(self) -> None:
        """The dir is created with parents=True so intermediate dirs are fine."""
        result = _make_result(name="deep")
        create_profile(result, self.cfg)
        self.assertTrue(self._profile_dir("deep").is_dir())


class SettingsFromDefaultsTests(CreateProfileTestBase):
    """Test 2: reads profile-defaults.json when clone_from is None."""

    def test_reads_defaults_template(self) -> None:
        defaults = {"someKey": "someValue", "nested": {"a": 1}}
        defaults_file = self.launcher_dir / "profile-defaults.json"
        defaults_file.write_text(json.dumps(defaults))

        result = _make_result(clone_from=None)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        self.assertEqual(settings["someKey"], "someValue")
        self.assertEqual(settings["nested"], {"a": 1})

    def test_no_defaults_file_produces_hardcoded_defaults(self) -> None:
        """When profile-defaults.json is missing, settings contain only the
        hardcoded disableAutoMode and claudewheel.disallowedTools entries."""
        result = _make_result(clone_from=None)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        expected = {
            "permissions": {"disableAutoMode": "disable"},
            "claudewheel": {"disallowedTools": DISALLOWED_TOOLS[:]},
        }
        self.assertEqual(settings, expected)

    def test_malformed_defaults_file_ignored(self) -> None:
        """A corrupt profile-defaults.json is silently skipped; hardcoded
        defaults are still applied."""
        defaults_file = self.launcher_dir / "profile-defaults.json"
        defaults_file.write_text("NOT VALID JSON{{{")

        result = _make_result(clone_from=None)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        expected = {
            "permissions": {"disableAutoMode": "disable"},
            "claudewheel": {"disallowedTools": DISALLOWED_TOOLS[:]},
        }
        self.assertEqual(settings, expected)


class SettingsFromCloneTests(CreateProfileTestBase):
    """Test 3: copies source profile's settings.json when clone_from is set."""

    def test_clones_existing_profile_settings(self) -> None:
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "source"
        source_dir.mkdir(parents=True)
        source_settings = {"clonedKey": True, "model": "opus"}
        (source_dir / "settings.json").write_text(json.dumps(source_settings))

        result = _make_result(name="cloned", clone_from="source")
        create_profile(result, self.cfg)

        settings = self._read_settings("cloned")
        self.assertEqual(settings["clonedKey"], True)
        self.assertEqual(settings["model"], "opus")

    def test_missing_source_settings_gets_hardcoded_defaults(self) -> None:
        """If the source profile dir exists but has no settings.json, settings
        contain only the hardcoded disableAutoMode and claudewheel.disallowedTools."""
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "source"
        source_dir.mkdir(parents=True)
        # No settings.json written

        result = _make_result(name="cloned2", clone_from="source")
        create_profile(result, self.cfg)

        settings = self._read_settings("cloned2")
        expected = {
            "permissions": {"disableAutoMode": "disable"},
            "claudewheel": {"disallowedTools": DISALLOWED_TOOLS[:]},
        }
        self.assertEqual(settings, expected)


class CheckboxOverridesTests(CreateProfileTestBase):
    """Test 4: checkbox overrides written correctly."""

    def test_disable_recap_sets_away_summary(self) -> None:
        result = _make_result(disable_recap=True)
        create_profile(result, self.cfg)
        self.assertFalse(self._read_settings()["awaySummaryEnabled"])

    def test_cleanup_10y_sets_period(self) -> None:
        result = _make_result(cleanup_10y=True)
        create_profile(result, self.cfg)
        self.assertEqual(self._read_settings()["cleanupPeriodDays"], 3650)

    def test_disable_memory_sets_auto_memory(self) -> None:
        result = _make_result(disable_memory=True)
        create_profile(result, self.cfg)
        self.assertFalse(self._read_settings()["autoMemoryEnabled"])

    def test_all_overrides_combined(self) -> None:
        result = _make_result(
            disable_recap=True, cleanup_10y=True, disable_memory=True
        )
        create_profile(result, self.cfg)
        settings = self._read_settings()
        self.assertFalse(settings["awaySummaryEnabled"])
        self.assertEqual(settings["cleanupPeriodDays"], 3650)
        self.assertFalse(settings["autoMemoryEnabled"])

    def test_no_overrides_leaves_settings_clean(self) -> None:
        """When all checkboxes are off, none of the override keys appear."""
        result = _make_result(
            disable_recap=False, cleanup_10y=False, disable_memory=False
        )
        create_profile(result, self.cfg)
        settings = self._read_settings()
        self.assertNotIn("awaySummaryEnabled", settings)
        self.assertNotIn("cleanupPeriodDays", settings)
        self.assertNotIn("autoMemoryEnabled", settings)

    def test_overrides_applied_on_top_of_defaults(self) -> None:
        """Overrides merge with values from profile-defaults.json."""
        defaults = {"awaySummaryEnabled": True, "otherKey": 42}
        defaults_file = self.launcher_dir / "profile-defaults.json"
        defaults_file.write_text(json.dumps(defaults))

        result = _make_result(disable_recap=True)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        # Override applied
        self.assertFalse(settings["awaySummaryEnabled"])
        # Original key preserved
        self.assertEqual(settings["otherKey"], 42)


class HooksWiringTests(CreateProfileTestBase):
    """Test 5: hooks template written to settings when wire_hooks=True."""

    def test_hooks_written_when_enabled(self) -> None:
        result = _make_result(wire_hooks=True)
        create_profile(result, self.cfg)
        settings = self._read_settings()
        self.assertIn("hooks", settings)
        self.assertEqual(settings["hooks"], _HOOKS_TEMPLATE)

    def test_no_hooks_when_disabled(self) -> None:
        result = _make_result(wire_hooks=False)
        create_profile(result, self.cfg)
        settings = self._read_settings()
        self.assertNotIn("hooks", settings)


class HookMergeTests(CreateProfileTestBase):
    """Test 6: when cloning from a profile that already has hooks,
    wanted hooks are merged without duplicates."""

    def test_merge_adds_missing_hooks(self) -> None:
        """Hooks from the template that are not in the source are appended."""
        existing_hooks = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "existing-hook-cmd"},
                        ],
                    }
                ]
            }
        }
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "src"
        source_dir.mkdir(parents=True)
        (source_dir / "settings.json").write_text(json.dumps(existing_hooks))

        result = _make_result(name="merged", clone_from="src", wire_hooks=True)
        create_profile(result, self.cfg)

        settings = self._read_settings("merged")
        hooks_list = settings["hooks"]["UserPromptSubmit"][0]["hooks"]
        cmds = [h["command"] for h in hooks_list]
        # Existing hook preserved
        self.assertIn("existing-hook-cmd", cmds)
        # Template hooks added
        for wanted in _HOOKS_TEMPLATE["UserPromptSubmit"][0]["hooks"]:
            self.assertIn(wanted["command"], cmds)

    def test_merge_does_not_duplicate_existing(self) -> None:
        """If the source already has one of the template hooks, it's not added again."""
        template_cmd = _HOOKS_TEMPLATE["UserPromptSubmit"][0]["hooks"][0]["command"]
        existing_hooks = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": template_cmd},
                        ],
                    }
                ]
            }
        }
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "dup"
        source_dir.mkdir(parents=True)
        (source_dir / "settings.json").write_text(json.dumps(existing_hooks))

        result = _make_result(name="nodedup", clone_from="dup", wire_hooks=True)
        create_profile(result, self.cfg)

        settings = self._read_settings("nodedup")
        hooks_list = settings["hooks"]["UserPromptSubmit"][0]["hooks"]
        cmds = [h["command"] for h in hooks_list]
        # The duplicated command should appear exactly once
        self.assertEqual(cmds.count(template_cmd), 1)

    def test_clone_without_hooks_gets_fresh_template(self) -> None:
        """Cloning a profile with no hooks section gets the full template."""
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "nohooks"
        source_dir.mkdir(parents=True)
        (source_dir / "settings.json").write_text(json.dumps({"someKey": 1}))

        result = _make_result(name="fresh", clone_from="nohooks", wire_hooks=True)
        create_profile(result, self.cfg)

        settings = self._read_settings("fresh")
        self.assertEqual(settings["hooks"], _HOOKS_TEMPLATE)
        # Cloned key is preserved
        self.assertEqual(settings["someKey"], 1)


class SymlinkCreationTests(CreateProfileTestBase):
    """Test 7: all 6 dirs symlinked to ~/.claude-shared/ when symlink_shared=True."""

    def test_symlinks_created(self) -> None:
        result = _make_result(symlink_shared=True)
        create_profile(result, self.cfg)

        profile_dir = self._profile_dir()
        shared_base = self.fake_home / ".claude-shared"
        for dirname in PROFILE_SHARED_DIRS:
            link = profile_dir / dirname
            self.assertTrue(link.is_symlink(), f"{dirname} should be a symlink")
            target = shared_base / dirname
            self.assertEqual(link.resolve(), target.resolve())

    def test_shared_target_dirs_created(self) -> None:
        """The target directories under ~/.claude-shared/ are created."""
        result = _make_result(symlink_shared=True)
        create_profile(result, self.cfg)

        shared_base = self.fake_home / ".claude-shared"
        for dirname in PROFILE_SHARED_DIRS:
            self.assertTrue((shared_base / dirname).is_dir())

    def test_all_six_dirs_present(self) -> None:
        """Exactly 6 shared dirs are defined."""
        self.assertEqual(len(PROFILE_SHARED_DIRS), 6)
        expected = {"projects", "session-env", "file-history", "tasks", "todos", "paste-cache"}
        self.assertEqual(set(PROFILE_SHARED_DIRS), expected)

    def test_existing_symlink_not_overwritten(self) -> None:
        """If a symlink already exists at the target, it is not replaced."""
        profile_dir = self._profile_dir()
        profile_dir.mkdir(parents=True)
        # Pre-create a symlink pointing elsewhere
        other_target = self.fake_home / "other-target"
        other_target.mkdir()
        existing_link = profile_dir / PROFILE_SHARED_DIRS[0]
        existing_link.symlink_to(other_target)

        result = _make_result(symlink_shared=True)
        create_profile(result, self.cfg)

        # The pre-existing symlink should still point to other_target
        self.assertEqual(existing_link.resolve(), other_target.resolve())


class NoSymlinksTests(CreateProfileTestBase):
    """Test 8: dirs NOT created when symlink_shared=False."""

    def test_no_symlinks_created(self) -> None:
        result = _make_result(symlink_shared=False)
        create_profile(result, self.cfg)

        profile_dir = self._profile_dir()
        for dirname in PROFILE_SHARED_DIRS:
            link = profile_dir / dirname
            self.assertFalse(link.exists(), f"{dirname} should not exist")
            self.assertFalse(link.is_symlink(), f"{dirname} should not be a symlink")

    def test_shared_base_not_created(self) -> None:
        """~/.claude-shared/ itself should not be created."""
        result = _make_result(symlink_shared=False)
        create_profile(result, self.cfg)
        shared_base = self.fake_home / ".claude-shared"
        self.assertFalse(shared_base.exists())


class OptionsRegistrationTests(CreateProfileTestBase):
    """Test 9: add_option and set_option_metadata called correctly."""

    def test_add_option_called(self) -> None:
        """The profile name is added to the 'profile' segment options."""
        result = _make_result(name="newprof")
        create_profile(result, self.cfg)

        # Reload options from disk to verify persistence
        options = json.loads((self.launcher_dir / "options.json").read_text())
        self.assertIn("newprof", options["profile"]["values"])

    def test_metadata_set(self) -> None:
        """Metadata with config_dir is set for the new profile."""
        result = _make_result(name="newprof")
        create_profile(result, self.cfg)

        options = json.loads((self.launcher_dir / "options.json").read_text())
        meta = options["profile"]["metadata"]["newprof"]
        self.assertEqual(meta, {"config_dir": "~/.claudewheel/profiles/newprof"})

    def test_add_option_with_mock(self) -> None:
        """Verify add_option and set_option_metadata are called with correct args."""
        result = _make_result(name="mocked")
        with mock.patch.object(self.cfg, "add_option") as mock_add, \
             mock.patch.object(self.cfg, "set_option_metadata") as mock_meta:
            create_profile(result, self.cfg)
            mock_add.assert_called_once_with("profile", "mocked")
            mock_meta.assert_called_once_with(
                "profile", "mocked", {"config_dir": "~/.claudewheel/profiles/mocked"}
            )


if __name__ == "__main__":
    unittest.main()
