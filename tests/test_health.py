"""Tests for health check functions in claudewheel.health."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel import guardrail, health
from claudewheel.defaults import DISALLOWED_TOOLS
from claudewheel.health import (
    _discover_profiles,
    check_auth_shadow,
    check_canonical_permissions_drift,
    check_deployed_hook_drift,
    check_hooks_wired,
    check_orphan_profiles,
    check_relocated_hook_paths,
    check_settings_defaults,
    check_shared_symlinks,
    check_tmp_claude_size,
    check_tokens,
    run_health_check,
)


class _HomeDirTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as Path.home() and patches it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher = patch.object(Path, "home", return_value=self.home)
        self._patcher.start()
        # Patch module-level path constants that were computed at import time
        self._shared_dir = self.home / ".claudewheel" / "shared"
        self._skills_dir = self.home / ".claudewheel" / "skills"
        self._profiles_dir = self.home / ".claudewheel" / "profiles"
        self._tokens_file = self.home / ".claudewheel" / "tokens.json"
        self._dir_patches = [
            patch("claudewheel.health.SKILLS_DIR", self._skills_dir),
            patch("claudewheel.health.PROFILES_DIR", self._profiles_dir),
            # health builds a ProfileStore + TokenStore from its own module
            # constants; without this the store hits the real
            # ~/.claudewheel/tokens.json.
            patch("claudewheel.health.TOKENS_FILE", self._tokens_file),
            patch("claudewheel.discovery.SHARED_DIR", self._shared_dir),
            patch("claudewheel.discovery.SKILLS_DIR", self._skills_dir),
            patch("claudewheel.discovery.PROFILES_DIR", self._profiles_dir),
            patch("claudewheel.discovery.TOKENS_FILE", self._tokens_file),
            patch("claudewheel.profile_info.PROFILES_DIR", self._profiles_dir),
        ]
        for p in self._dir_patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._dir_patches:
            p.stop()
        self._patcher.stop()
        self._tmp.cleanup()

    def _make_profile(self, name: str) -> Path:
        """Create a profile dir with .credentials.json and return its path."""
        pdir = self._profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        return pdir


# ---------------------------------------------------------------------------
# _discover_profiles
# ---------------------------------------------------------------------------


class DiscoverProfilesTests(_HomeDirTestCase):
    """Tests for _discover_profiles()."""

    def test_finds_dirs_with_credentials(self) -> None:
        """Profiles with .credentials.json are discovered."""
        self._make_profile("alpha")
        self._make_profile("beta")
        result = _discover_profiles()
        names = [p.name for p in result]
        self.assertEqual(names, ["alpha", "beta"])

    def test_ignores_dirs_without_any_marker(self) -> None:
        """Profile dirs without .credentials.json or settings.json are skipped."""
        # Has credentials
        self._make_profile("real")
        # Missing both markers
        fake = self._profiles_dir / "fake"
        fake.mkdir(parents=True, exist_ok=True)
        result = _discover_profiles()
        names = [p.name for p in result]
        self.assertEqual(names, ["real"])

    def test_returns_sorted_list(self) -> None:
        """Profiles are returned sorted by directory name."""
        self._make_profile("zeta")
        self._make_profile("alpha")
        self._make_profile("mid")
        result = _discover_profiles()
        names = [p.name for p in result]
        self.assertEqual(names, ["alpha", "mid", "zeta"])

    def test_finds_token_backed_profile_without_credentials(self) -> None:
        """A profile dir with a token entry but no .credentials.json is discovered."""
        # Dir exists but has no .credentials.json
        (self._profiles_dir / "work").mkdir(parents=True, exist_ok=True)
        # Write tokens.json with a key for "work"
        tokens_dir = self.home / ".claudewheel"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        tokens_file = tokens_dir / "tokens.json"
        tokens_file.write_text(json.dumps({"work": "tok-abc"}))
        with patch("claudewheel.discovery.TOKENS_FILE", tokens_file):
            result = _discover_profiles()
        names = [p.name for p in result]
        self.assertIn("work", names)

    def test_token_profile_merged_and_sorted(self) -> None:
        """Token-backed profiles are merged with credential-based ones and sorted."""
        self._make_profile("beta")
        (self._profiles_dir / "alpha").mkdir(parents=True, exist_ok=True)
        tokens_dir = self.home / ".claudewheel"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        tokens_file = tokens_dir / "tokens.json"
        tokens_file.write_text(json.dumps({"alpha": "tok-a"}))
        with patch("claudewheel.discovery.TOKENS_FILE", tokens_file):
            result = _discover_profiles()
        names = [p.name for p in result]
        self.assertEqual(names, ["alpha", "beta"])

    def test_returns_empty_when_no_profiles(self) -> None:
        """Returns empty list when no .claude-* dirs exist."""
        result = _discover_profiles()
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# check_shared_symlinks
# ---------------------------------------------------------------------------


class CheckSharedSymlinksTests(_HomeDirTestCase):
    """Tests for check_shared_symlinks()."""

    EXPECTED_DIRS = ["projects", "session-env", "file-history", "tasks", "todos", "paste-cache"]

    def _setup_shared(self) -> Path:
        """Create ~/.claudewheel/shared/ with all expected subdirs."""
        shared = self._shared_dir
        shared.mkdir(parents=True, exist_ok=True)
        for d in self.EXPECTED_DIRS:
            (shared / d).mkdir()
        return shared

    def _link_profile(self, pdir: Path, shared: Path) -> None:
        """Create correct symlinks in a profile dir pointing to shared."""
        for d in self.EXPECTED_DIRS:
            link = pdir / d
            link.symlink_to(shared / d)

    def test_ok_when_all_symlinks_correct(self) -> None:
        """Returns OK when every profile has correct symlinks."""
        shared = self._setup_shared()
        pdir = self._make_profile("good")
        self._link_profile(pdir, shared)

        result = check_shared_symlinks()
        self.assertTrue(result.ok)
        self.assertIn("1 profiles OK", result.detail)

    def test_warn_when_symlink_wrong_target(self) -> None:
        """Returns WARN when a symlink points to the wrong target."""
        shared = self._setup_shared()
        pdir = self._make_profile("bad")
        # Create correct symlinks for most dirs
        self._link_profile(pdir, shared)
        # Break one symlink by pointing it elsewhere
        wrong_target = self.home / "wrong"
        wrong_target.mkdir()
        (pdir / "projects").unlink()
        (pdir / "projects").symlink_to(wrong_target)

        result = check_shared_symlinks()
        self.assertFalse(result.ok)
        self.assertIn("bad/projects", result.detail)

    def test_warn_when_dir_not_symlink(self) -> None:
        """Returns WARN when a profile subdir is a real directory, not a symlink."""
        shared = self._setup_shared()
        pdir = self._make_profile("nolink")
        self._link_profile(pdir, shared)
        # Replace one symlink with a real dir
        (pdir / "todos").unlink()
        (pdir / "todos").mkdir()

        result = check_shared_symlinks()
        self.assertFalse(result.ok)
        self.assertIn("nolink/todos", result.detail)

    def test_ok_no_profiles(self) -> None:
        """Returns OK with detail message when no profiles exist."""
        result = check_shared_symlinks()
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


# ---------------------------------------------------------------------------
# check_hooks_wired
# ---------------------------------------------------------------------------


class CheckHooksWiredTests(_HomeDirTestCase):
    """Tests for check_hooks_wired()."""

    def _write_settings(self, pdir: Path, settings: dict) -> None:
        """Write settings.json into a profile directory."""
        (pdir / "settings.json").write_text(json.dumps(settings))

    def _good_settings(self) -> dict:
        """Return settings with all four canonical hook wirings present."""
        return {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {"command": "/usr/bin/hook-timestamp"},
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Agent",
                        "hooks": [
                            {"command": "/usr/bin/hook-block-worktree"},
                        ]
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"command": "/usr/bin/hook-block-unsafe-commands"},
                        ]
                    },
                ],
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"command": "/usr/bin/hook-advise-commands"},
                        ]
                    },
                ],
            }
        }

    def _three_hook_settings(self) -> dict:
        """Return settings with only the three old hooks (no PostToolUse advise)."""
        settings = self._good_settings()
        del settings["hooks"]["PostToolUse"]
        return settings

    def test_ok_when_all_hooks_present(self) -> None:
        """Returns OK when all four canonical hook wirings are present."""
        pdir = self._make_profile("hooked")
        self._write_settings(pdir, self._good_settings())

        result = check_hooks_wired()
        self.assertTrue(result.ok)
        self.assertIn("1 profiles OK", result.detail)

    def test_warn_when_only_three_old_hooks(self) -> None:
        """A profile with only the three old hooks fails, missing PostToolUse advise."""
        pdir = self._make_profile("three-only")
        self._write_settings(pdir, self._three_hook_settings())

        result = check_hooks_wired()
        self.assertFalse(result.ok)
        self.assertIn("PostToolUse", result.detail)
        self.assertIn("hook-advise-commands", result.detail)

    def test_warn_when_hook_timestamp_missing(self) -> None:
        """Returns WARN when hook-timestamp is missing from UserPromptSubmit."""
        pdir = self._make_profile("partial")
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {"command": "/usr/bin/some-other-hook"},
                        ]
                    }
                ]
            }
        }
        self._write_settings(pdir, settings)

        result = check_hooks_wired()
        self.assertFalse(result.ok)
        self.assertIn("hook-timestamp", result.detail)

    def test_warn_when_block_unsafe_commands_missing(self) -> None:
        """Returns WARN when hook-block-unsafe-commands is missing from PreToolUse."""
        pdir = self._make_profile("no-bash-hook")
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {"command": "/usr/bin/hook-timestamp"},
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Agent",
                        "hooks": [
                            {"command": "/usr/bin/hook-block-worktree"},
                        ]
                    }
                ],
            }
        }
        self._write_settings(pdir, settings)

        result = check_hooks_wired()
        self.assertFalse(result.ok)
        self.assertIn("hook-block-unsafe-commands", result.detail)

    def test_warn_when_no_settings_json(self) -> None:
        """Returns WARN when settings.json does not exist."""
        self._make_profile("bare")

        result = check_hooks_wired()
        self.assertFalse(result.ok)
        self.assertIn("no settings.json", result.detail)

    def test_ok_no_profiles(self) -> None:
        """Returns OK when no profiles exist."""
        result = check_hooks_wired()
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


# ---------------------------------------------------------------------------
# check_settings_defaults
# ---------------------------------------------------------------------------


class CheckSettingsDefaultsTests(_HomeDirTestCase):
    """Tests for check_settings_defaults()."""

    def _write_settings(self, pdir: Path, settings: dict) -> None:
        (pdir / "settings.json").write_text(json.dumps(settings))

    def _good_settings(self) -> dict:
        # Permission-array content is now the canonical-drift check's job, so
        # check_settings_defaults no longer enforces any deny/ask count. These
        # arrays are deliberately empty to prove the old thresholds are gone.
        return {
            "awaySummaryEnabled": False,
            "cleanupPeriodDays": 365,
            "autoMemoryEnabled": False,
            "permissions": {
                "deny": [],
                "ask": [],
                "disableAutoMode": "disable",
            },
            "claudewheel": {"disallowedTools": DISALLOWED_TOOLS[:]},
        }

    def test_ok_when_all_correct(self) -> None:
        """Returns OK when all settings match expected defaults."""
        pdir = self._make_profile("correct")
        self._write_settings(pdir, self._good_settings())

        result = check_settings_defaults()
        self.assertTrue(result.ok)
        self.assertIn("1 profiles OK", result.detail)

    def test_ok_when_permission_arrays_below_old_thresholds(self) -> None:
        """Thresholds removed: few (or zero) deny/ask rules no longer warn."""
        pdir = self._make_profile("fewRules")
        settings = self._good_settings()
        settings["permissions"] = {
            "deny": ["Bash(rm:*)"],
            "ask": [],
            "disableAutoMode": "disable",
        }
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertTrue(result.ok)

    def test_warn_when_auto_mode_not_disabled(self) -> None:
        """disableAutoMode is still enforced after the threshold removal."""
        pdir = self._make_profile("autoOn")
        settings = self._good_settings()
        settings["permissions"] = {"deny": [], "ask": []}
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("auto mode not disabled", result.detail)

    def test_warn_when_away_summary_enabled(self) -> None:
        """Returns WARN when awaySummaryEnabled is not false."""
        pdir = self._make_profile("awayOn")
        settings = self._good_settings()
        settings["awaySummaryEnabled"] = True
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("awaySummaryEnabled != false", result.detail)

    def test_warn_when_cleanup_period_too_low(self) -> None:
        """Returns WARN when cleanupPeriodDays < 365."""
        pdir = self._make_profile("lowCleanup")
        settings = self._good_settings()
        settings["cleanupPeriodDays"] = 30
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("cleanupPeriodDays < 365", result.detail)

    def test_warn_when_cleanup_period_missing(self) -> None:
        """Returns WARN when cleanupPeriodDays is absent."""
        pdir = self._make_profile("noCleanup")
        settings = self._good_settings()
        del settings["cleanupPeriodDays"]
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("cleanupPeriodDays < 365", result.detail)

    def test_warn_when_auto_memory_enabled(self) -> None:
        """Returns WARN when autoMemoryEnabled is not false."""
        pdir = self._make_profile("memOn")
        settings = self._good_settings()
        settings["autoMemoryEnabled"] = True
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("autoMemoryEnabled != false", result.detail)

    def test_warn_when_disallowed_tools_missing(self) -> None:
        """Returns WARN when claudewheel.disallowedTools is absent."""
        pdir = self._make_profile("noCw")
        settings = self._good_settings()
        del settings["claudewheel"]
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("missing disallowedTools", result.detail)

    def test_warn_when_stale_top_level_disallowed_tools(self) -> None:
        """Returns WARN when the old top-level disallowedTools key is still present."""
        pdir = self._make_profile("staleKey")
        settings = self._good_settings()
        # Add the old inert top-level key alongside the correct nested one
        settings["disallowedTools"] = DISALLOWED_TOOLS[:]
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertFalse(result.ok)
        self.assertIn("inert top-level disallowedTools", result.detail)

    def test_ok_when_disallowed_tools_in_claudewheel_namespace(self) -> None:
        """Returns OK when disallowedTools lives under claudewheel with no top-level key."""
        pdir = self._make_profile("goodNs")
        settings = self._good_settings()
        # Ensure no top-level key exists
        settings.pop("disallowedTools", None)
        self._write_settings(pdir, settings)

        result = check_settings_defaults()
        self.assertTrue(result.ok)

    def test_ok_no_profiles(self) -> None:
        """Returns OK when no profiles exist."""
        result = check_settings_defaults()
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


# ---------------------------------------------------------------------------
# check_tokens
# ---------------------------------------------------------------------------


class CheckTokensTests(_HomeDirTestCase):
    """Tests for check_tokens()."""

    def setUp(self) -> None:
        super().setUp()
        # check_tokens() uses the module-level TOKENS_FILE constant (computed at
        # import time), so we redirect it at the temp home's .claudewheel/tokens.json.
        self._tokens_file = self.home / ".claudewheel" / "tokens.json"
        self._tokens_patcher = patch("claudewheel.health.TOKENS_FILE", self._tokens_file)
        self._tokens_patcher.start()

    def tearDown(self) -> None:
        self._tokens_patcher.stop()
        super().tearDown()

    def _write_tokens(self, tokens: dict) -> None:
        """Write tokens.json in the temp home's .claudewheel/ dir."""
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text(json.dumps(tokens))

    def test_ok_when_all_profiles_have_tokens(self) -> None:
        """Returns OK when every profile has a matching token entry."""
        self._make_profile("alpha")
        self._make_profile("beta")
        self._write_tokens({"alpha": "tok-aaa", "beta": "tok-bbb"})

        result = check_tokens()
        self.assertTrue(result.ok)
        self.assertIn("2 profiles OK", result.detail)

    def test_warn_when_profile_missing_from_tokens(self) -> None:
        """Returns WARN when a profile has no entry in tokens.json."""
        self._make_profile("alpha")
        self._make_profile("beta")
        self._write_tokens({"alpha": "tok-aaa"})

        result = check_tokens()
        self.assertFalse(result.ok)
        self.assertIn("beta", result.detail)

    def test_ok_when_tokens_file_missing(self) -> None:
        """Returns OK when tokens.json does not exist (nothing to check against)."""
        self._make_profile("lonely")

        result = check_tokens()
        self.assertTrue(result.ok)
        self.assertIn("tokens.json not found", result.detail)

    def test_ok_no_profiles_no_tokens(self) -> None:
        """Returns OK when neither profiles nor tokens.json exist."""
        result = check_tokens()
        self.assertTrue(result.ok)

    def test_warn_when_token_value_empty(self) -> None:
        """Returns WARN when a profile's token value is empty string."""
        self._make_profile("empty")
        self._write_tokens({"empty": ""})

        result = check_tokens()
        self.assertFalse(result.ok)
        self.assertIn("empty", result.detail)

    def test_warn_when_token_value_not_string(self) -> None:
        """Returns WARN when a profile's token value is not a string."""
        self._make_profile("numeric")
        self._write_tokens({"numeric": 12345})

        result = check_tokens()
        self.assertFalse(result.ok)
        self.assertIn("numeric", result.detail)

    def test_no_warn_for_settings_only_profile(self) -> None:
        """Settings-only profiles (no credentials, no token) don't trigger warnings."""
        # Create a profile with only settings.json (no .credentials.json)
        pdir = self._profiles_dir / "newprof"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "settings.json").write_text("{}")
        # Tokens file exists but has no entry for "newprof"
        self._write_tokens({})

        result = check_tokens()
        self.assertTrue(result.ok)
        self.assertNotIn("newprof", result.detail)


# ---------------------------------------------------------------------------
# check_orphan_profiles
# ---------------------------------------------------------------------------


class CheckOrphanProfilesTests(_HomeDirTestCase):
    """Tests for check_orphan_profiles()."""

    def _write_options(self, profile_values: list[str]) -> None:
        """Write a minimal options.json with the given profile values."""
        options_dir = self.home / ".claudewheel"
        options_dir.mkdir(parents=True, exist_ok=True)
        options = {"profile": {"values": profile_values}}
        (options_dir / "options.json").write_text(json.dumps(options))

    def test_ok_when_no_orphans(self) -> None:
        """Returns OK when all profile dirs are registered profiles."""
        self._make_profile("alpha")
        self._write_options(["alpha"])
        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"):
            result = check_orphan_profiles()
        self.assertTrue(result.ok)
        self.assertIn("no orphan", result.detail)

    def test_warns_on_orphan_dir(self) -> None:
        """Returns WARN when a profile dir is not registered anywhere."""
        self._make_profile("known")
        # Create an orphan dir (no .credentials.json, not in options)
        orphan = self._profiles_dir / "stale"
        orphan.mkdir(parents=True, exist_ok=True)
        self._write_options(["known"])
        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"):
            result = check_orphan_profiles()
        self.assertFalse(result.ok)
        self.assertIn("stale", result.detail)

    def test_dir_in_options_not_orphan(self) -> None:
        """A profile dir listed in options.json (without .credentials.json) is not orphan."""
        # Dir exists but has no .credentials.json
        (self._profiles_dir / "pending").mkdir(parents=True, exist_ok=True)
        self._write_options(["pending"])
        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"):
            result = check_orphan_profiles()
        self.assertTrue(result.ok)

    def test_flags_broken_symlinks(self) -> None:
        """Orphan dirs with broken symlinks are flagged in the detail."""
        orphan = self._profiles_dir / "broken"
        orphan.mkdir(parents=True, exist_ok=True)
        # Create a broken symlink inside
        (orphan / "projects").symlink_to(self.home / "nonexistent")
        self._write_options([])
        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"):
            result = check_orphan_profiles()
        self.assertFalse(result.ok)
        self.assertIn("broken", result.detail.lower())
        self.assertIn("projects", result.detail)

    def test_registered_profile_not_orphan(self) -> None:
        """A dir with .credentials.json (registered profile) is never orphan."""
        self._make_profile("real")
        self._write_options([])  # not in options, but has credentials
        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"):
            result = check_orphan_profiles()
        self.assertTrue(result.ok)

    def test_dir_in_tokens_not_orphan(self) -> None:
        """A profile dir with an entry in tokens.json is not orphan."""
        (self._profiles_dir / "work").mkdir(parents=True, exist_ok=True)
        self._write_options([])
        tokens_dir = self.home / ".claudewheel"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        (tokens_dir / "tokens.json").write_text(json.dumps({"work": "tok-abc123"}))
        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"), \
             patch("claudewheel.discovery.TOKENS_FILE",
                    self.home / ".claudewheel" / "tokens.json"):
            result = check_orphan_profiles()
        self.assertTrue(result.ok)

    def test_ok_when_no_profiles_dir(self) -> None:
        """Returns OK when ~/.claudewheel/profiles/ does not exist."""
        # Don't create profiles dir
        with patch("claudewheel.health.PROFILES_DIR",
                    self.home / ".claudewheel" / "profiles"):
            result = check_orphan_profiles()
        self.assertTrue(result.ok)
        self.assertIn("no profiles dir", result.detail)

    def test_pinned_profile_not_orphan(self) -> None:
        """A profile dir listed in options.json pinned (not values) is not orphan.

        Wizard-created profiles are registered in the pinned list, not the
        values list. The orphan check must consider both lists.
        """
        # Dir exists but has no .credentials.json (not discovered)
        (self._profiles_dir / "wizard-prof").mkdir(parents=True, exist_ok=True)
        # Write options.json with the profile only in pinned
        options_dir = self.home / ".claudewheel"
        options_dir.mkdir(parents=True, exist_ok=True)
        options = {"profile": {"values": [], "pinned": ["wizard-prof"]}}
        (options_dir / "options.json").write_text(json.dumps(options))

        with patch("claudewheel.health.OPTIONS_FILE",
                    self.home / ".claudewheel" / "options.json"):
            result = check_orphan_profiles()
        self.assertTrue(result.ok)
        self.assertIn("no orphan", result.detail)


# ---------------------------------------------------------------------------
# check_auth_shadow
# ---------------------------------------------------------------------------


class CheckAuthShadowTests(_HomeDirTestCase):
    """Tests for check_auth_shadow()."""

    def setUp(self) -> None:
        super().setUp()
        self._tokens_file = self.home / ".claudewheel" / "tokens.json"
        self._tokens_patcher = patch("claudewheel.health.TOKENS_FILE", self._tokens_file)
        self._tokens_patcher.start()
        # detect_auth_shadow (in profile_info) reads TOKENS_FILE from its own module
        self._pi_tokens_patcher = patch("claudewheel.profile_info.TOKENS_FILE", self._tokens_file)
        self._pi_tokens_patcher.start()

    def tearDown(self) -> None:
        self._pi_tokens_patcher.stop()
        self._tokens_patcher.stop()
        super().tearDown()

    def _write_tokens(self, tokens: dict) -> None:
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text(json.dumps(tokens))

    def _write_credentials(self, pdir: Path, data: dict) -> None:
        (pdir / ".credentials.json").write_text(json.dumps(data))

    def test_flagged_when_both_token_and_claude_ai_oauth(self) -> None:
        """Profile with both tokens.json entry AND claudeAiOauth in .credentials.json is flagged."""
        pdir = self._make_profile("work")
        self._write_tokens({"work": {"token": "tok-xxx", "created": "2025-01-01", "expires_at": "2026-01-01"}})
        self._write_credentials(pdir, {"claudeAiOauth": {"accessToken": "short-lived"}})

        result = check_auth_shadow()
        self.assertFalse(result.ok)
        self.assertIn("work", result.detail)
        self.assertIn("shadowed", result.detail)

    def test_not_flagged_when_only_token(self) -> None:
        """Profile with only tokens.json entry (no claudeAiOauth) is not flagged."""
        pdir = self._make_profile("clean")
        self._write_tokens({"clean": "tok-abc"})
        self._write_credentials(pdir, {"mcpOAuth": {"some": "data"}})

        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_not_flagged_when_only_credentials(self) -> None:
        """Profile with claudeAiOauth but no tokens.json entry is not flagged."""
        pdir = self._make_profile("session-only")
        self._write_tokens({})  # no entry for "session-only"
        self._write_credentials(pdir, {"claudeAiOauth": {"accessToken": "x"}})

        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_not_flagged_when_mcp_oauth_only(self) -> None:
        """Profile with mcpOAuth but no claudeAiOauth is not flagged."""
        pdir = self._make_profile("mcp-only")
        self._write_tokens({"mcp-only": "tok-mcp"})
        self._write_credentials(pdir, {"mcpOAuth": {"provider": "github"}})

        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_no_tokens_file(self) -> None:
        """Returns OK when tokens.json does not exist (no shadow possible)."""
        self._make_profile("any")
        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_no_profiles(self) -> None:
        """Returns OK when no profiles are discovered."""
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text("{}")
        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


# ---------------------------------------------------------------------------
# check_tmp_claude_size / _real_disk_usage
# ---------------------------------------------------------------------------


class CheckTmpClaudeSizeTests(unittest.TestCase):
    """Tests for check_tmp_claude_size() and its real-usage measurement.

    The check must report the REAL tmpfs block usage of /tmp/claude-$UID/,
    excluding symlink targets (both file and directory symlinks) which live
    outside /tmp and consume zero /tmp space.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name) / "claude"
        self.tmp_dir.mkdir()
        # Somewhere OUTSIDE tmp_dir to host symlink targets.
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _real_blocks(path: Path) -> int:
        """Real block usage of a single regular file via lstat."""
        st = path.lstat()
        return st.st_blocks * 512

    def test_symlink_to_large_file_not_counted(self) -> None:
        """A symlink to a large file outside the dir must NOT add its size."""
        regular = self.tmp_dir / "real.bin"
        regular.write_bytes(b"\x00" * 100_000)
        big = self.outside / "big.bin"
        big.write_bytes(b"\x00" * 5_000_000)
        (self.tmp_dir / "link.bin").symlink_to(big)

        usage = health._real_disk_usage(self.tmp_dir)
        # Only the one regular file's blocks are counted; the 5 MB symlink
        # target contributes nothing.
        self.assertEqual(usage, self._real_blocks(regular))

    def test_symlinked_directory_not_descended(self) -> None:
        """Contents of a symlinked directory pointing outside must NOT count."""
        regular = self.tmp_dir / "real.bin"
        regular.write_bytes(b"\x00" * 100_000)
        # Directory of heavy files living outside tmp_dir.
        heavy_dir = self.outside / "heavy"
        heavy_dir.mkdir()
        (heavy_dir / "a.bin").write_bytes(b"\x00" * 4_000_000)
        (heavy_dir / "b.bin").write_bytes(b"\x00" * 4_000_000)
        # Symlink the whole directory into tmp_dir.
        (self.tmp_dir / "linkdir").symlink_to(heavy_dir)

        usage = health._real_disk_usage(self.tmp_dir)
        self.assertEqual(usage, self._real_blocks(regular))

    def test_regular_files_counted(self) -> None:
        """Regular (non-symlink) files, including nested ones, ARE counted."""
        f1 = self.tmp_dir / "a.bin"
        f1.write_bytes(b"\x00" * 100_000)
        nested = self.tmp_dir / "sub"
        nested.mkdir()
        f2 = nested / "b.bin"
        f2.write_bytes(b"\x00" * 200_000)

        usage = health._real_disk_usage(self.tmp_dir)
        self.assertEqual(usage, self._real_blocks(f1) + self._real_blocks(f2))
        self.assertGreater(usage, 0)

    def test_check_reports_ok_and_ignores_symlink_targets(self) -> None:
        """The top-level check reports small usage despite a huge symlink target."""
        (self.tmp_dir / "real.bin").write_bytes(b"\x00" * 100_000)
        big = self.outside / "big.bin"
        big.write_bytes(b"\x00" * 5_000_000)
        (self.tmp_dir / "link.bin").symlink_to(big)

        with patch.object(health, "_tmp_claude_dir", return_value=self.tmp_dir):
            result = check_tmp_claude_size()
        self.assertTrue(result.ok)
        self.assertEqual(result.label, "/tmp/claude")
        # Well under 1 MB of real usage -> reported as ~0 MB.
        self.assertIn("MB", result.detail)

    def test_not_present(self) -> None:
        """Returns OK 'not present' when the dir does not exist."""
        missing = Path(self._tmp.name) / "does-not-exist"
        with patch.object(health, "_tmp_claude_dir", return_value=missing):
            result = check_tmp_claude_size()
        self.assertTrue(result.ok)
        self.assertIn("not present", result.detail)

    def test_threshold_warns_above_1gb(self) -> None:
        """Usage above 1024 MB warns with the '>1 GB threshold' message."""
        over = 1025 * 1024 * 1024
        with patch.object(health, "_tmp_claude_dir", return_value=self.tmp_dir), \
             patch.object(health, "_real_disk_usage", return_value=over):
            result = check_tmp_claude_size()
        self.assertFalse(result.ok)
        self.assertIn("1 GB threshold", result.detail)

    def test_threshold_ok_at_1gb_boundary(self) -> None:
        """Usage of exactly 1024 MB is OK (boundary is inclusive)."""
        at = 1024 * 1024 * 1024
        with patch.object(health, "_tmp_claude_dir", return_value=self.tmp_dir), \
             patch.object(health, "_real_disk_usage", return_value=at):
            result = check_tmp_claude_size()
        self.assertTrue(result.ok)
        self.assertNotIn("threshold", result.detail)


# ---------------------------------------------------------------------------
# check_canonical_permissions_drift
# ---------------------------------------------------------------------------


class CheckCanonicalPermissionsDriftTests(_HomeDirTestCase):
    """Tests for check_canonical_permissions_drift()."""

    def setUp(self) -> None:
        super().setUp()
        # The check reads the module-level SHARED_SETTINGS_FILE constant, so
        # redirect it into the temp home.
        self._shared_settings_file = self.home / ".claudewheel" / "shared-settings.json"
        self._ss_patcher = patch("claudewheel.health.SHARED_SETTINGS_FILE", self._shared_settings_file)
        self._ss_patcher.start()

    def tearDown(self) -> None:
        self._ss_patcher.stop()
        super().tearDown()

    def _canonical_perms(self) -> dict:
        """A permissions block that exactly matches the canonical guardrail model."""
        return {
            "deny": guardrail.canonical_deny_rules(),
            "ask": guardrail.canonical_ask_rules(),
            # A non-conflicting allow that must be left alone.
            "allow": ["Bash(git rm:*)"],
        }

    def _write_settings(self, pdir: Path, permissions: dict) -> None:
        (pdir / "settings.json").write_text(json.dumps({"permissions": permissions}))

    def _write_shared(self, permissions: dict) -> None:
        self._shared_settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings_file.write_text(
            json.dumps({"profileDefaults": {"permissions": permissions}})
        )

    def test_ok_when_everything_matches(self) -> None:
        """OK when profile and profileDefaults both match canonical with no conflicting allows."""
        pdir = self._make_profile("clean")
        self._write_settings(pdir, self._canonical_perms())
        self._write_shared(self._canonical_perms())

        result = check_canonical_permissions_drift()
        self.assertTrue(result.ok)
        self.assertIn("match canonical", result.detail)

    def test_warn_when_deny_entries_missing(self) -> None:
        """WARN naming canonical deny entries a profile is missing."""
        pdir = self._make_profile("missingDeny")
        perms = self._canonical_perms()
        # Drop two canonical deny rules.
        perms["deny"] = [d for d in perms["deny"] if d not in ("Bash(rm:*)", "Bash(git stash:*)")]
        self._write_settings(pdir, perms)

        result = check_canonical_permissions_drift()
        self.assertFalse(result.ok)
        self.assertIn("missingDeny", result.detail)
        self.assertIn("missing", result.detail)
        self.assertIn("Bash(rm:*)", result.detail)
        self.assertIn("Bash(git stash:*)", result.detail)

    def test_warn_when_extra_ask_entries(self) -> None:
        """WARN naming non-canonical ask entries a profile carries."""
        pdir = self._make_profile("extraAsk")
        perms = self._canonical_perms()
        perms["ask"] = perms["ask"] + ["Bash(kill:*)"]
        self._write_settings(pdir, perms)

        result = check_canonical_permissions_drift()
        self.assertFalse(result.ok)
        self.assertIn("extraAsk", result.detail)
        self.assertIn("extra", result.detail)
        self.assertIn("Bash(kill:*)", result.detail)

    def test_warn_when_conflicting_allow(self) -> None:
        """WARN flagging a permissions.allow entry that is a dead/conflicting allow."""
        pdir = self._make_profile("conflictAllow")
        perms = self._canonical_perms()
        perms["allow"] = ["Bash(git rm:*)", "Bash(git stash:*)"]
        self._write_settings(pdir, perms)

        result = check_canonical_permissions_drift()
        self.assertFalse(result.ok)
        self.assertIn("conflictAllow", result.detail)
        self.assertIn("dead/conflicting", result.detail)
        self.assertIn("Bash(git stash:*)", result.detail)

    def test_warn_when_stale_profile_defaults(self) -> None:
        """WARN when shared-settings profileDefaults drifts from canonical."""
        stale = self._canonical_perms()
        stale["deny"] = [d for d in stale["deny"] if d != "Bash(git restore:*)"]
        self._write_shared(stale)

        result = check_canonical_permissions_drift()
        self.assertFalse(result.ok)
        self.assertIn("profileDefaults", result.detail)
        self.assertIn("Bash(git restore:*)", result.detail)

    def test_ok_no_profiles_no_shared(self) -> None:
        """OK when there are no profiles and no shared-settings.json."""
        result = check_canonical_permissions_drift()
        self.assertTrue(result.ok)


# ---------------------------------------------------------------------------
# check_deployed_hook_drift
# ---------------------------------------------------------------------------


class CheckDeployedHookDriftTests(unittest.TestCase):
    """Tests for check_deployed_hook_drift().

    The check byte-compares each deployed hook script under SCRIPTS_DIR against
    the HOOK_SCRIPTS model. Warn-only; absence (no dir / not deployed) is OK.
    """

    #: A small controlled model so tests don't depend on the real script bodies.
    MODEL = {
        "hook-alpha": "#!/usr/bin/env bash\necho alpha\n",
        "hook-beta": "#!/usr/bin/env bash\necho beta\n",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._scripts_dir = Path(self._tmp.name) / "scripts"
        self._patches = [
            patch("claudewheel.health.SCRIPTS_DIR", self._scripts_dir),
            patch("claudewheel.health.HOOK_SCRIPTS", self.MODEL),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _deploy(self, name: str, content: str) -> None:
        self._scripts_dir.mkdir(parents=True, exist_ok=True)
        (self._scripts_dir / name).write_text(content)

    def test_ok_when_deployed_matches_model(self) -> None:
        """OK when every deployed script is byte-identical to the model."""
        for name, content in self.MODEL.items():
            self._deploy(name, content)

        result = check_deployed_hook_drift()
        self.assertTrue(result.ok)
        self.assertEqual(result.label, "hook-drift")
        self.assertIn("2 deployed hook scripts match model", result.detail)

    def test_warn_when_deployed_script_mutated(self) -> None:
        """WARN naming the drifted script when a deployed file differs."""
        self._deploy("hook-alpha", self.MODEL["hook-alpha"])
        # Mutate hook-beta on disk so it no longer matches the model.
        self._deploy("hook-beta", "#!/usr/bin/env bash\necho TAMPERED\n")

        result = check_deployed_hook_drift()
        self.assertFalse(result.ok)
        self.assertIn("hook-beta", result.detail)
        self.assertNotIn("hook-alpha", result.detail)
        self.assertIn("deploy-hooks", result.detail)

    def test_ok_when_scripts_dir_absent(self) -> None:
        """OK (skip) when SCRIPTS_DIR does not exist -- CI / fresh machines."""
        # Do not create the scripts dir at all.
        result = check_deployed_hook_drift()
        self.assertTrue(result.ok)
        self.assertIn("not deployed", result.detail)

    def test_ok_when_script_not_deployed(self) -> None:
        """A model script absent on disk is skipped, not counted as drift."""
        # Only deploy one of the two model scripts.
        self._deploy("hook-alpha", self.MODEL["hook-alpha"])

        result = check_deployed_hook_drift()
        self.assertTrue(result.ok)
        self.assertIn("1 deployed hook scripts match model", result.detail)

    def test_ok_when_dir_exists_but_no_model_scripts(self) -> None:
        """OK with 'none deployed' message when the dir has no model scripts."""
        self._scripts_dir.mkdir(parents=True, exist_ok=True)
        (self._scripts_dir / "unrelated-tool").write_text("x")

        result = check_deployed_hook_drift()
        self.assertTrue(result.ok)
        self.assertIn("no model hook scripts deployed", result.detail)


# ---------------------------------------------------------------------------
# run_health_check -- corrupt tokens.json carve-out
# ---------------------------------------------------------------------------


class HealthRunCorruptTokensTests(_HomeDirTestCase):
    """The single-load carve-out: a corrupt tokens.json fails the token checks
    but the full health run still completes with every check reported."""

    def setUp(self) -> None:
        super().setUp()
        cw = self.home / ".claudewheel"
        cw.mkdir(parents=True, exist_ok=True)
        # Redirect the remaining real-filesystem constants into the temp home so
        # the full run stays hermetic (no reads/writes of the real store).
        extra = [
            patch("claudewheel.health.INODES_FILE", cw / "shared" / "inodes.json"),
            patch("claudewheel.health.SHARED_SETTINGS_FILE", cw / "shared-settings.json"),
            patch("claudewheel.health.OPTIONS_FILE", cw / "options.json"),
            patch("claudewheel.health.SCRIPTS_DIR", cw / "scripts"),
        ]
        for p in extra:
            p.start()
            self.addCleanup(p.stop)

    def test_corrupt_tokens_fails_token_checks_but_run_completes(self) -> None:
        self._make_profile("work")
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text("not valid json{{{")

        results = run_health_check()

        # Every check is reported -- nothing crashed or was skipped.
        self.assertEqual(len(results), 15)
        labels = [r.label for r in results]

        # Both token checks failed with the actionable exception message.
        tokens_result = next(r for r in results if r.label == "tokens")
        self.assertFalse(tokens_result.ok)
        self.assertIn("corrupt", tokens_result.detail)
        self.assertIn("retry", tokens_result.detail)

        expiry_result = next(r for r in results if r.label == "token-expiry")
        self.assertFalse(expiry_result.ok)
        self.assertIn("corrupt", expiry_result.detail)

        # Profile-based checks still ran (dir-only enumeration, has_token False).
        self.assertIn("hooks-wired", labels)
        self.assertIn("orphan-profiles", labels)

    def test_standalone_check_tokens_fails_on_corrupt_file(self) -> None:
        """Called directly (no run-level token view), check_tokens still fails."""
        self._make_profile("work")
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text("{not json")

        result = check_tokens()
        self.assertFalse(result.ok)
        self.assertIn("corrupt", result.detail)


# ---------------------------------------------------------------------------
# check_relocated_hook_paths
# ---------------------------------------------------------------------------


class CheckRelocatedHookPathsTests(_HomeDirTestCase):
    """Tests for check_relocated_hook_paths() -- the relocation blind-spot check."""

    def setUp(self) -> None:
        super().setUp()
        self._scripts_dir = self.home / ".claudewheel" / "scripts"
        self._shared_settings = self.home / ".claudewheel" / "shared-settings.json"
        extra = [
            patch("claudewheel.health.SCRIPTS_DIR", self._scripts_dir),
            patch("claudewheel.health.SHARED_SETTINGS_FILE", self._shared_settings),
        ]
        for p in extra:
            p.start()
            self.addCleanup(p.stop)

    def _write_settings(self, pdir: Path, settings: dict) -> None:
        (pdir / "settings.json").write_text(json.dumps(settings))

    def _timestamp_hooks(self, scripts_dir) -> dict:
        return {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [
                    {"type": "command",
                     "command": str(Path(scripts_dir) / "hook-timestamp")},
                ]},
            ],
        }

    def test_passes_when_commands_under_current_scripts_dir(self) -> None:
        pdir = self._make_profile("current")
        self._write_settings(pdir, {"hooks": self._timestamp_hooks(self._scripts_dir)})

        result = check_relocated_hook_paths()
        self.assertTrue(result.ok)
        self.assertIn("current scripts dir", result.detail)

    def test_fails_naming_profile_with_stale_root(self) -> None:
        pdir = self._make_profile("relocated")
        self._write_settings(
            pdir, {"hooks": self._timestamp_hooks("/old/home/.claudewheel/scripts")}
        )

        result = check_relocated_hook_paths()
        self.assertFalse(result.ok)
        self.assertIn("relocated", result.detail)
        self.assertIn("/old/home/.claudewheel/scripts/hook-timestamp", result.detail)
        self.assertIn("patch-profiles", result.detail)

    def test_profile_without_hooks_passes(self) -> None:
        pdir = self._make_profile("nohooks")
        self._write_settings(pdir, {"permissions": {}})

        result = check_relocated_hook_paths()
        self.assertTrue(result.ok)

    def test_user_custom_hook_under_other_dir_passes(self) -> None:
        """A non-claudewheel hook command under any dir is ignored (not flagged)."""
        pdir = self._make_profile("custom")
        settings = {"hooks": {"UserPromptSubmit": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": str(self._scripts_dir / "hook-timestamp")},
                {"type": "command", "command": "/opt/mine/my-own-hook"},
            ]},
        ]}}
        self._write_settings(pdir, settings)

        result = check_relocated_hook_paths()
        self.assertTrue(result.ok)

    def test_shared_settings_stale_root_flagged(self) -> None:
        self._shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings.write_text(
            json.dumps({"hooks": self._timestamp_hooks("/stale/scripts")})
        )

        result = check_relocated_hook_paths()
        self.assertFalse(result.ok)
        self.assertIn("shared-settings.json", result.detail)
        self.assertIn("/stale/scripts/hook-timestamp", result.detail)


if __name__ == "__main__":
    unittest.main()
