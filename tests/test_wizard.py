"""Tests for create_profile() and run_profile_wizard() in claudewheel.wizard."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.wizard import WizardResult, create_profile, run_profile_wizard
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
    build_canonical_shared_settings,
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

        self._scripts_dir = self.launcher_dir / "scripts"
        self._shared_settings_file = self.launcher_dir / "shared-settings.json"
        self._patches = [
            mock.patch.object(config_mod, "CONFIG_DIR", self.launcher_dir),
            mock.patch.object(config_mod, "CONFIG_FILE", self.launcher_dir / "config.json"),
            mock.patch.object(config_mod, "SEGMENTS_FILE", self.launcher_dir / "segments.json"),
            mock.patch.object(config_mod, "OPTIONS_FILE", self.launcher_dir / "options.json"),
            mock.patch.object(config_mod, "STATE_FILE", self.launcher_dir / "state.json"),
            mock.patch.object(config_mod, "THEMES_DIR", self.launcher_dir / "themes"),
            mock.patch.object(config_mod, "HOOKS_DIR", self.launcher_dir / "hooks"),
            mock.patch.object(config_mod, "SCRIPTS_DIR", self._scripts_dir),
            mock.patch.object(config_mod, "SHARED_DIR", self.fake_home / ".claudewheel" / "shared"),
            mock.patch.object(config_mod, "SHARED_SETTINGS_FILE", self._shared_settings_file),
            # wizard.py imports PROFILES_DIR, SHARED_DIR, SKILLS_DIR directly
            mock.patch.object(wizard_mod, "PROFILES_DIR", self.launcher_dir / "profiles"),
            mock.patch.object(wizard_mod, "SCRIPTS_DIR", self._scripts_dir),
            mock.patch.object(wizard_mod, "SHARED_SETTINGS_FILE", self._shared_settings_file),
            mock.patch.object(wizard_mod, "SHARED_DIR", self.fake_home / ".claudewheel" / "shared"),
            mock.patch.object(wizard_mod, "SKILLS_DIR", self.fake_home / ".claudewheel" / "skills"),
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

    def _expected_hooks(self) -> dict:
        """Return the canonical hooks template using the test scripts dir."""
        return build_canonical_shared_settings(self._scripts_dir)["hooks"]


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
    """Test 2: reads profileDefaults from shared-settings.json when clone_from is None."""

    def test_reads_defaults_from_shared_settings(self) -> None:
        shared = build_canonical_shared_settings(self._scripts_dir)
        shared["profileDefaults"] = {"someKey": "someValue", "nested": {"a": 1}}
        self._shared_settings_file.write_text(json.dumps(shared))

        result = _make_result(clone_from=None)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        self.assertEqual(settings["someKey"], "someValue")
        self.assertEqual(settings["nested"], {"a": 1})

    def test_no_shared_settings_file_produces_hardcoded_defaults(self) -> None:
        """When shared-settings.json is missing, profileDefaults from
        build_canonical_shared_settings are used."""
        result = _make_result(clone_from=None)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        # profileDefaults includes permissions with deny/ask rules
        canonical = build_canonical_shared_settings(self._scripts_dir)
        profile_defaults = canonical["profileDefaults"]
        # permissions from profileDefaults get disableAutoMode merged in
        expected_permissions = dict(profile_defaults["permissions"])
        expected_permissions["disableAutoMode"] = "disable"
        self.assertEqual(settings["permissions"], expected_permissions)
        self.assertEqual(settings["claudewheel"], {"disallowedTools": DISALLOWED_TOOLS[:]})
        self.assertFalse(settings["awaySummaryEnabled"])
        self.assertEqual(settings["cleanupPeriodDays"], 3650)

    def test_malformed_shared_settings_uses_canonical_defaults(self) -> None:
        """A corrupt shared-settings.json falls back to canonical defaults."""
        self._shared_settings_file.write_text("NOT VALID JSON{{{")

        result = _make_result(clone_from=None)
        create_profile(result, self.cfg)

        settings = self._read_settings()
        # Should have profileDefaults from canonical
        canonical = build_canonical_shared_settings(self._scripts_dir)
        profile_defaults = canonical["profileDefaults"]
        self.assertFalse(settings["awaySummaryEnabled"])
        self.assertEqual(settings["cleanupPeriodDays"], profile_defaults["cleanupPeriodDays"])


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

    def test_no_overrides_preserves_profile_defaults(self) -> None:
        """When all checkboxes are off, profileDefaults values are preserved
        but not overridden by the wizard."""
        result = _make_result(
            disable_recap=False, cleanup_10y=False, disable_memory=False
        )
        create_profile(result, self.cfg)
        settings = self._read_settings()
        # profileDefaults already set these; checkboxes being off means
        # the wizard doesn't override them, so the profileDefaults values remain
        canonical = build_canonical_shared_settings(self._scripts_dir)
        profile_defaults = canonical["profileDefaults"]
        self.assertEqual(settings["awaySummaryEnabled"], profile_defaults["awaySummaryEnabled"])
        self.assertEqual(settings["cleanupPeriodDays"], profile_defaults["cleanupPeriodDays"])
        self.assertEqual(settings["autoMemoryEnabled"], profile_defaults["autoMemoryEnabled"])

    def test_overrides_applied_on_top_of_defaults(self) -> None:
        """Overrides merge with values from shared-settings.json profileDefaults."""
        shared = build_canonical_shared_settings(self._scripts_dir)
        shared["profileDefaults"] = {"awaySummaryEnabled": True, "otherKey": 42}
        self._shared_settings_file.write_text(json.dumps(shared))

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
        self.assertEqual(settings["hooks"], self._expected_hooks())

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

        expected_hooks = self._expected_hooks()
        settings = self._read_settings("merged")
        hooks_list = settings["hooks"]["UserPromptSubmit"][0]["hooks"]
        cmds = [h["command"] for h in hooks_list]
        # Existing hook preserved
        self.assertIn("existing-hook-cmd", cmds)
        # Template hooks added
        for wanted in expected_hooks["UserPromptSubmit"][0]["hooks"]:
            self.assertIn(wanted["command"], cmds)

    def test_merge_does_not_duplicate_existing(self) -> None:
        """If the source already has one of the template hooks, it's not added again."""
        expected_hooks = self._expected_hooks()
        template_cmd = expected_hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
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
        self.assertEqual(settings["hooks"], self._expected_hooks())
        # Cloned key is preserved
        self.assertEqual(settings["someKey"], 1)


class SymlinkCreationTests(CreateProfileTestBase):
    """Test 7: all 6 dirs symlinked to ~/.claudewheel/shared/ when symlink_shared=True."""

    def test_symlinks_created(self) -> None:
        result = _make_result(symlink_shared=True)
        create_profile(result, self.cfg)

        profile_dir = self._profile_dir()
        shared_base = self.fake_home / ".claudewheel" / "shared"
        for dirname in PROFILE_SHARED_DIRS:
            link = profile_dir / dirname
            self.assertTrue(link.is_symlink(), f"{dirname} should be a symlink")
            target = shared_base / dirname
            self.assertEqual(link.resolve(), target.resolve())

    def test_shared_target_dirs_created(self) -> None:
        """The target directories under ~/.claudewheel/shared/ are created."""
        result = _make_result(symlink_shared=True)
        create_profile(result, self.cfg)

        shared_base = self.fake_home / ".claudewheel" / "shared"
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
        """~/.claudewheel/shared/ itself should not be created."""
        result = _make_result(symlink_shared=False)
        create_profile(result, self.cfg)
        shared_base = self.fake_home / ".claudewheel" / "shared"
        self.assertFalse(shared_base.exists())


class OptionsRegistrationTests(CreateProfileTestBase):
    """Test 9: add_option and set_option_metadata called correctly."""

    def test_add_option_called(self) -> None:
        """The profile name is added to the 'profile' segment options."""
        result = _make_result(name="newprof")
        create_profile(result, self.cfg)

        # Reload options from disk to verify persistence
        options = json.loads((self.launcher_dir / "options.json").read_text())
        self.assertIn("newprof", options["profile"]["pinned"])

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


class FakeTerminal:
    """A mock Terminal that feeds pre-recorded keystrokes and captures output."""

    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self._index = 0
        self.rows = 40
        self.cols = 120
        self.output: list[str] = []

    def enter_raw(self) -> None:
        pass

    def exit_raw(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_size(self) -> tuple[int, int]:
        return self.rows, self.cols

    def read_key(self) -> str:
        if self._index >= len(self._keys):
            # Safety net: if keys are exhausted, cancel the wizard
            return "ESC"
        key = self._keys[self._index]
        self._index += 1
        return key

    def write(self, text: str) -> None:
        self.output.append(text)

    def flush(self) -> None:
        pass


class WizardTUITestBase(unittest.TestCase):
    """Base class for wizard TUI tests.

    Patches Terminal, signal.signal, and PROFILES_DIR so run_profile_wizard
    can execute without a real terminal or filesystem side effects.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake_profiles_dir = Path(self._tmp.name) / "profiles"
        self.fake_profiles_dir.mkdir(parents=True)

        # Patch PROFILES_DIR so _validate_name checks our temp dir
        self._profiles_patch = mock.patch.object(
            wizard_mod, "PROFILES_DIR", self.fake_profiles_dir
        )
        self._profiles_patch.start()
        self.addCleanup(self._profiles_patch.stop)

        # Patch signal.signal to avoid SIGWINCH issues in test
        self._signal_patch = mock.patch(
            "claudewheel.wizard.signal.signal",
            return_value=signal.SIG_DFL,
        )
        self._signal_patch.start()
        self.addCleanup(self._signal_patch.stop)

    def _run_wizard(
        self, keys: list[str], existing_profiles: list[str] | None = None,
    ) -> WizardResult:
        """Run the wizard with fake keystrokes and return the result."""
        if existing_profiles is None:
            existing_profiles = []
        fake_term = FakeTerminal(keys)
        with mock.patch("claudewheel.wizard.Terminal", return_value=fake_term):
            return run_profile_wizard(existing_profiles)


class EnterFromNameSubmitsTests(WizardTUITestBase):
    """Test that pressing Enter while focused on the Name field submits the form."""

    def test_type_name_and_enter_submits(self) -> None:
        """Type 'myprofile' then ENTER. Should return a result with that name."""
        keys = list("myprofile") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "myprofile")

    def test_type_hyphenated_name_and_enter_submits(self) -> None:
        """Type 'my-test' then ENTER. Should return result with that name."""
        keys = list("my-test") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "my-test")

    def test_defaults_all_checkboxes_true(self) -> None:
        """When submitting from Name, all checkbox defaults should be True."""
        keys = list("basic") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertTrue(result.wire_hooks)
        self.assertTrue(result.symlink_shared)
        self.assertTrue(result.disable_recap)
        self.assertTrue(result.cleanup_10y)
        self.assertTrue(result.disable_memory)
        self.assertTrue(result.disable_attribution)

    def test_default_clone_from_is_none(self) -> None:
        """Settings source defaults to 'Defaults template', so clone_from is None."""
        keys = list("quick") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertIsNone(result.clone_from)

    def test_config_dir_matches_name(self) -> None:
        """Config dir should reflect the typed name."""
        keys = list("myname") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertEqual(result.config_dir, "~/.claudewheel/profiles/myname")


class EnterOnEmptyNameTests(WizardTUITestBase):
    """Test that Enter on empty Name field shows an error instead of submitting."""

    def test_enter_on_empty_rejected_then_valid_name_accepted(self) -> None:
        """First ENTER is rejected (empty name), then type 'valid' + ENTER works."""
        keys = ["ENTER"] + list("valid") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "valid")

    def test_enter_on_whitespace_only_rejected(self) -> None:
        """Spaces followed by ENTER should be rejected (name is stripped)."""
        keys = [" ", " ", "ENTER"] + ["BACKSPACE", "BACKSPACE"] + list("ok") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "ok")


class EscCancelsTests(WizardTUITestBase):
    """Test that ESC cancels the wizard."""

    def test_esc_cancels_immediately(self) -> None:
        """Pressing ESC should return a cancelled result."""
        keys = ["ESC"]
        result = self._run_wizard(keys)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.name, "")

    def test_ctrl_c_cancels(self) -> None:
        """CTRL_C should also cancel."""
        keys = ["CTRL_C"]
        result = self._run_wizard(keys)
        self.assertTrue(result.cancelled)

    def test_esc_after_typing_cancels(self) -> None:
        """ESC after typing some characters should cancel without submitting."""
        keys = list("half") + ["ESC"]
        result = self._run_wizard(keys)
        self.assertTrue(result.cancelled)


class NameValidationTests(WizardTUITestBase):
    """Test that invalid names are rejected and the wizard continues."""

    def test_uppercase_rejected(self) -> None:
        """Uppercase chars in name should be rejected by validation."""
        # Type "UPPER" + ENTER (rejected), then clear and type valid name
        keys = list("UPPER") + ["ENTER"]
        # The characters 'U', 'P', 'E', 'R' are uppercase and won't match
        # the regex [a-z0-9][a-z0-9-]*. But they ARE printable, so they get
        # appended to the text field. The validation happens on ENTER.
        keys += ["BACKSPACE"] * 5 + list("valid") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "valid")

    def test_reserved_name_rejected(self) -> None:
        """'default' is a reserved name and should be rejected."""
        keys = list("default") + ["ENTER"]
        keys += ["BACKSPACE"] * 7 + list("ok") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "ok")

    def test_existing_profile_name_rejected(self) -> None:
        """A name that already exists in existing_profiles should be rejected."""
        keys = list("taken") + ["ENTER"]
        keys += ["BACKSPACE"] * 5 + list("fresh") + ["ENTER"]
        result = self._run_wizard(keys, existing_profiles=["taken"])
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "fresh")

    def test_existing_dir_rejected(self) -> None:
        """A name whose profile directory already exists should be rejected."""
        (self.fake_profiles_dir / "exists").mkdir()
        keys = list("exists") + ["ENTER"]
        keys += ["BACKSPACE"] * 6 + list("new") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "new")


class TabNavigationTests(WizardTUITestBase):
    """Test Tab/Shift-Tab navigation through focusable fields."""

    def test_tab_skips_readonly_config_dir(self) -> None:
        """TAB from Name should skip Config dir (readonly) and land on Settings source.

        From Settings source, pressing ENTER does nothing (it's a radio), so
        we TAB again to Advanced, TAB again to Create, then ENTER.
        But first we need a name. Type it, TAB to next fields, then navigate
        back to enter via Create button.
        """
        # Type name, TAB to Settings source, TAB to Advanced, TAB to Create, ENTER
        keys = list("nav-test") + ["TAB", "TAB", "TAB", "ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "nav-test")

    def test_shift_tab_goes_backward(self) -> None:
        """SHIFT_TAB from Settings source should go back to Name."""
        keys = (
            list("test")
            + ["TAB"]        # Name -> Settings source
            + ["SHIFT_TAB"]  # Settings source -> Name
            # Now we're back on Name; clear and retype, then ENTER
            + ["BACKSPACE"] * 4
            + list("back")
            + ["ENTER"]
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "back")

    def test_tab_wraps_around(self) -> None:
        """TAB from the last focusable field should wrap to the first (Name)."""
        # With Advanced collapsed, focusable: Name(0), Settings source(2),
        # Advanced(3), Create(10). So 4 TABs wraps back to Name.
        keys = (
            list("wrap")
            + ["TAB", "TAB", "TAB", "TAB"]  # Name -> SS -> Adv -> Create -> Name
            # Now back on Name, clear and retype
            + ["BACKSPACE"] * 4
            + list("wrapped")
            + ["ENTER"]
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "wrapped")

    def test_shift_tab_wraps_backward(self) -> None:
        """SHIFT_TAB from Name should wrap to the last focusable (Create)."""
        keys = (
            list("myprof")
            + ["SHIFT_TAB"]  # Name -> Create (wraps backward)
            + ["ENTER"]      # ENTER on Create button
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "myprof")


class AdvancedToggleTests(WizardTUITestBase):
    """Test expanding the Advanced section and toggling checkboxes."""

    def test_expand_advanced_and_toggle_checkbox(self) -> None:
        """Expand Advanced, navigate to first checkbox, toggle it off, then submit."""
        keys = (
            list("adv-test")
            + ["TAB"]        # Name -> Settings source
            + ["TAB"]        # Settings source -> Advanced
            + ["RIGHT"]      # "Hide advanced" -> "Show advanced" (expand)
            + ["TAB"]        # Advanced -> Wire common hooks (first checkbox)
            + [" "]          # Toggle Wire hooks off (True -> False)
            # Navigate back to Name to submit via ENTER
            # Focusable with Advanced expanded: Name(0), SS(2), Adv(3),
            # Wire(4), Symlink(5), Recap(6), 10y(7), Memory(8), CoAuth(9), Create(10)
            # Currently at Wire(4), index 3 in focusable list.
            # We need to get back to Name. Shift-Tab 4 times:
            # Wire -> Advanced -> SS -> Name
            + ["SHIFT_TAB"] * 4
            + ["ENTER"]
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "adv-test")
        # Wire hooks was toggled off
        self.assertFalse(result.wire_hooks)
        # All others should still be True (default)
        self.assertTrue(result.symlink_shared)
        self.assertTrue(result.disable_recap)
        self.assertTrue(result.cleanup_10y)
        self.assertTrue(result.disable_memory)
        self.assertTrue(result.disable_attribution)

    def test_toggle_multiple_checkboxes(self) -> None:
        """Toggle two checkboxes off, verify both are reflected."""
        keys = (
            list("multi")
            + ["TAB", "TAB"]         # Name -> SS -> Advanced
            + ["RIGHT"]              # Expand Advanced
            + ["TAB"]                # Advanced -> Wire hooks
            + [" "]                  # Toggle Wire hooks off
            + ["TAB"]                # Wire hooks -> Symlink shared
            + [" "]                  # Toggle Symlink off
            + ["SHIFT_TAB"] * 5     # Back to Name: Sym -> Wire -> Adv -> SS -> Name
            + ["ENTER"]
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertFalse(result.wire_hooks)
        self.assertFalse(result.symlink_shared)
        self.assertTrue(result.disable_recap)

    def test_collapse_advanced_hides_checkboxes(self) -> None:
        """Expanding then collapsing Advanced should return to default behavior."""
        keys = (
            list("collapse")
            + ["TAB", "TAB"]         # Name -> SS -> Advanced
            + ["RIGHT"]              # Expand
            + ["LEFT"]               # Collapse back
            + ["TAB"]                # Advanced -> Create (checkboxes hidden)
            + ["ENTER"]              # Submit from Create button
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "collapse")
        # All defaults should be True since we never toggled anything
        self.assertTrue(result.wire_hooks)
        self.assertTrue(result.symlink_shared)

    def test_space_cycles_advanced_radio(self) -> None:
        """Space on Advanced field should cycle it (same as RIGHT)."""
        keys = (
            list("space-adv")
            + ["TAB", "TAB"]   # Name -> SS -> Advanced
            + [" "]            # Expand via Space
            + ["TAB"]          # Advanced -> Wire hooks (first checkbox, visible)
            + [" "]            # Toggle Wire hooks off
            + ["SHIFT_TAB"] * 4  # Back to Name
            + ["ENTER"]
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertFalse(result.wire_hooks)


class SettingsSourceTests(WizardTUITestBase):
    """Test changing the Settings source radio field."""

    def test_clone_from_existing_profile(self) -> None:
        """Cycle Settings source to an existing profile name."""
        keys = (
            list("clonetest")
            + ["TAB"]        # Name -> Settings source
            + ["RIGHT"]      # "Defaults template" -> "existing" (next option)
            + ["SHIFT_TAB"]  # Back to Name
            + ["ENTER"]
        )
        result = self._run_wizard(keys, existing_profiles=["existing"])
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "clonetest")
        self.assertEqual(result.clone_from, "existing")

    def test_cycle_back_to_defaults(self) -> None:
        """Cycle forward past all profiles and back to Defaults template."""
        keys = (
            list("cycle")
            + ["TAB"]             # Name -> Settings source
            + ["RIGHT"]           # Defaults -> alpha
            + ["RIGHT"]           # alpha -> beta
            + ["RIGHT"]           # beta -> Defaults (wraps)
            + ["SHIFT_TAB"]       # Back to Name
            + ["ENTER"]
        )
        result = self._run_wizard(keys, existing_profiles=["alpha", "beta"])
        self.assertFalse(result.cancelled)
        self.assertIsNone(result.clone_from)

    def test_left_cycles_backward(self) -> None:
        """LEFT on Settings source should cycle backward (wrapping)."""
        keys = (
            list("leftcycle")
            + ["TAB"]        # Name -> Settings source
            + ["LEFT"]       # Defaults -> last profile (wraps backward)
            + ["SHIFT_TAB"]  # Back to Name
            + ["ENTER"]
        )
        result = self._run_wizard(keys, existing_profiles=["alpha", "beta"])
        self.assertFalse(result.cancelled)
        self.assertEqual(result.clone_from, "beta")


class CreateButtonTests(WizardTUITestBase):
    """Test submitting via the Create button."""

    def test_submit_from_create_button(self) -> None:
        """Navigate to Create button and press ENTER."""
        # With Advanced collapsed: Name(0), SS(2), Adv(3), Create(10)
        keys = list("btntest") + ["TAB", "TAB", "TAB", "ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "btntest")

    def test_create_button_validates_name(self) -> None:
        """Create button should validate the name, not blindly submit."""
        # Navigate to Create with empty name -> rejected, then type and submit
        keys = (
            ["TAB", "TAB", "TAB"]  # Name(empty) -> SS -> Adv -> Create
            + ["ENTER"]             # Rejected: empty name
            + ["SHIFT_TAB"] * 3    # Create -> Adv -> SS -> Name
            + list("fixed")
            + ["ENTER"]             # Submit from Name
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "fixed")


class BackspaceTests(WizardTUITestBase):
    """Test backspace editing in the Name field."""

    def test_backspace_deletes_characters(self) -> None:
        """Backspace should delete the last character from the name."""
        keys = list("hello") + ["BACKSPACE", "BACKSPACE"] + list("p") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        # "hello" minus 2 chars = "hel" + "p" = "help"
        self.assertEqual(result.name, "help")

    def test_backspace_on_empty_name_is_safe(self) -> None:
        """Backspace on an empty name should not crash."""
        keys = ["BACKSPACE", "BACKSPACE"] + list("safe") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "safe")


class ErrorClearingTests(WizardTUITestBase):
    """Test that errors are cleared when the user interacts after an error."""

    def test_typing_clears_error(self) -> None:
        """After an error from ENTER on empty, typing should clear it.

        We can't directly observe the error string, but we verify the wizard
        continues to accept input (doesn't get stuck).
        """
        keys = ["ENTER"] + list("works") + ["ENTER"]
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "works")

    def test_tab_clears_error(self) -> None:
        """After an error, TAB should clear it and move focus."""
        keys = (
            ["ENTER"]               # Error: empty name
            + ["TAB"]               # Clear error, move to Settings source
            + ["SHIFT_TAB"]         # Back to Name
            + list("tabclear")
            + ["ENTER"]
        )
        result = self._run_wizard(keys)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.name, "tabclear")


class KeyExhaustionSafetyTests(WizardTUITestBase):
    """Test that the FakeTerminal safety net works when keys are exhausted."""

    def test_exhausted_keys_cancel(self) -> None:
        """If the key list runs out, the FakeTerminal returns ESC to cancel."""
        keys = list("some")  # No ENTER or ESC -- keys will run out
        result = self._run_wizard(keys)
        self.assertTrue(result.cancelled)


class AuthFlowTests(unittest.TestCase):
    """Tests for run_auth_flow() post-wizard auth setup.

    run_auth_flow presents its menu via ui.run_selection (mocked here) and
    returns one of four outcome strings: "authenticated", "skip", "cancel",
    "failed". Assertions use exact string comparison -- all four outcome
    strings are truthy, so truthiness checks would be meaningless.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake_home = Path(self._tmp.name)

        # Capture stdout so tests can inspect printed output
        self._stdout_buf = io.StringIO()
        self._stdout_trap = contextlib.redirect_stdout(self._stdout_buf)
        self._stdout_trap.__enter__()
        self.addCleanup(self._stdout_trap.__exit__, None, None, None)

    def _profile_dir(self, name: str = "test") -> Path:
        return self.fake_home / ".claudewheel" / "profiles" / name

    def _make_fake_binary(self) -> Path:
        fake_binary = self.fake_home / "fake-claude"
        fake_binary.touch()
        fake_binary.chmod(0o755)
        return fake_binary

    def test_skip_choice_returns_skip(self) -> None:
        """Choosing the skip option returns 'skip'."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", return_value="skip"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "skip")

    def test_form_cancel_returns_cancel(self) -> None:
        """Esc/Ctrl-C on the selection form (None) returns 'cancel', not 'skip'."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "cancel")

    def test_selection_options_and_flags(self) -> None:
        """The form gets three (key, label) options, inline (no alt screen)."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection",
                        return_value=None) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test")
        mock_sel.assert_called_once()
        args, kwargs = mock_sel.call_args
        self.assertEqual(args[0], "Authenticate profile 'test'")
        self.assertEqual([key for key, _label in args[1]],
                         ["session", "token", "skip"])
        self.assertFalse(kwargs.get("use_alt_screen", True))

    def test_custom_skip_label(self) -> None:
        """skip_label customizes the third option's label; key stays 'skip'."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection",
                        return_value="skip") as mock_sel:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   skip_label="Launch without auth")
        self.assertEqual(result, "skip")
        args, _kwargs = mock_sel.call_args
        self.assertEqual(args[1][2], ("skip", "Launch without auth"))

    def test_session_login_binary_not_found(self) -> None:
        """Session login returns 'failed' when Claude binary is missing."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", return_value="session"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")
        self.assertIn("not found", self._stdout_buf.getvalue())

    def test_session_login_success(self) -> None:
        """Session login returns 'authenticated' when subprocess succeeds and credentials exist."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("authtest")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            # Simulate claude auth login creating credentials
            (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text("{}")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("claudewheel.wizard.run_selection", return_value="session"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run):
            result = run_auth_flow(config_dir_str, "authtest")
        self.assertEqual(result, "authenticated")
        self.assertIn("successful", self._stdout_buf.getvalue())

    def test_session_login_no_credentials(self) -> None:
        """Session login returns 'failed' when subprocess succeeds but no credentials."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("nocred")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="session"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)):
            result = run_auth_flow(config_dir_str, "nocred")
        self.assertEqual(result, "failed")
        self.assertIn("not complete", self._stdout_buf.getvalue())

    def test_session_login_subprocess_error(self) -> None:
        """Session login returns 'failed' when subprocess returns non-zero."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="session"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")

    def test_long_lived_token_success(self) -> None:
        """Long-lived token path saves token via add_token and returns 'authenticated'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch("builtins.input", return_value="sk-ant-fake-token-12345"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", "sk-ant-fake-token-12345")

    def test_long_lived_token_binary_not_found(self) -> None:
        """Long-lived token returns 'failed' when Claude binary is missing."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")

    def test_long_lived_token_subprocess_error(self) -> None:
        """Long-lived token returns 'failed' when setup-token exits non-zero."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")

    def test_long_lived_token_empty_token(self) -> None:
        """Empty token input returns 'failed'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")
        self.assertIn("No token", self._stdout_buf.getvalue())

    def test_long_lived_token_non_standard_prefix_warns(self) -> None:
        """Token without sk-ant- prefix prints a warning but still saves."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch("builtins.input", return_value="some-other-token"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "authenticated")
        self.assertIn("Warning", self._stdout_buf.getvalue())
        mock_add.assert_called_once_with("test", "some-other-token")

    def test_long_lived_token_keyboard_interrupt_on_paste(self) -> None:
        """KeyboardInterrupt while pasting token returns 'failed'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch("builtins.input", side_effect=KeyboardInterrupt), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")

    def test_long_lived_token_save_error(self) -> None:
        """OSError from add_token returns 'failed'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="token"), \
             mock.patch("builtins.input", return_value="sk-ant-fake-token-12345"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch("claudewheel.wizard.add_token",
                        side_effect=OSError("disk full")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")
        self.assertIn("Error saving token", self._stdout_buf.getvalue())

    def test_session_login_subprocess_os_error(self) -> None:
        """Session login returns 'failed' when subprocess raises OSError."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", return_value="session"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        side_effect=OSError("exec failed")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test")
        self.assertEqual(result, "failed")
        self.assertIn("Error running", self._stdout_buf.getvalue())

    def test_find_claude_binary_falls_back_to_which(self) -> None:
        """When CLAUDE_SYMLINK doesn't exist, falls back to shutil.which."""
        from claudewheel.wizard import _find_claude_binary

        with mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value="/usr/bin/claude"):
            result = _find_claude_binary()
        self.assertEqual(result, "/usr/bin/claude")

    def test_find_claude_binary_returns_none_when_nothing_found(self) -> None:
        """Returns None when both CLAUDE_SYMLINK and which() fail."""
        from claudewheel.wizard import _find_claude_binary

        with mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value=None):
            result = _find_claude_binary()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
