"""Tests for health check functions in claudewheel.health."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.defaults import DISALLOWED_TOOLS
from claudewheel.health import (
    _discover_profiles,
    check_auth_shadow,
    check_hooks_wired,
    check_orphan_profiles,
    check_settings_defaults,
    check_shared_symlinks,
    check_tokens,
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
        self._dir_patches = [
            patch("claudewheel.health.SKILLS_DIR", self._skills_dir),
            patch("claudewheel.health.PROFILES_DIR", self._profiles_dir),
            patch("claudewheel.discovery.SHARED_DIR", self._shared_dir),
            patch("claudewheel.discovery.SKILLS_DIR", self._skills_dir),
            patch("claudewheel.discovery.PROFILES_DIR", self._profiles_dir),
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
        """Return settings with all required hooks present."""
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
            }
        }

    def test_ok_when_all_hooks_present(self) -> None:
        """Returns OK when all required hooks are present."""
        pdir = self._make_profile("hooked")
        self._write_settings(pdir, self._good_settings())

        result = check_hooks_wired()
        self.assertTrue(result.ok)
        self.assertIn("1 profiles OK", result.detail)

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
        self.assertIn("missing hook-timestamp", result.detail)

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
        self.assertIn("missing PreToolUse hook-block-unsafe-commands", result.detail)

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
        return {
            "awaySummaryEnabled": False,
            "cleanupPeriodDays": 365,
            "autoMemoryEnabled": False,
            "permissions": {
                "deny": ["a", "b", "c", "d", "e"],
                "ask": ["a", "b", "c", "d"],
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

    def tearDown(self) -> None:
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
        """Returns OK when tokens.json does not exist."""
        self._make_profile("any")
        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no tokens.json", result.detail)

    def test_no_profiles(self) -> None:
        """Returns OK when no profiles are discovered."""
        self._tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self._tokens_file.write_text("{}")
        result = check_auth_shadow()
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


if __name__ == "__main__":
    unittest.main()
