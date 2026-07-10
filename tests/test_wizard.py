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
from claudewheel import profile_ops as profile_ops_mod
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
from claudewheel.theme import parse_theme

THEME = parse_theme(DEFAULT_THEME_DARK)


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
            # ConfigManager() (built in setUp) calls _recover_incomplete_renames(),
            # which now builds a ProfileStore from the config module's own path
            # constants. Redirect those so recovery never touches the real home.
            mock.patch.object(profile_ops_mod, "PROFILES_DIR", self.launcher_dir / "profiles"),
            mock.patch.object(config_mod, "PROFILES_DIR", self.launcher_dir / "profiles"),
            mock.patch.object(config_mod, "TOKENS_FILE", self.launcher_dir / "tokens.json"),
            mock.patch.object(config_mod, "SKILLS_DIR", self.fake_home / ".claudewheel" / "skills"),
            # wizard.create_profile builds a ProfileStore from these constants.
            mock.patch.object(wizard_mod, "PROFILES_DIR", self.launcher_dir / "profiles"),
            mock.patch.object(wizard_mod, "SCRIPTS_DIR", self._scripts_dir),
            mock.patch.object(wizard_mod, "SHARED_SETTINGS_FILE", self._shared_settings_file),
            mock.patch.object(wizard_mod, "SHARED_DIR", self.fake_home / ".claudewheel" / "shared"),
            mock.patch.object(wizard_mod, "SKILLS_DIR", self.fake_home / ".claudewheel" / "skills"),
            mock.patch.object(wizard_mod, "OPTIONS_FILE", self.launcher_dir / "options.json"),
            mock.patch.object(wizard_mod, "STATE_FILE", self.launcher_dir / "state.json"),
            mock.patch.object(wizard_mod, "TOKENS_FILE", self.launcher_dir / "tokens.json"),
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

    def test_no_symlinks_when_checkbox_off(self) -> None:
        """symlink_shared=False threads through create_profile: no links created."""
        result = _make_result(symlink_shared=False)
        create_profile(result, self.cfg)

        profile_dir = self._profile_dir()
        for dirname in PROFILE_SHARED_DIRS:
            self.assertFalse((profile_dir / dirname).exists(),
                             f"{dirname} should not exist")
            self.assertFalse((profile_dir / dirname).is_symlink(),
                             f"{dirname} should not be a symlink")
        self.assertFalse((profile_dir / "skills").exists())
        self.assertFalse((profile_dir / "skills").is_symlink())
        # Settings still written even without symlinks.
        self.assertTrue((profile_dir / "settings.json").exists())

    # NOTE: a pre-existing profile dir is a hard error (ProfileStore.create
    # refuses it). The old "existing symlink not overwritten" test is superseded
    # by tests/test_profile_store_write.py::CreateTests::test_create_existing_dir.


class OptionsRegistrationTests(CreateProfileTestBase):
    """Registration lands the profile name in options.json (pinned), no metadata."""

    def test_add_option_called(self) -> None:
        """The profile name is added to the 'profile' segment pinned list."""
        result = _make_result(name="newprof")
        create_profile(result, self.cfg)

        # Reload options from disk to verify persistence
        options = json.loads((self.launcher_dir / "options.json").read_text())
        self.assertIn("newprof", options["profile"]["pinned"])

    def test_no_config_dir_metadata_written(self) -> None:
        """config_dir is never persisted -- no metadata entry is created."""
        result = _make_result(name="newprof")
        create_profile(result, self.cfg)

        options = json.loads((self.launcher_dir / "options.json").read_text())
        self.assertNotIn("newprof", options["profile"].get("metadata", {}))


class SummaryLinesTests(CreateProfileTestBase):
    """create_profile returns summary data; presentation is the caller's job."""

    def test_summary_lines_returned(self) -> None:
        result = _make_result(name="sumtest", wire_hooks=True)
        lines = create_profile(result, self.cfg)
        self.assertEqual(lines[0], "Created profile 'sumtest':")
        joined = "\n".join(lines)
        self.assertIn("Config dir:", joined)
        self.assertIn(str(self._profile_dir("sumtest")), joined)
        self.assertIn("Settings from:  defaults", joined)
        self.assertIn("Hooks wired:    True", joined)

    def test_clone_source_in_summary(self) -> None:
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "src"
        source_dir.mkdir(parents=True)
        result = _make_result(name="cloned", clone_from="src")
        lines = create_profile(result, self.cfg)
        self.assertIn("Settings from:  src", "\n".join(lines))

    def test_nothing_printed(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            create_profile(_make_result(name="quiet"), self.cfg)
        self.assertEqual(buf.getvalue(), "")


class FakeTerminal:
    """A mock Terminal that feeds pre-recorded keystrokes and captures output."""

    def __init__(self, keys: list[str], in_raw: bool = False):
        self._keys = list(keys)
        self._index = 0
        self.rows = 40
        self.cols = 120
        self.output: list[str] = []
        self._in_raw = in_raw

    def enter_raw(self, alt_screen: bool = True) -> None:
        self._in_raw = True

    def exit_raw(self) -> None:
        self._in_raw = False

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

    Patches signal.signal (the form runner in ui.py installs a SIGWINCH
    handler) and PROFILES_DIR so run_profile_wizard can execute without a
    real terminal or filesystem side effects. The FakeTerminal is injected
    directly -- run_profile_wizard takes the terminal as a parameter.
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
            "claudewheel.ui.signal.signal",
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
        self.fake_term = FakeTerminal(keys)
        return run_profile_wizard(existing_profiles, THEME, self.fake_term)


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


class AuthFlowTestBase(unittest.TestCase):
    """Shared setup for run_auth_flow() tests.

    Patches detect_browsers to a fixed single-browser list so the browser
    selection form (shown after picking session/token) is deterministic and
    never scans the real filesystem. Patches STATE_FILE to a temp path so
    the auth flow's browser-choice persistence never touches the real
    ~/.claudewheel/state.json.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake_home = Path(self._tmp.name)

        # Poison HOME so run_auth_flow's Path(config_dir).expanduser() on the
        # literal "~/.claudewheel/profiles/test" resolves into the sandbox
        # instead of the real home.
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.fake_home)
        self.addCleanup(self._restore_home)
        self._home_patch = mock.patch.object(Path, "home", return_value=self.fake_home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

        # Capture stdout so tests can inspect printed output
        self._stdout_buf = io.StringIO()
        self._stdout_trap = contextlib.redirect_stdout(self._stdout_buf)
        self._stdout_trap.__enter__()
        self.addCleanup(self._stdout_trap.__exit__, None, None, None)

        self._browsers_patch = mock.patch(
            "claudewheel.wizard.detect_browsers",
            return_value=[("/usr/bin/firefox", "Firefox")],
        )
        self._browsers_patch.start()
        self.addCleanup(self._browsers_patch.stop)

        # run_auth_flow requires a theme and a terminal; run_selection is
        # mocked in these tests, so a MagicMock terminal suffices.
        self.term = mock.MagicMock()

        # Isolate the auth flow's browser-choice persistence from the real
        # state.json. load_state_value/save_state_value read STATE_FILE from
        # the state module's globals, so patching there covers wizard.py too.
        self.state_file = self.fake_home / "state.json"
        self._state_patch = mock.patch(
            "claudewheel.state.STATE_FILE", self.state_file)
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

    def _restore_home(self) -> None:
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home

    def _read_state(self) -> dict:
        """Read the patched state.json, or {} if it doesn't exist."""
        if not self.state_file.exists():
            return {}
        return json.loads(self.state_file.read_text())

    def _profile_dir(self, name: str = "test") -> Path:
        return self.fake_home / ".claudewheel" / "profiles" / name

    def _make_fake_binary(self) -> Path:
        fake_binary = self.fake_home / "fake-claude"
        fake_binary.touch()
        fake_binary.chmod(0o755)
        return fake_binary


class AuthFlowTests(AuthFlowTestBase):
    """Tests for run_auth_flow() post-wizard auth setup.

    run_auth_flow presents its menu via ui.run_selection (mocked here) and
    returns one of four outcome strings: "authenticated", "skip", "cancel",
    "failed". Assertions use exact string comparison -- all four outcome
    strings are truthy, so truthiness checks would be meaningless.

    Session/token paths mock run_selection with a two-item side_effect:
    the method choice, then the browser choice.
    """

    def test_skip_choice_returns_skip(self) -> None:
        """Choosing the skip option returns 'skip'."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", autospec=True, return_value="skip"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "skip")

    def test_form_cancel_returns_cancel(self) -> None:
        """Esc/Ctrl-C on the selection form (None) returns 'cancel', not 'skip'."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", autospec=True, return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "cancel")

    def test_selection_options_and_flags(self) -> None:
        """The form gets three (key, label) options, theme, and terminal.

        No use_alt_screen override: the form renders fullscreen (borrowed
        as a page when the caller's terminal is already raw).
        """
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value=None) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        mock_sel.assert_called_once()
        args, kwargs = mock_sel.call_args
        self.assertEqual(args[0], "Authenticate profile 'test'")
        self.assertEqual([key for key, _label in args[1]],
                         ["session", "token", "paste", "skip"])
        self.assertEqual(args[2], THEME)
        self.assertIs(args[3], self.term)
        self.assertNotIn("use_alt_screen", kwargs)

    def test_custom_skip_label(self) -> None:
        """skip_label customizes the third option's label; key stays 'skip'."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="skip") as mock_sel:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term,
                                   skip_label="Launch without auth")
        self.assertEqual(result, "skip")
        args, _kwargs = mock_sel.call_args
        self.assertEqual(args[1][3], ("skip", "Launch without auth"))

    def test_session_login_binary_not_found(self) -> None:
        """Session login returns 'failed' when Claude binary is missing."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
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

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run):
            result = run_auth_flow(config_dir_str, "authtest", THEME, self.term)
        self.assertEqual(result, "authenticated")
        self.assertIn("successful", self._stdout_buf.getvalue())

    def test_session_login_no_credentials(self) -> None:
        """Session login returns 'failed' when subprocess succeeds but no credentials."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("nocred")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)):
            result = run_auth_flow(config_dir_str, "nocred", THEME, self.term)
        self.assertEqual(result, "failed")
        self.assertIn("not complete", self._stdout_buf.getvalue())

    def test_session_login_subprocess_error(self) -> None:
        """Session login returns 'failed' when subprocess returns non-zero."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")

    def test_long_lived_token_success(self) -> None:
        """The token is scraped from the PTY capture, validated, and saved.

        No paste prompt appears on the happy path: input() is never called.
        """
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch("builtins.input", side_effect=AssertionError("no paste prompt expected")) as mock_input, \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID) as mock_probe, \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", CAPTURED_TOKEN)
        mock_probe.assert_called_once_with(CAPTURED_TOKEN)
        mock_input.assert_not_called()

    def test_long_lived_token_binary_not_found(self) -> None:
        """Long-lived token returns 'failed' when Claude binary is missing."""
        from claudewheel.wizard import run_auth_flow
        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")

    def test_long_lived_token_subprocess_error(self) -> None:
        """Long-lived token returns 'failed' when setup-token exits non-zero."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(1, b"")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")

    def test_long_lived_token_pty_error(self) -> None:
        """RuntimeError from run_under_pty (no /dev/tty) returns 'failed'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        side_effect=RuntimeError("cannot open /dev/tty")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")
        self.assertIn("Error running claude setup-token",
                      self._stdout_buf.getvalue())

    def test_long_lived_token_empty_recovery_paste(self) -> None:
        """Extraction failure + empty recovery paste returns 'failed'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, b"no token in this output")), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()
        self.assertIn("No token", self._stdout_buf.getvalue())

    def test_long_lived_token_recovery_paste_probe_gated(self) -> None:
        """A recovery-pasted token is gated by the probe, not its prefix.

        Reworked from the deleted warn-and-save path: a token without the
        sk-ant- prefix is no longer saved on a warning -- the live probe is
        the gate. VALID means it saves; there is no prefix warning left.
        """
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch("builtins.input", return_value="some-other-token"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, b"nothing to extract")), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID) as mock_probe, \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "authenticated")
        self.assertNotIn("Warning", self._stdout_buf.getvalue())
        mock_probe.assert_called_once_with("some-other-token")
        mock_add.assert_called_once_with("test", "some-other-token")

    def test_long_lived_token_keyboard_interrupt_on_recovery_paste(self) -> None:
        """KeyboardInterrupt during the recovery paste returns 'failed'."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch("builtins.input", side_effect=KeyboardInterrupt), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, b"nothing to extract")), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()

    def test_long_lived_token_save_error(self) -> None:
        """OSError from add_token returns 'failed' even for a VALID token."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token",
                        side_effect=OSError("disk full")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        self.assertEqual(result, "failed")
        self.assertIn("Error saving token", self._stdout_buf.getvalue())

    def test_session_login_subprocess_os_error(self) -> None:
        """Session login returns 'failed' when subprocess raises OSError."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True, side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        side_effect=OSError("exec failed")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
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


class BrowserSelectionTests(AuthFlowTestBase):
    """Tests for the browser selection form shown after picking an auth method."""

    def test_browser_form_shown_after_session_choice(self) -> None:
        """Picking 'session' shows a second form with browsers + copy option."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "/usr/bin/firefox"]) as mock_sel, \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)):
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(mock_sel.call_count, 2)
        args, kwargs = mock_sel.call_args_list[1]
        self.assertEqual(args[0], "Choose browser")
        self.assertEqual(args[1], [("/usr/bin/firefox", "Firefox"),
                                   ("copy", "Copy URL instead")])
        self.assertNotIn("use_alt_screen", kwargs)

    def test_browser_form_shown_after_token_choice(self) -> None:
        """Picking 'token' also shows the browser form."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]) as mock_sel, \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(1, b"")):
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(mock_sel.call_count, 2)
        args, _kwargs = mock_sel.call_args_list[1]
        self.assertEqual(args[0], "Choose browser")

    def test_no_browser_form_on_skip(self) -> None:
        """Choosing skip never shows the browser form."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["skip"]) as mock_sel:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "skip")
        self.assertEqual(mock_sel.call_count, 1)

    def test_no_browser_form_on_cancel(self) -> None:
        """Cancelling the method form (None) never shows the browser form."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=[None]) as mock_sel:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "cancel")
        self.assertEqual(mock_sel.call_count, 1)

    def test_esc_on_browser_form_cancels(self) -> None:
        """Esc (None) on the browser form cancels the whole auth flow."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]), \
             mock.patch("claudewheel.wizard.subprocess.run") as mock_run:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "cancel")
        mock_run.assert_not_called()

    def test_zero_browsers_shows_copy_only(self) -> None:
        """When no browsers are detected, the form offers only the copy option."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.detect_browsers",
                        return_value=[]), \
             mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        args, _kwargs = mock_sel.call_args_list[1]
        self.assertEqual(args[1], [("copy", "Copy URL instead")])

    def test_session_browser_path_sets_env(self) -> None:
        """A selected browser path is passed to claude auth login via BROWSER."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "/usr/bin/firefox"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)) as mock_run:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["BROWSER"], "/usr/bin/firefox")

    def test_session_copy_sets_browser_false(self) -> None:
        """Choosing 'copy' sets BROWSER=false and prints a note."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)) as mock_run:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["BROWSER"], "false")
        self.assertIn("suppressed", self._stdout_buf.getvalue())

    def test_token_browser_path_sets_env(self) -> None:
        """The token helper passes the browser to setup-token via BROWSER."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "/usr/bin/firefox"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(1, b"")) as mock_pty:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        argv, env = mock_pty.call_args.args
        self.assertEqual(argv[1], "setup-token")
        self.assertEqual(env["BROWSER"], "/usr/bin/firefox")

    def test_token_copy_sets_browser_false(self) -> None:
        """'copy' on the token path sets BROWSER=false for claude setup-token."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(1, b"")) as mock_pty:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        _argv, env = mock_pty.call_args.args
        self.assertEqual(env["BROWSER"], "false")


class AuthBrowserPersistenceTests(AuthFlowTestBase):
    """Tests for remembering the browser choice across auth flows.

    A successful auth stores the chosen browser key (path or "copy") under
    "auth_browser" in state.json; the next browser form pre-focuses it via
    initial_key. Failed or cancelled auth must not persist anything.
    """

    def _successful_session_run(self, browser_choice: str) -> str:
        """Run a session auth that succeeds, choosing *browser_choice*."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("authtest")
        config_dir.mkdir(parents=True, exist_ok=True)
        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text("{}")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", browser_choice]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run):
            return run_auth_flow(str(config_dir), "authtest", THEME, self.term)

    def test_browser_persisted_after_successful_session_auth(self) -> None:
        """A successful session auth writes the browser path to state.json."""
        result = self._successful_session_run("/usr/bin/firefox")
        self.assertEqual(result, "authenticated")
        self.assertEqual(self._read_state().get("auth_browser"),
                         "/usr/bin/firefox")

    def test_copy_choice_persisted_after_successful_auth(self) -> None:
        """The 'copy' pseudo-browser is remembered like a real browser."""
        result = self._successful_session_run("copy")
        self.assertEqual(result, "authenticated")
        self.assertEqual(self._read_state().get("auth_browser"), "copy")

    def test_browser_persisted_after_successful_token_auth(self) -> None:
        """A successful token auth also persists the browser choice."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "/usr/bin/firefox"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "authenticated")
        self.assertEqual(self._read_state().get("auth_browser"),
                         "/usr/bin/firefox")

    def test_browser_persisted_after_unverified_token_save(self) -> None:
        """An explicit unverified save also persists the browser choice.

        The browser step itself worked -- only the validation probe could
        not reach the API.
        """
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "/usr/bin/firefox", "save"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.UNREACHABLE), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "unverified")
        self.assertEqual(self._read_state().get("auth_browser"),
                         "/usr/bin/firefox")

    def test_not_persisted_after_failed_auth(self) -> None:
        """A failed auth (non-zero exit) must not remember the browser."""
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "/usr/bin/firefox"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "failed")
        self.assertNotIn("auth_browser", self._read_state())

    def test_not_persisted_after_browser_form_cancel(self) -> None:
        """Esc on the browser form must not write anything to state."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "cancel")
        self.assertNotIn("auth_browser", self._read_state())

    def test_not_persisted_on_skip(self) -> None:
        """Skipping auth never touches state.json."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="skip"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(result, "skip")
        self.assertNotIn("auth_browser", self._read_state())

    def test_remembered_browser_passed_as_initial_key(self) -> None:
        """The browser form pre-focuses the remembered choice via initial_key."""
        from claudewheel.wizard import run_auth_flow

        self.state_file.write_text(
            json.dumps({"auth_browser": "/usr/bin/firefox"}))

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        _args, kwargs = mock_sel.call_args_list[1]
        self.assertEqual(kwargs.get("initial_key"), "/usr/bin/firefox")

    def test_no_remembered_browser_passes_none(self) -> None:
        """Without a remembered choice, initial_key is None (first option focused)."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        _args, kwargs = mock_sel.call_args_list[1]
        self.assertIsNone(kwargs.get("initial_key"))

    def test_non_string_remembered_value_passes_none(self) -> None:
        """A corrupt (non-string) auth_browser value degrades to initial_key=None."""
        from claudewheel.wizard import run_auth_flow

        self.state_file.write_text(json.dumps({"auth_browser": 42}))

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        _args, kwargs = mock_sel.call_args_list[1]
        self.assertIsNone(kwargs.get("initial_key"))

    def test_stale_browser_still_passed_as_initial_key(self) -> None:
        """A remembered browser missing from the options is still passed through.

        run_selection itself falls back to the first option when initial_key
        isn't found (covered in test_ui.py) -- the wizard doesn't filter.
        """
        from claudewheel.wizard import run_auth_flow

        self.state_file.write_text(
            json.dumps({"auth_browser": "/usr/bin/uninstalled-browser"}))

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", None]) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        args, kwargs = mock_sel.call_args_list[1]
        self.assertEqual(kwargs.get("initial_key"),
                         "/usr/bin/uninstalled-browser")
        # The stale path is not among the offered options
        self.assertNotIn("/usr/bin/uninstalled-browser",
                         [key for key, _label in args[1]])

    def test_persist_preserves_other_state_keys(self) -> None:
        """Writing auth_browser must not clobber unrelated state.json keys."""
        self.state_file.write_text(
            json.dumps({"launch_count": 7, "recent_dirs": ["/home/x"]}))

        result = self._successful_session_run("/usr/bin/firefox")
        self.assertEqual(result, "authenticated")

        state = self._read_state()
        self.assertEqual(state.get("auth_browser"), "/usr/bin/firefox")
        self.assertEqual(state.get("launch_count"), 7)
        self.assertEqual(state.get("recent_dirs"), ["/home/x"])


class CookedWindowTests(AuthFlowTestBase):
    """The subprocess helpers run their whole body inside terminal.cooked().

    Inside the cooked window the claude subprocess, the prints, and the
    token paste input() see a real cooked terminal; raw mode (and the alt
    screen, if any) is restored when the window closes.
    """

    def _track_cooked(self, events: list[str]) -> None:
        """Instrument self.term.cooked() to record enter/exit events."""
        cm = self.term.cooked.return_value
        cm.__enter__.side_effect = lambda *a: events.append("enter")
        cm.__exit__.side_effect = lambda *a: events.append("exit") or False

    def test_session_subprocess_runs_inside_cooked_window(self) -> None:
        from claudewheel.wizard import run_auth_flow

        events: list[str] = []
        self._track_cooked(events)
        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            events.append("subprocess")
            return subprocess.CompletedProcess(cmd, 1)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run):
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.assertEqual(events, ["enter", "subprocess", "exit"])

    def test_token_pty_capture_runs_inside_cooked_window(self) -> None:
        """run_under_pty needs the real cooked terminal (it sets raw itself)."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        events: list[str] = []
        self._track_cooked(events)
        fake_binary = self._make_fake_binary()

        def fake_pty(argv, env):
            events.append("pty")
            return (0, CAPTURED_OUTPUT)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        side_effect=fake_pty), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "authenticated")
        self.assertEqual(events, ["enter", "pty", "exit"])

    def test_recovery_paste_input_happens_inside_cooked_window(self) -> None:
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        events: list[str] = []
        self._track_cooked(events)
        fake_binary = self._make_fake_binary()

        def fake_input(prompt=""):
            events.append("input")
            return "sk-ant-recovered"

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch("builtins.input", side_effect=fake_input), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, b"nothing to extract")), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "authenticated")
        self.assertEqual(events, ["enter", "input", "exit"])

    def test_unverified_choice_form_shown_outside_cooked_window(self) -> None:
        """The Save-unvalidated/Abort form renders after the cooked window
        closed, so it borrows the caller's raw session like the other forms."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        events: list[str] = []
        self._track_cooked(events)
        fake_binary = self._make_fake_binary()

        def fake_selection(title, options, theme, terminal, **kwargs):
            if title.startswith("Token could not be validated"):
                events.append("choice-form")
                return "abort"
            events.append("form")
            return {"Authenticate profile 'test'": "token",
                    "Choose browser": "copy"}[title]

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=fake_selection), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.UNREACHABLE):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "failed")
        self.assertEqual(events,
                         ["form", "form", "enter", "exit", "choice-form"])

    def test_no_cooked_window_on_skip(self) -> None:
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="skip"):
            run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)

        self.term.cooked.assert_not_called()

    def test_binary_lookup_failure_still_inside_cooked_window(self) -> None:
        """Even the binary-not-found print happens inside the cooked window."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", Path("/nonexistent/claude")), \
             mock.patch("claudewheel.wizard.shutil.which", return_value=None):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "failed")
        self.term.cooked.assert_called_once()


# A realistic setup-token capture: label line, then the token. wizard's
# extraction (auth.extract_token) must find CAPTURED_TOKEN in this buffer.
CAPTURED_TOKEN = "sk-ant-oat01-" + "A" * 60
CAPTURED_OUTPUT = (b"Your token (valid for 1 year):\r\n"
                   + CAPTURED_TOKEN.encode("ascii") + b"\r\n")


class TokenValidationRedGreenTests(AuthFlowTestBase):
    """Red-green regression test for the truncated-token bug.

    Originally the token path saved whatever the user pasted and reported
    success without ever probing the API. A truncated or stale token
    (valid-looking sk-ant- prefix, but rejected by the API with 401) was
    saved and reported as "authenticated". This test asserts the fix:
    a token whose validation probe returns INVALID must NEVER be saved
    and the flow must not report "authenticated".

    Written red-first: against the pre-validation code (subprocess.run +
    paste prompt, no probe) this test fails because add_token IS called
    and the outcome IS "authenticated". The mocks cover both the old shape
    (subprocess.run + input paste) and the new shape (run_under_pty capture
    + probe + one recovery re-paste) so the same test demonstrates red and
    green.
    """

    def test_invalid_token_never_saved_and_not_authenticated(self) -> None:
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch("claudewheel.wizard.run_under_pty", create=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.INVALID), \
             mock.patch("builtins.input",
                        return_value="sk-ant-oat01-TRUNCATED"), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        mock_add.assert_not_called()
        self.assertNotEqual(result, "authenticated")
        self.assertEqual(result, "failed")


class TokenRecoveryPasteTests(AuthFlowTestBase):
    """Tests for the explicit manual-paste recovery when extraction fails.

    Reworked from the old TokenPasteTests: the paste is no longer the normal
    path (the token is scraped from the PTY capture) -- it only appears as a
    clearly labeled recovery step after an extraction failure, and every
    pasted token still goes through the validation probe.
    """

    def _run_recovery_flow(self, pasted: str,
                           probe_result: str | None = None,
                           ) -> tuple[str, mock.MagicMock]:
        """Run the token path with extraction failing and *pasted* recovery."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()
        if probe_result is None:
            probe_result = auth.VALID

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch("builtins.input", return_value=pasted), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, b"nothing extractable here")), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=probe_result), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test", THEME, self.term)
        return result, mock_add

    def test_embedded_whitespace_removed(self) -> None:
        """Linebreaks, spaces, and tabs from a wrapped terminal copy are stripped."""
        result, mock_add = self._run_recovery_flow("sk-ant-oat01-\nABC DEF\t123")
        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", "sk-ant-oat01-ABCDEF123")

    def test_surrounding_whitespace_removed(self) -> None:
        """Leading/trailing whitespace is stripped like the old .strip() did."""
        result, mock_add = self._run_recovery_flow("  sk-ant-token-1  \n")
        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", "sk-ant-token-1")

    def test_whitespace_only_input_fails(self) -> None:
        """Input that cleans down to nothing is treated as no token."""
        result, mock_add = self._run_recovery_flow(" \n\t ")
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()
        self.assertIn("No token", self._stdout_buf.getvalue())

    def test_extraction_failure_message_and_prompt_printed(self) -> None:
        """The recovery is explicit: a hard error message, then a labeled prompt."""
        self._run_recovery_flow("sk-ant-anything")
        out = self._stdout_buf.getvalue()
        self.assertIn("could not extract the token", out)

    def test_recovery_pasted_token_goes_through_probe(self) -> None:
        """A recovery-pasted token that the API rejects is never saved."""
        from claudewheel import auth
        result, mock_add = self._run_recovery_flow(
            "sk-ant-bad", probe_result=auth.INVALID)
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()


class TokenValidationOutcomeTests(AuthFlowTestBase):
    """Tests for the five-outcome hard-validation flow (Phase 3b)."""

    def _run_scraped_flow(self, probe_results, selections=None,
                          reprompt="", captured=CAPTURED_OUTPUT):
        """Run the token path with a successful scrape.

        probe_results: side_effect list for validate_token.
        selections: run_selection side_effect (default token/copy).
        reprompt: what input() returns if the INVALID re-prompt fires.
        """
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()
        if selections is None:
            selections = ["token", "copy"]

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=selections) as mock_sel, \
             mock.patch("builtins.input", return_value=reprompt) as mock_input, \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, captured)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        side_effect=probe_results) as mock_probe, \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)
        return result, mock_add, mock_probe, mock_input, mock_sel

    def test_valid_token_saved_no_paste_prompt(self) -> None:
        """VALID: the scraped token is saved; input() is never called."""
        from claudewheel import auth
        result, mock_add, mock_probe, mock_input, _sel = \
            self._run_scraped_flow([auth.VALID])
        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", CAPTURED_TOKEN)
        mock_probe.assert_called_once_with(CAPTURED_TOKEN)
        mock_input.assert_not_called()

    def test_invalid_then_valid_reprompt_saves_pasted_token(self) -> None:
        """INVALID scrape + VALID re-paste: the re-pasted token is saved."""
        from claudewheel import auth
        result, mock_add, mock_probe, mock_input, _sel = \
            self._run_scraped_flow([auth.INVALID, auth.VALID],
                                   reprompt="sk-ant-oat01-repasted")
        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", "sk-ant-oat01-repasted")
        self.assertEqual(mock_probe.call_args_list,
                         [mock.call(CAPTURED_TOKEN),
                          mock.call("sk-ant-oat01-repasted")])
        self.assertIn("rejected by the API (401)",
                      self._stdout_buf.getvalue())

    def test_invalid_twice_fails_never_saved(self) -> None:
        """INVALID scrape + INVALID re-paste: exactly one re-prompt, no save."""
        from claudewheel import auth
        result, mock_add, mock_probe, mock_input, _sel = \
            self._run_scraped_flow([auth.INVALID, auth.INVALID],
                                   reprompt="sk-ant-still-bad")
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()
        mock_input.assert_called_once()
        self.assertEqual(mock_probe.call_count, 2)

    def test_invalid_then_empty_reprompt_fails(self) -> None:
        """INVALID scrape + empty re-paste: failed, nothing saved."""
        from claudewheel import auth
        result, mock_add, mock_probe, _input, _sel = \
            self._run_scraped_flow([auth.INVALID], reprompt="")
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()
        mock_probe.assert_called_once()

    def test_unreachable_save_choice_returns_unverified(self) -> None:
        """UNREACHABLE + explicit 'Save unvalidated': saved, 'unverified'."""
        from claudewheel import auth
        result, mock_add, _probe, mock_input, mock_sel = \
            self._run_scraped_flow([auth.UNREACHABLE],
                                   selections=["token", "copy", "save"])
        self.assertEqual(result, "unverified")
        mock_add.assert_called_once_with("test", CAPTURED_TOKEN)
        mock_input.assert_not_called()
        # The third run_selection call is the save/abort choice form
        args, _kwargs = mock_sel.call_args_list[2]
        self.assertIn("Token could not be validated", args[0])
        self.assertIn("API unreachable", args[0])
        self.assertEqual([key for key, _label in args[1]],
                         ["save", "abort"])

    def test_unreachable_abort_choice_fails_not_saved(self) -> None:
        """UNREACHABLE + 'Abort': failed, nothing saved."""
        from claudewheel import auth
        result, mock_add, _probe, _input, _sel = \
            self._run_scraped_flow([auth.UNREACHABLE],
                                   selections=["token", "copy", "abort"])
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()

    def test_unreachable_choice_form_cancel_fails_closed(self) -> None:
        """Esc (None) on the choice form counts as abort, not save."""
        from claudewheel import auth
        result, mock_add, _probe, _input, _sel = \
            self._run_scraped_flow([auth.UNREACHABLE],
                                   selections=["token", "copy", None])
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()

    def test_indeterminate_save_choice_returns_unverified(self) -> None:
        """INDETERMINATE behaves like UNREACHABLE: explicit choice to save."""
        from claudewheel import auth
        result, mock_add, _probe, _input, mock_sel = \
            self._run_scraped_flow([auth.INDETERMINATE],
                                   selections=["token", "copy", "save"])
        self.assertEqual(result, "unverified")
        mock_add.assert_called_once_with("test", CAPTURED_TOKEN)
        args, _kwargs = mock_sel.call_args_list[2]
        self.assertIn("validation inconclusive", args[0])

    def test_indeterminate_abort_choice_fails_not_saved(self) -> None:
        """INDETERMINATE + 'Abort': failed, nothing saved."""
        from claudewheel import auth
        result, mock_add, _probe, _input, _sel = \
            self._run_scraped_flow([auth.INDETERMINATE],
                                   selections=["token", "copy", "abort"])
        self.assertEqual(result, "failed")
        mock_add.assert_not_called()

    def test_unverified_save_error_fails(self) -> None:
        """OSError while saving the unverified token still returns 'failed'."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy", "save"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.UNREACHABLE), \
             mock.patch("claudewheel.wizard.add_token",
                        side_effect=OSError("disk full")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)
        self.assertEqual(result, "failed")
        self.assertIn("Error saving token", self._stdout_buf.getvalue())

    def test_reprompted_token_unreachable_offers_choice(self) -> None:
        """INVALID scrape + re-paste whose probe is UNREACHABLE: choice form."""
        from claudewheel import auth
        result, mock_add, mock_probe, mock_input, _sel = \
            self._run_scraped_flow([auth.INVALID, auth.UNREACHABLE],
                                   selections=["token", "copy", "save"],
                                   reprompt="sk-ant-oat01-repasted")
        self.assertEqual(result, "unverified")
        mock_add.assert_called_once_with("test", "sk-ant-oat01-repasted")


class OnboardingFlagTests(CreateProfileTestBase):
    """create_profile() must write .claude.json with hasCompletedOnboarding: true."""

    def _read_claude_json(self, name: str = "test") -> dict:
        path = self._profile_dir(name) / ".claude.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def test_create_profile_writes_onboarding_flag(self) -> None:
        """A freshly created profile has .claude.json with hasCompletedOnboarding."""
        result = _make_result(name="onboard")
        create_profile(result, self.cfg)
        cj = self._read_claude_json("onboard")
        self.assertTrue(cj.get("hasCompletedOnboarding"),
                        ".claude.json must contain hasCompletedOnboarding: true")

    # NOTE: the "preserve existing .claude.json" and "corrupt .claude.json"
    # cases pre-created the profile directory, which ProfileStore.create now
    # refuses (FileExistsError). create() always writes onboarding into a fresh
    # dir; the merge/tolerance behavior of _set_onboarding_flag remains exercised
    # via run_auth_flow (OnboardingFlagAuthTests) where .claude.json may pre-exist.


class OnboardingFlagAuthTests(AuthFlowTestBase):
    """run_auth_flow() must write .claude.json with hasCompletedOnboarding
    after successful auth (authenticated or unverified)."""

    def _read_claude_json(self, config_dir: str) -> dict:
        path = Path(config_dir).expanduser() / ".claude.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def test_session_login_success_writes_onboarding_flag(self) -> None:
        """After successful session login, .claude.json has the flag."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("authonboard")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)
        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text("{}")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run):
            result = run_auth_flow(config_dir_str, "authonboard", THEME, self.term)

        self.assertEqual(result, "authenticated")
        cj = self._read_claude_json(config_dir_str)
        self.assertTrue(cj.get("hasCompletedOnboarding"),
                        ".claude.json must contain hasCompletedOnboarding after session login")

    def test_token_valid_writes_onboarding_flag(self) -> None:
        """After a validated token save, .claude.json has the flag."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("tokenonboard")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)
        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow(config_dir_str, "tokenonboard", THEME, self.term)

        self.assertEqual(result, "authenticated")
        cj = self._read_claude_json(config_dir_str)
        self.assertTrue(cj.get("hasCompletedOnboarding"),
                        ".claude.json must contain hasCompletedOnboarding after token save")

    def test_unverified_save_writes_onboarding_flag(self) -> None:
        """After an unverified token save, .claude.json has the flag."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("unverified")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)
        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy", "save"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.UNREACHABLE), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow(config_dir_str, "unverified", THEME, self.term)

        self.assertEqual(result, "unverified")
        cj = self._read_claude_json(config_dir_str)
        self.assertTrue(cj.get("hasCompletedOnboarding"),
                        ".claude.json must contain hasCompletedOnboarding after unverified save")

    def test_failed_auth_does_not_write_flag(self) -> None:
        """Failed auth must NOT write the onboarding flag."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("failauth")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)
        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1)):
            result = run_auth_flow(config_dir_str, "failauth", THEME, self.term)

        self.assertEqual(result, "failed")
        cj = self._read_claude_json(config_dir_str)
        self.assertFalse(cj.get("hasCompletedOnboarding", False),
                         "Failed auth must not write hasCompletedOnboarding")

    def test_auth_preserves_existing_claude_json_keys(self) -> None:
        """Auth must merge the flag without clobbering existing keys."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("mergeauth")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)
        existing = {"machineID": "xyz", "someOther": 42}
        (config_dir / ".claude.json").write_text(json.dumps(existing))
        fake_binary = self._make_fake_binary()

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["token", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.run_under_pty", autospec=True,
                        return_value=(0, CAPTURED_OUTPUT)), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow(config_dir_str, "mergeauth", THEME, self.term)

        self.assertEqual(result, "authenticated")
        cj = self._read_claude_json(config_dir_str)
        self.assertTrue(cj.get("hasCompletedOnboarding"))
        self.assertEqual(cj["machineID"], "xyz")
        self.assertEqual(cj["someOther"], 42)


class TierCaptureTests(AuthFlowTestBase):
    """Tests for rate-limit tier capture during session login."""

    def test_tier_captured_on_session_login(self) -> None:
        """When .credentials.json has rateLimitTier, it is stored in tokens.json."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("tiertest")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        tokens_file = self.fake_home / ".claudewheel" / "tokens.json"
        tokens_file.parent.mkdir(parents=True, exist_ok=True)

        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            creds = {
                "claudeAiOauth": {
                    "rateLimitTier": "default_claude_pro",
                    "subscriptionType": "claude_pro",
                    "accessToken": "secret",
                }
            }
            (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text(
                json.dumps(creds))
            return subprocess.CompletedProcess(cmd, 0)

        from claudewheel import tokens as tokens_mod
        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run), \
             mock.patch.object(tokens_mod, "TOKENS_FILE", tokens_file):
            result = run_auth_flow(config_dir_str, "tiertest", THEME, self.term)

        self.assertEqual(result, "authenticated")
        tokens = json.loads(tokens_file.read_text())
        entry = tokens["tiertest"]
        self.assertEqual(entry["rateLimitTier"], "default_claude_pro")
        self.assertEqual(entry["subscriptionType"], "claude_pro")
        # Should NOT have the access token
        self.assertNotIn("accessToken", entry)

    def test_tier_not_captured_when_absent(self) -> None:
        """When .credentials.json has no tier fields, tokens.json is not written."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("notier")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        tokens_file = self.fake_home / ".claudewheel" / "tokens.json"
        tokens_file.parent.mkdir(parents=True, exist_ok=True)

        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            # No claudeAiOauth section
            (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text("{}")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run), \
             mock.patch("claudewheel.wizard.store_tier") as mock_store:
            result = run_auth_flow(config_dir_str, "notier", THEME, self.term)

        self.assertEqual(result, "authenticated")
        mock_store.assert_not_called()

    def test_tier_capture_tolerates_corrupt_credentials(self) -> None:
        """Corrupt .credentials.json does not crash the auth flow."""
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("corrupt")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        fake_binary = self._make_fake_binary()

        def fake_run(cmd, env=None):
            (Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json").write_text("{bad json")
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["session", "copy"]), \
             mock.patch.object(wizard_mod, "CLAUDE_SYMLINK", fake_binary), \
             mock.patch("claudewheel.wizard.subprocess.run", side_effect=fake_run), \
             mock.patch("claudewheel.wizard.store_tier") as mock_store:
            result = run_auth_flow(config_dir_str, "corrupt", THEME, self.term)

        # Auth succeeds (credentials file exists) despite corrupt JSON
        self.assertEqual(result, "authenticated")
        mock_store.assert_not_called()


class HookMergeGapTests(CreateProfileTestBase):
    """Phase 3: every canonical hook wiring lands, not just UserPromptSubmit.

    Regression for the wizard merge gap: cloning a profile that already had a
    hooks section used to merge ONLY UserPromptSubmit, silently dropping the
    PreToolUse (Agent/Bash) and PostToolUse (Bash) wirings. The wizard now
    reuses the additive, matcher-based merge so all four wirings survive.
    """

    def _assert_all_wirings(self, hooks: dict) -> None:
        """Assert every guardrail.EXPECTED_HOOK_WIRINGS tuple is wired."""
        from claudewheel import guardrail
        for event, matcher, script in guardrail.EXPECTED_HOOK_WIRINGS:
            entries = hooks.get(event, [])
            entry = next(
                (e for e in entries if e.get("matcher") == matcher), None)
            self.assertIsNotNone(entry, f"missing {event}[{matcher}] wiring")
            cmds = [h["command"] for h in entry["hooks"]]
            self.assertIn(str(self._scripts_dir / script), cmds)

    def test_clone_with_only_userpromptsubmit_gets_all_wirings(self) -> None:
        """A source whose hooks section has only UserPromptSubmit still ends up
        with all four canonical wirings after creation."""
        existing = {
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [
                        {"type": "command",
                         "command": str(self._scripts_dir / "hook-timestamp")},
                    ]},
                ],
            }
        }
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "onlyups"
        source_dir.mkdir(parents=True)
        (source_dir / "settings.json").write_text(json.dumps(existing))

        result = _make_result(name="mergegap", clone_from="onlyups",
                              wire_hooks=True)
        create_profile(result, self.cfg)

        settings = self._read_settings("mergegap")
        self._assert_all_wirings(settings["hooks"])

    def test_clone_custom_ups_hook_preserved_and_all_wired(self) -> None:
        """A user-added UserPromptSubmit hook is preserved while the missing
        canonical wirings are merged in."""
        existing = {
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": "/opt/custom/my-hook"},
                    ]},
                ],
            }
        }
        source_dir = self.fake_home / ".claudewheel" / "profiles" / "custom"
        source_dir.mkdir(parents=True)
        (source_dir / "settings.json").write_text(json.dumps(existing))

        result = _make_result(name="custmerge", clone_from="custom",
                              wire_hooks=True)
        create_profile(result, self.cfg)

        settings = self._read_settings("custmerge")
        self._assert_all_wirings(settings["hooks"])
        cmds = [h["command"]
                for h in settings["hooks"]["UserPromptSubmit"][0]["hooks"]]
        self.assertIn("/opt/custom/my-hook", cmds)

    def test_fresh_profile_gets_canonical_permissions_and_all_wirings(self) -> None:
        """A fresh (non-cloned) profile carries the canonical deny/ask arrays
        from the guardrail model and all four hook wirings."""
        from claudewheel import guardrail
        result = _make_result(name="freshcanon", wire_hooks=True)
        create_profile(result, self.cfg)

        settings = self._read_settings("freshcanon")
        self._assert_all_wirings(settings["hooks"])
        self.assertEqual(
            settings["permissions"]["deny"], guardrail.canonical_deny_rules())
        self.assertEqual(
            settings["permissions"]["ask"], guardrail.canonical_ask_rules())


class PasteTokenTests(AuthFlowTestBase):
    """Tests for the 'Paste token directly' auth flow (_auth_paste_token).

    The paste path skips browser selection entirely -- the user pastes a
    token they already have. Validation and outcome handling mirror
    _auth_long_lived_token.
    """

    def test_paste_valid_token_saves_and_authenticates(self) -> None:
        """VALID paste: token saved, outcome is 'authenticated'."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input", return_value="sk-ant-oat01-GOODTOKEN123"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID) as mock_probe, \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", "sk-ant-oat01-GOODTOKEN123")
        mock_probe.assert_called_once_with("sk-ant-oat01-GOODTOKEN123")

    def test_paste_invalid_then_valid_repaste_saves(self) -> None:
        """INVALID first paste + VALID re-paste: the re-pasted token is saved."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input",
                        side_effect=["sk-ant-bad", "sk-ant-good"]), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        side_effect=[auth.INVALID, auth.VALID]) as mock_probe, \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "authenticated")
        mock_add.assert_called_once_with("test", "sk-ant-good")
        self.assertEqual(mock_probe.call_count, 2)
        self.assertIn("rejected by the API (401)",
                      self._stdout_buf.getvalue())

    def test_paste_invalid_twice_fails(self) -> None:
        """INVALID first paste + INVALID re-paste: fails, nothing saved."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input",
                        side_effect=["sk-ant-bad1", "sk-ant-bad2"]), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        side_effect=[auth.INVALID, auth.INVALID]), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "failed")
        mock_add.assert_not_called()

    def test_paste_unreachable_save_returns_unverified(self) -> None:
        """UNREACHABLE + explicit 'Save unvalidated': saved, 'unverified'."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["paste", "save"]) as mock_sel, \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.UNREACHABLE), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "unverified")
        mock_add.assert_called_once_with("test", "sk-ant-token")
        # The second run_selection call is the save/abort choice form
        args, _kwargs = mock_sel.call_args_list[1]
        self.assertIn("API unreachable", args[0])

    def test_paste_unreachable_abort_fails(self) -> None:
        """UNREACHABLE + 'Abort': failed, nothing saved."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["paste", "abort"]), \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.UNREACHABLE), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "failed")
        mock_add.assert_not_called()

    def test_paste_empty_input_cancels(self) -> None:
        """Empty paste (Enter with no token) returns 'cancel'."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "cancel")
        mock_add.assert_not_called()

    def test_paste_skips_browser_selection(self) -> None:
        """The paste path never shows the browser selection form."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste") as mock_sel, \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            run_auth_flow("~/.claudewheel/profiles/test", "test",
                          THEME, self.term)

        # Only one run_selection call: the auth method choice.
        # No "Choose browser" call.
        mock_sel.assert_called_once()
        args, _kwargs = mock_sel.call_args
        self.assertIn("Authenticate profile", args[0])

    def test_paste_does_not_persist_browser_state(self) -> None:
        """The paste path has no browser -- auth_browser must not be written."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "authenticated")
        self.assertNotIn("auth_browser", self._read_state())

    def test_paste_sets_onboarding_flag(self) -> None:
        """Successful paste auth sets hasCompletedOnboarding in .claude.json."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        config_dir = self._profile_dir("pasteonboard")
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir_str = str(config_dir)

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token"):
            result = run_auth_flow(config_dir_str, "pasteonboard",
                                   THEME, self.term)

        self.assertEqual(result, "authenticated")
        cj_path = config_dir / ".claude.json"
        self.assertTrue(cj_path.exists())
        cj = json.loads(cj_path.read_text())
        self.assertTrue(cj.get("hasCompletedOnboarding"))

    def test_paste_option_in_selection_list(self) -> None:
        """The paste option appears in the auth method selection form."""
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value=None) as mock_sel:
            run_auth_flow("~/.claudewheel/profiles/test", "test",
                          THEME, self.term)

        mock_sel.assert_called_once()
        args, _kwargs = mock_sel.call_args
        keys = [key for key, _label in args[1]]
        self.assertEqual(keys, ["session", "token", "paste", "skip"])

    def test_paste_indeterminate_save_returns_unverified(self) -> None:
        """INDETERMINATE + save: saved, 'unverified'."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        side_effect=["paste", "save"]) as mock_sel, \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.INDETERMINATE), \
             mock.patch("claudewheel.wizard.add_token") as mock_add:
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "unverified")
        mock_add.assert_called_once_with("test", "sk-ant-token")
        args, _kwargs = mock_sel.call_args_list[1]
        self.assertIn("validation inconclusive", args[0])

    def test_paste_save_error_returns_failed(self) -> None:
        """OSError from add_token returns 'failed' even for a VALID token."""
        from claudewheel import auth
        from claudewheel.wizard import run_auth_flow

        with mock.patch("claudewheel.wizard.run_selection", autospec=True,
                        return_value="paste"), \
             mock.patch("builtins.input", return_value="sk-ant-token"), \
             mock.patch("claudewheel.auth.validate_token", autospec=True,
                        return_value=auth.VALID), \
             mock.patch("claudewheel.wizard.add_token",
                        side_effect=OSError("disk full")):
            result = run_auth_flow("~/.claudewheel/profiles/test", "test",
                                   THEME, self.term)

        self.assertEqual(result, "failed")
        self.assertIn("Error saving token", self._stdout_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
