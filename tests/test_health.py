"""Tests for health check functions in claudewheel.health."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
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
    check_shared_settings_drift,
    check_shared_symlinks,
    check_tmp_claude_size,
    check_token_expiry,
    check_tokens,
    run_health_check,
)
from claudewheel.profile_data import PROFILE_DATA_DIRNAME
from claudewheel.tokens import EXPIRY_UNKNOWN_FIELD
from tests.wheelhelpers import build_profile_dir, write_token_entry


class _HomeDirTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as Path.home() and patches it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher = patch.object(
            Path, "home", autospec=True, return_value=self.home
        )
        self._patcher.start()
        # Patch module-level path constants that were computed at import time
        self._shared_dir = self.home / ".claudewheel" / "shared"
        self._skills_dir = self.home / ".claudewheel" / "skills"
        self._profiles_dir = self.home / ".claudewheel" / "profiles"
        # Health now takes an explicit workspace; build one rooted at the sandbox.
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )
        self._dir_patches: list[Any] = []

    def tearDown(self) -> None:
        for p in self._dir_patches:
            p.stop()
        self._patcher.stop()
        self._tmp.cleanup()

    def _make_profile(self, name: str) -> Path:
        """Create a profile dir with .credentials.json and return its path."""
        return build_profile_dir(
            self._profiles_dir,
            name,
            parents=True,
            exist_ok=True,
            credentials=True,
        )

    def _write_token(self, name: str, entry: dict[str, Any] | str) -> Path:
        """Store *name*'s token entry inside its own profile directory."""
        pdir = self._profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        return write_token_entry(pdir, entry)


# ---------------------------------------------------------------------------
# _discover_profiles
# ---------------------------------------------------------------------------


class DiscoverProfilesTests(_HomeDirTestCase):
    """Tests for _discover_profiles(self.ws)."""

    def test_finds_dirs_with_credentials(self) -> None:
        """Profiles with .credentials.json are discovered."""
        self._make_profile("alpha")
        self._make_profile("beta")
        result = _discover_profiles(self.ws)
        names = [p.name for p in result]
        self.assertEqual(names, ["alpha", "beta"])

    def test_ignores_dirs_without_any_marker(self) -> None:
        """Profile dirs without .credentials.json or settings.json are skipped."""
        # Has credentials
        self._make_profile("real")
        # Missing both markers
        fake = self._profiles_dir / "fake"
        fake.mkdir(parents=True, exist_ok=True)
        result = _discover_profiles(self.ws)
        names = [p.name for p in result]
        self.assertEqual(names, ["real"])

    def test_returns_sorted_list(self) -> None:
        """Profiles are returned sorted by directory name."""
        self._make_profile("zeta")
        self._make_profile("alpha")
        self._make_profile("mid")
        result = _discover_profiles(self.ws)
        names = [p.name for p in result]
        self.assertEqual(names, ["alpha", "mid", "zeta"])

    def test_finds_data_backed_profile_without_credentials(self) -> None:
        """A profile dir carrying only claudewheel's data dir is discovered."""
        (self._profiles_dir / "work" / PROFILE_DATA_DIRNAME).mkdir(
            parents=True, exist_ok=True
        )
        result = _discover_profiles(self.ws)
        names = [p.name for p in result]
        self.assertIn("work", names)

    def test_data_profile_merged_and_sorted(self) -> None:
        """Data-dir-backed profiles merge with credential-based ones and sort."""
        self._make_profile("beta")
        (self._profiles_dir / "alpha" / PROFILE_DATA_DIRNAME).mkdir(
            parents=True, exist_ok=True
        )
        result = _discover_profiles(self.ws)
        names = [p.name for p in result]
        self.assertEqual(names, ["alpha", "beta"])

    def test_returns_empty_when_no_profiles(self) -> None:
        """Returns empty list when no .claude-* dirs exist."""
        result = _discover_profiles(self.ws)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# check_shared_symlinks
# ---------------------------------------------------------------------------


class CheckSharedSymlinksTests(_HomeDirTestCase):
    """Tests for check_shared_symlinks(self.ws)."""

    EXPECTED_DIRS = [
        "projects",
        "session-env",
        "file-history",
        "tasks",
        "todos",
        "paste-cache",
    ]

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

        result = check_shared_symlinks(self.ws)
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

        result = check_shared_symlinks(self.ws)
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

        result = check_shared_symlinks(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("nolink/todos", result.detail)

    def test_ok_no_profiles(self) -> None:
        """Returns OK with detail message when no profiles exist."""
        result = check_shared_symlinks(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


# ---------------------------------------------------------------------------
# check_hooks_wired
# ---------------------------------------------------------------------------


class CheckHooksWiredTests(_HomeDirTestCase):
    """Tests for check_hooks_wired(self.ws)."""

    def _write_settings(self, pdir: Path, settings: dict[str, Any]) -> None:
        """Write settings.json into a profile directory."""
        (pdir / "settings.json").write_text(json.dumps(settings))

    def _good_settings(self) -> dict[str, Any]:
        """Return settings with all four canonical hook wirings present.

        Commands are rooted at the workspace's current scripts dir because
        hooks-wired now requires the exact canonical command, not a substring.
        """
        return self._settings_under(self.ws.scripts_dir)

    def _three_hook_settings(self) -> dict[str, Any]:
        """Return settings with only the three old hooks (no PostToolUse advise)."""
        settings = self._good_settings()
        del settings["hooks"]["PostToolUse"]
        return settings

    def test_ok_when_all_hooks_present(self) -> None:
        """Returns OK when all four canonical hook wirings are present."""
        pdir = self._make_profile("hooked")
        self._write_settings(pdir, self._good_settings())

        result = check_hooks_wired(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("1 profiles OK", result.detail)

    def test_warn_when_only_three_old_hooks(self) -> None:
        """A profile with only the three old hooks fails, missing PostToolUse advise."""
        pdir = self._make_profile("three-only")
        self._write_settings(pdir, self._three_hook_settings())

        result = check_hooks_wired(self.ws)
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

        result = check_hooks_wired(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("hook-timestamp", result.detail)

    def test_warn_when_block_unsafe_commands_missing(self) -> None:
        """Returns WARN when hook-block-unsafe-commands is missing from PreToolUse."""
        pdir = self._make_profile("no-bash-hook")
        sd = self.ws.scripts_dir
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {"command": str(sd / "hook-timestamp")},
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Agent",
                        "hooks": [
                            {"command": str(sd / "hook-block-worktree")},
                        ],
                    }
                ],
            }
        }
        self._write_settings(pdir, settings)

        result = check_hooks_wired(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("hook-block-unsafe-commands", result.detail)

    def test_warn_when_no_settings_json(self) -> None:
        """Returns WARN when settings.json does not exist."""
        self._make_profile("bare")

        result = check_hooks_wired(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("no settings.json", result.detail)

    def test_ok_no_profiles(self) -> None:
        """Returns OK when no profiles exist."""
        result = check_hooks_wired(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)

    def _settings_under(self, scripts_dir: str | Path) -> dict[str, Any]:
        """Build all four canonical wirings with commands rooted at *scripts_dir*."""
        scripts_dir = Path(scripts_dir)
        return {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"command": str(scripts_dir / "hook-timestamp")},
                        ],
                    },
                ],
                "PreToolUse": [
                    {
                        "matcher": "Agent",
                        "hooks": [
                            {"command": str(scripts_dir / "hook-block-worktree")},
                        ],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "command": str(
                                    scripts_dir / "hook-block-unsafe-commands"
                                )
                            },
                        ],
                    },
                ],
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"command": str(scripts_dir / "hook-advise-commands")},
                        ],
                    },
                ],
            }
        }

    def test_warn_when_hooks_under_wrong_dir(self) -> None:
        """Right basenames under the WRONG scripts dir must FAIL hooks-wired.

        The substring matcher used to pass here even though the hooks pointed at
        a directory that is not the current scripts dir. The exact-command
        matcher rejects them.
        """
        pdir = self._make_profile("wrong-dir")
        self._write_settings(pdir, self._settings_under("/nonexistent/dead/scripts"))

        result = check_hooks_wired(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("hook-timestamp", result.detail)

    def test_damaged_hooks_fail_then_pass_after_repair(self) -> None:
        """Phase-1-style damage: hooks pointing at a dead dir FAIL pre-repair,
        and PASS once repathed to the current scripts dir (post-repair)."""
        pdir = self._make_profile("damaged")

        # Pre-repair: every hook points at a dead /tmp directory.
        self._write_settings(pdir, self._settings_under("/tmp/dead-scripts-xyz"))
        pre = check_hooks_wired(self.ws)
        self.assertFalse(pre.ok)

        # Post-repair: repathed to the workspace's current scripts dir.
        self._write_settings(pdir, self._settings_under(self.ws.scripts_dir))
        post = check_hooks_wired(self.ws)
        self.assertTrue(post.ok)
        self.assertIn("1 profiles OK", post.detail)


# ---------------------------------------------------------------------------
# check_settings_defaults
# ---------------------------------------------------------------------------


class CheckSettingsDefaultsTests(_HomeDirTestCase):
    """Tests for check_settings_defaults(self.ws)."""

    def _write_settings(self, pdir: Path, settings: dict[str, Any]) -> None:
        (pdir / "settings.json").write_text(json.dumps(settings))

    def _good_settings(self) -> dict[str, Any]:
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

        result = check_settings_defaults(self.ws)
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

        result = check_settings_defaults(self.ws)
        self.assertTrue(result.ok)

    def test_warn_when_auto_mode_not_disabled(self) -> None:
        """disableAutoMode is still enforced after the threshold removal."""
        pdir = self._make_profile("autoOn")
        settings = self._good_settings()
        settings["permissions"] = {"deny": [], "ask": []}
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("auto mode not disabled", result.detail)

    def test_warn_when_away_summary_enabled(self) -> None:
        """Returns WARN when awaySummaryEnabled is not false."""
        pdir = self._make_profile("awayOn")
        settings = self._good_settings()
        settings["awaySummaryEnabled"] = True
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("awaySummaryEnabled != false", result.detail)

    def test_warn_when_cleanup_period_too_low(self) -> None:
        """Returns WARN when cleanupPeriodDays < 365."""
        pdir = self._make_profile("lowCleanup")
        settings = self._good_settings()
        settings["cleanupPeriodDays"] = 30
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("cleanupPeriodDays < 365", result.detail)

    def test_warn_when_cleanup_period_missing(self) -> None:
        """Returns WARN when cleanupPeriodDays is absent."""
        pdir = self._make_profile("noCleanup")
        settings = self._good_settings()
        del settings["cleanupPeriodDays"]
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("cleanupPeriodDays < 365", result.detail)

    def test_warn_when_auto_memory_enabled(self) -> None:
        """Returns WARN when autoMemoryEnabled is not false."""
        pdir = self._make_profile("memOn")
        settings = self._good_settings()
        settings["autoMemoryEnabled"] = True
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("autoMemoryEnabled != false", result.detail)

    def test_warn_when_disallowed_tools_missing(self) -> None:
        """Returns WARN when claudewheel.disallowedTools is absent."""
        pdir = self._make_profile("noCw")
        settings = self._good_settings()
        del settings["claudewheel"]
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("missing disallowedTools", result.detail)

    def test_warn_when_stale_top_level_disallowed_tools(self) -> None:
        """Returns WARN when the old top-level disallowedTools key is still present."""
        pdir = self._make_profile("staleKey")
        settings = self._good_settings()
        # Add the old inert top-level key alongside the correct nested one
        settings["disallowedTools"] = DISALLOWED_TOOLS[:]
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("inert top-level disallowedTools", result.detail)

    def test_ok_when_disallowed_tools_in_claudewheel_namespace(self) -> None:
        """Returns OK when disallowedTools lives under claudewheel with no top-level key."""
        pdir = self._make_profile("goodNs")
        settings = self._good_settings()
        # Ensure no top-level key exists
        settings.pop("disallowedTools", None)
        self._write_settings(pdir, settings)

        result = check_settings_defaults(self.ws)
        self.assertTrue(result.ok)

    def test_ok_no_profiles(self) -> None:
        """Returns OK when no profiles exist."""
        result = check_settings_defaults(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)


# ---------------------------------------------------------------------------
# check_tokens
# ---------------------------------------------------------------------------


class CheckTokenExpiryTests(_HomeDirTestCase):
    """Tests for check_token_expiry(self.ws), focused on the unknown case."""

    def test_unknown_expiry_never_flagged_as_expiring(self) -> None:
        """An externally-issued token (unknown expiry) is never expiring-soon."""
        self._write_token(
            "pasted",
            {
                "token": "sk-ant-x",
                "created": "2020-01-01",
                EXPIRY_UNKNOWN_FIELD: True,
            },
        )
        result = check_token_expiry(self.ws)
        self.assertTrue(result.ok)
        self.assertNotIn("pasted", result.detail)

    def test_near_expiry_ttl_token_is_flagged(self) -> None:
        """A genuine TTL token close to expiry is still flagged."""
        from datetime import date, timedelta

        soon = (date.today() + timedelta(days=5)).isoformat()
        self._write_token(
            "scraped",
            {"token": "sk-ant-y", "created": "2020-01-01", "expires_at": soon},
        )
        result = check_token_expiry(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("scraped", result.detail)

    def test_unknown_alongside_expiring_reports_only_expiring(self) -> None:
        """A mix: the unknown token is skipped, the expiring TTL token flagged."""
        from datetime import date, timedelta

        soon = (date.today() + timedelta(days=3)).isoformat()
        self._write_token("pasted", {"token": "sk-ant-x", EXPIRY_UNKNOWN_FIELD: True})
        self._write_token("scraped", {"token": "sk-ant-y", "expires_at": soon})
        result = check_token_expiry(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("scraped", result.detail)
        self.assertNotIn("pasted", result.detail)


class CheckTokensTests(_HomeDirTestCase):
    """Tests for check_tokens(self.ws)."""

    def test_ok_when_all_profiles_have_tokens(self) -> None:
        """Returns OK when every profile has a matching token entry."""
        self._make_profile("alpha")
        self._make_profile("beta")
        self._write_token("alpha", {"token": "tok-aaa"})
        self._write_token("beta", {"token": "tok-bbb"})

        result = check_tokens(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("2 profiles OK", result.detail)

    def test_warn_when_profile_stores_no_token(self) -> None:
        """Returns WARN when a credentialed profile stores no token entry."""
        self._make_profile("alpha")
        self._make_profile("beta")
        self._write_token("alpha", {"token": "tok-aaa"})

        result = check_tokens(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("beta", result.detail)

    def test_warn_when_no_profile_stores_a_token(self) -> None:
        """A credentialed profile with nothing stored is reported, not excused."""
        self._make_profile("lonely")

        result = check_tokens(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("lonely", result.detail)

    def test_ok_no_profiles(self) -> None:
        """Returns OK when there are no profiles at all."""
        result = check_tokens(self.ws)
        self.assertTrue(result.ok)

    def test_unreadable_entry_fails_naming_the_profile(self) -> None:
        """A corrupt entry fails the check and names the profile it belongs to."""
        self._make_profile("broken")
        self._write_token("broken", {"token": "t"}).write_text("{not json")

        result = check_tokens(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("broken", result.detail)
        self.assertIn("corrupt", result.detail)

    def test_warn_when_token_value_empty(self) -> None:
        """Returns WARN when a profile's token value is empty string."""
        self._make_profile("empty")
        self._write_token("empty", {"token": ""})

        result = check_tokens(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("empty", result.detail)

    def test_warn_when_token_value_not_string(self) -> None:
        """Returns WARN when a profile's token value is not a string."""
        self._make_profile("numeric")
        self._write_token("numeric", {"token": 12345})

        result = check_tokens(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("numeric", result.detail)

    def test_no_warn_for_settings_only_profile(self) -> None:
        """Settings-only profiles (no credentials, no token) don't trigger warnings."""
        # Create a profile with only settings.json (no .credentials.json)
        pdir = self._profiles_dir / "newprof"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "settings.json").write_text("{}")

        result = check_tokens(self.ws)
        self.assertTrue(result.ok)
        self.assertNotIn("newprof", result.detail)


# ---------------------------------------------------------------------------
# check_orphan_profiles
# ---------------------------------------------------------------------------


class CheckOrphanProfilesTests(_HomeDirTestCase):
    """Tests for check_orphan_profiles(self.ws)."""

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
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no orphan", result.detail)

    def test_warns_on_orphan_dir(self) -> None:
        """Returns WARN when a profile dir is not registered anywhere."""
        self._make_profile("known")
        # Create an orphan dir (no .credentials.json, not in options)
        orphan = self._profiles_dir / "stale"
        orphan.mkdir(parents=True, exist_ok=True)
        self._write_options(["known"])
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("stale", result.detail)

    def test_dir_in_options_not_orphan(self) -> None:
        """A profile dir listed in options.json (without .credentials.json) is not orphan."""
        # Dir exists but has no .credentials.json
        (self._profiles_dir / "pending").mkdir(parents=True, exist_ok=True)
        self._write_options(["pending"])
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertTrue(result.ok)

    def test_flags_broken_symlinks(self) -> None:
        """Orphan dirs with broken symlinks are flagged in the detail."""
        orphan = self._profiles_dir / "broken"
        orphan.mkdir(parents=True, exist_ok=True)
        # Create a broken symlink inside
        (orphan / "projects").symlink_to(self.home / "nonexistent")
        self._write_options([])
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("broken", result.detail.lower())
        self.assertIn("projects", result.detail)

    def test_registered_profile_not_orphan(self) -> None:
        """A dir with .credentials.json (registered profile) is never orphan."""
        self._make_profile("real")
        self._write_options([])  # not in options, but has credentials
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertTrue(result.ok)

    def test_dir_with_claudewheel_data_not_orphan(self) -> None:
        """A profile dir carrying claudewheel's data dir is discovered, not orphan."""
        (self._profiles_dir / "work" / PROFILE_DATA_DIRNAME).mkdir(
            parents=True, exist_ok=True
        )
        self._write_options([])
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertTrue(result.ok)

    def test_ok_when_no_profiles_dir(self) -> None:
        """Returns OK when ~/.claudewheel/profiles/ does not exist."""
        # Don't create profiles dir
        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
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

        with patch.object(health, "print_health_report", health.print_health_report):
            result = check_orphan_profiles(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no orphan", result.detail)


# ---------------------------------------------------------------------------
# check_file_permissions
# ---------------------------------------------------------------------------


class CheckFilePermissionsTests(_HomeDirTestCase):
    """Tests for check_file_permissions(self.ws) -- the 0700/0600 invariant.

    Every profile's ``.credentials.json`` and claudewheel token entry must be
    0600 and the data directory holding the entry 0700, so the check has to fail
    on each of those three modes individually and name the offending profile.
    """

    def _locked_profile(self, name: str) -> Path:
        """A profile at the modes the production writers produce."""
        pdir = self._make_profile(name)
        (pdir / ".credentials.json").chmod(0o600)
        return pdir

    def test_ok_when_everything_is_locked_down(self) -> None:
        """Credentials 0600, data dir 0700, token file 0600 -> pass."""
        self._locked_profile("alpha")
        self._write_token("alpha", {"token": "tok-aaa"})
        self._locked_profile("beta")
        self._write_token("beta", {"token": "tok-bbb"})

        result = health.check_file_permissions(self.ws)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("locked down", result.detail)

    def test_ok_when_no_profiles(self) -> None:
        """Nothing discovered -> nothing to complain about."""
        result = health.check_file_permissions(self.ws)
        self.assertTrue(result.ok, result.detail)

    def test_world_readable_token_file_fails_naming_the_profile(self) -> None:
        self._locked_profile("alpha")
        token_file = self._write_token("alpha", {"token": "tok-aaa"})
        token_file.chmod(0o644)

        result = health.check_file_permissions(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("alpha", result.detail)
        self.assertIn("token.json", result.detail)
        self.assertIn("0o644", result.detail)

    def test_world_readable_data_dir_fails_naming_the_profile(self) -> None:
        self._locked_profile("alpha")
        self._write_token("alpha", {"token": "tok-aaa"})
        (self._profiles_dir / "alpha" / PROFILE_DATA_DIRNAME).chmod(0o755)

        result = health.check_file_permissions(self.ws)
        self.assertFalse(result.ok)
        self.assertIn(f"alpha/{PROFILE_DATA_DIRNAME}", result.detail)
        self.assertIn("0o755", result.detail)

    def test_world_readable_credentials_fails_naming_the_profile(self) -> None:
        pdir = self._locked_profile("alpha")
        self._write_token("alpha", {"token": "tok-aaa"})
        (pdir / ".credentials.json").chmod(0o644)

        result = health.check_file_permissions(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("alpha/.credentials.json", result.detail)
        self.assertIn("0o644", result.detail)

    def test_every_offender_is_reported_not_just_the_first(self) -> None:
        """Two profiles, three kinds of drift -> all of them named."""
        good = self._locked_profile("alpha")
        (good / ".credentials.json").chmod(0o644)
        bad = self._locked_profile("beta")
        token_file = self._write_token("beta", {"token": "t"})
        token_file.chmod(0o640)
        (bad / PROFILE_DATA_DIRNAME).chmod(0o750)

        result = health.check_file_permissions(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("alpha/.credentials.json", result.detail)
        self.assertIn(f"beta/{PROFILE_DATA_DIRNAME}/token.json", result.detail)
        self.assertIn(f"beta/{PROFILE_DATA_DIRNAME} is 0o750", result.detail)

    def test_profile_without_credentials_or_data_passes(self) -> None:
        """A settings-only profile has neither file: absence is not drift."""
        pdir = self._profiles_dir / "fresh"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "settings.json").write_text("{}")

        result = health.check_file_permissions(self.ws)
        self.assertTrue(result.ok, result.detail)

    def test_data_dir_without_a_token_file_is_checked_alone(self) -> None:
        """The dir mode is enforced even when no entry has been written yet."""
        self._locked_profile("alpha")
        data_dir = self._profiles_dir / "alpha" / PROFILE_DATA_DIRNAME
        data_dir.mkdir(parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        self.assertTrue(health.check_file_permissions(self.ws).ok)

        data_dir.chmod(0o777)
        result = health.check_file_permissions(self.ws)
        self.assertFalse(result.ok)
        self.assertIn(f"alpha/{PROFILE_DATA_DIRNAME} is 0o777", result.detail)

    def test_corrupt_token_entry_does_not_break_the_permission_check(self) -> None:
        """Permissions are a stat question -- an unparseable entry is irrelevant."""
        self._locked_profile("alpha")
        token_file = self._write_token("alpha", {"token": "t"})
        token_file.write_text("{not json")
        token_file.chmod(0o600)

        result = health.check_file_permissions(self.ws)
        self.assertTrue(result.ok, result.detail)


# ---------------------------------------------------------------------------
# check_auth_shadow
# ---------------------------------------------------------------------------


class CheckAuthShadowTests(_HomeDirTestCase):
    """Tests for check_auth_shadow(self.ws)."""

    def _write_credentials(self, pdir: Path, data: dict[str, Any]) -> None:
        (pdir / ".credentials.json").write_text(json.dumps(data))

    def test_flagged_when_both_token_and_claude_ai_oauth(self) -> None:
        """Profile with both tokens.json entry AND claudeAiOauth in .credentials.json is flagged."""
        pdir = self._make_profile("work")
        self._write_token(
            "work",
            {"token": "tok-xxx", "created": "2025-01-01", "expires_at": "2026-01-01"},
        )
        self._write_credentials(pdir, {"claudeAiOauth": {"accessToken": "short-lived"}})

        result = check_auth_shadow(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("work", result.detail)
        self.assertIn("shadowed", result.detail)

    def test_not_flagged_when_only_token(self) -> None:
        """Profile with only tokens.json entry (no claudeAiOauth) is not flagged."""
        pdir = self._make_profile("clean")
        self._write_token("clean", {"token": "tok-abc"})
        self._write_credentials(pdir, {"mcpOAuth": {"some": "data"}})

        result = check_auth_shadow(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_not_flagged_when_only_credentials(self) -> None:
        """Profile with claudeAiOauth but no tokens.json entry is not flagged."""
        pdir = self._make_profile("session-only")  # stores no token entry
        self._write_credentials(pdir, {"claudeAiOauth": {"accessToken": "x"}})

        result = check_auth_shadow(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_not_flagged_when_mcp_oauth_only(self) -> None:
        """Profile with mcpOAuth but no claudeAiOauth is not flagged."""
        pdir = self._make_profile("mcp-only")
        self._write_token("mcp-only", {"token": "tok-mcp"})
        self._write_credentials(pdir, {"mcpOAuth": {"provider": "github"}})

        result = check_auth_shadow(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_no_stored_tokens(self) -> None:
        """Returns OK when no profile stores a token (no shadow possible)."""
        self._make_profile("any")
        result = check_auth_shadow(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no auth shadow", result.detail)

    def test_no_profiles(self) -> None:
        """Returns OK when no profiles are discovered."""
        result = check_auth_shadow(self.ws)
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

        with patch.object(
            health, "_tmp_claude_dir", autospec=True, return_value=self.tmp_dir
        ):
            result = check_tmp_claude_size()
        self.assertTrue(result.ok)
        self.assertEqual(result.label, "/tmp/claude")
        # Well under 1 MB of real usage -> reported as ~0 MB.
        self.assertIn("MB", result.detail)

    def test_not_present(self) -> None:
        """Returns OK 'not present' when the dir does not exist."""
        missing = Path(self._tmp.name) / "does-not-exist"
        with patch.object(
            health, "_tmp_claude_dir", autospec=True, return_value=missing
        ):
            result = check_tmp_claude_size()
        self.assertTrue(result.ok)
        self.assertIn("not present", result.detail)

    def test_threshold_warns_above_1gb(self) -> None:
        """Usage above 1024 MB warns with the '>1 GB threshold' message."""
        over = 1025 * 1024 * 1024
        with (
            patch.object(
                health, "_tmp_claude_dir", autospec=True, return_value=self.tmp_dir
            ),
            patch.object(health, "_real_disk_usage", autospec=True, return_value=over),
        ):
            result = check_tmp_claude_size()
        self.assertFalse(result.ok)
        self.assertIn("1 GB threshold", result.detail)

    def test_threshold_ok_at_1gb_boundary(self) -> None:
        """Usage of exactly 1024 MB is OK (boundary is inclusive)."""
        at = 1024 * 1024 * 1024
        with (
            patch.object(
                health, "_tmp_claude_dir", autospec=True, return_value=self.tmp_dir
            ),
            patch.object(health, "_real_disk_usage", autospec=True, return_value=at),
        ):
            result = check_tmp_claude_size()
        self.assertTrue(result.ok)
        self.assertNotIn("threshold", result.detail)


# ---------------------------------------------------------------------------
# check_canonical_permissions_drift
# ---------------------------------------------------------------------------


class CheckCanonicalPermissionsDriftTests(_HomeDirTestCase):
    """Tests for check_canonical_permissions_drift(self.ws)."""

    def setUp(self) -> None:
        super().setUp()
        # The check reads the module-level SHARED_SETTINGS_FILE constant, so
        # redirect it into the temp home.
        self._shared_settings_file = self.home / ".claudewheel" / "shared-settings.json"

    def _canonical_perms(self) -> dict[str, Any]:
        """A permissions block that exactly matches the canonical guardrail model."""
        return {
            "deny": guardrail.canonical_deny_rules(),
            "ask": guardrail.canonical_ask_rules(),
            # A non-conflicting allow that must be left alone.
            "allow": ["Bash(git rm:*)"],
        }

    def _write_settings(self, pdir: Path, permissions: dict[str, Any]) -> None:
        (pdir / "settings.json").write_text(json.dumps({"permissions": permissions}))

    def _write_shared(self, permissions: dict[str, Any]) -> None:
        self._shared_settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings_file.write_text(
            json.dumps({"profileDefaults": {"permissions": permissions}})
        )

    def test_ok_when_everything_matches(self) -> None:
        """OK when profile and profileDefaults both match canonical with no conflicting allows."""
        pdir = self._make_profile("clean")
        self._write_settings(pdir, self._canonical_perms())
        self._write_shared(self._canonical_perms())

        result = check_canonical_permissions_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("match canonical", result.detail)

    def test_warn_when_deny_entries_missing(self) -> None:
        """WARN naming canonical deny entries a profile is missing."""
        pdir = self._make_profile("missingDeny")
        perms = self._canonical_perms()
        # Drop two canonical deny rules.
        perms["deny"] = [
            d for d in perms["deny"] if d not in ("Bash(rm:*)", "Bash(git stash:*)")
        ]
        self._write_settings(pdir, perms)

        result = check_canonical_permissions_drift(self.ws)
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

        result = check_canonical_permissions_drift(self.ws)
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

        result = check_canonical_permissions_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("conflictAllow", result.detail)
        self.assertIn("dead/conflicting", result.detail)
        self.assertIn("Bash(git stash:*)", result.detail)

    def test_warn_when_stale_profile_defaults(self) -> None:
        """WARN when shared-settings profileDefaults drifts from canonical."""
        stale = self._canonical_perms()
        stale["deny"] = [d for d in stale["deny"] if d != "Bash(git restore:*)"]
        self._write_shared(stale)

        result = check_canonical_permissions_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("profileDefaults", result.detail)
        self.assertIn("Bash(git restore:*)", result.detail)

    def test_ok_no_profiles_no_shared(self) -> None:
        """OK when there are no profiles and no shared-settings.json."""
        result = check_canonical_permissions_drift(self.ws)
        self.assertTrue(result.ok)


# ---------------------------------------------------------------------------
# check_deployed_hook_drift
# ---------------------------------------------------------------------------


class CheckDeployedHookDriftTests(unittest.TestCase):
    """Tests for check_deployed_hook_drift(self.ws).

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
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            Path(self._tmp.name), claude_dir=Path(self._tmp.name) / ".claude"
        )
        self._scripts_dir = self.ws.scripts_dir
        self._patches = [
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

        result = check_deployed_hook_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertEqual(result.label, "hook-drift")
        self.assertIn("2 deployed hook scripts match model", result.detail)

    def test_warn_when_deployed_script_mutated(self) -> None:
        """WARN naming the drifted script when a deployed file differs."""
        self._deploy("hook-alpha", self.MODEL["hook-alpha"])
        # Mutate hook-beta on disk so it no longer matches the model.
        self._deploy("hook-beta", "#!/usr/bin/env bash\necho TAMPERED\n")

        result = check_deployed_hook_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("hook-beta", result.detail)
        self.assertNotIn("hook-alpha", result.detail)
        self.assertIn("deploy-hooks", result.detail)

    def test_ok_when_scripts_dir_absent(self) -> None:
        """OK (skip) when SCRIPTS_DIR does not exist -- CI / fresh machines."""
        # Do not create the scripts dir at all.
        result = check_deployed_hook_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("not deployed", result.detail)

    def test_ok_when_script_not_deployed(self) -> None:
        """A model script absent on disk is skipped, not counted as drift."""
        # Only deploy one of the two model scripts.
        self._deploy("hook-alpha", self.MODEL["hook-alpha"])

        result = check_deployed_hook_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("1 deployed hook scripts match model", result.detail)

    def test_ok_when_dir_exists_but_no_model_scripts(self) -> None:
        """OK with 'none deployed' message when the dir has no model scripts."""
        self._scripts_dir.mkdir(parents=True, exist_ok=True)
        (self._scripts_dir / "unrelated-tool").write_text("x")

        result = check_deployed_hook_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no model hook scripts deployed", result.detail)


# ---------------------------------------------------------------------------
# run_health_check -- an unreadable token entry
# ---------------------------------------------------------------------------


class HealthRunCorruptTokensTests(_HomeDirTestCase):
    """One profile's unreadable token entry fails the token checks while the
    full health run still completes with every check reported."""

    def test_corrupt_entry_fails_token_checks_but_run_completes(self) -> None:
        self._make_profile("work")
        self._write_token("work", {"token": "t"}).write_text("not valid json{{{")

        results = run_health_check(self.ws)

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

        # Profile-based checks still ran (enumeration tolerated the bad entry).
        self.assertIn("hooks-wired", labels)
        self.assertIn("orphan-profiles", labels)

    def test_one_bad_entry_does_not_hide_a_good_profile(self) -> None:
        """The other profile's token is still read and reported."""
        self._make_profile("work")
        self._make_profile("other")
        self._write_token("other", {"token": "tok-other"})
        self._write_token("work", {"token": "t"}).write_text("{not json")

        result = check_tokens(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("work", result.detail)
        self.assertNotIn("other", result.detail)


# ---------------------------------------------------------------------------
# check_relocated_hook_paths
# ---------------------------------------------------------------------------


class CheckRelocatedHookPathsTests(_HomeDirTestCase):
    """Tests for check_relocated_hook_paths(self.ws) -- the relocation blind-spot check."""

    def setUp(self) -> None:
        super().setUp()
        self._scripts_dir = self.home / ".claudewheel" / "scripts"
        self._shared_settings = self.home / ".claudewheel" / "shared-settings.json"

    def _write_settings(self, pdir: Path, settings: dict[str, Any]) -> None:
        (pdir / "settings.json").write_text(json.dumps(settings))

    def _timestamp_hooks(self, scripts_dir: str | Path) -> dict[str, Any]:
        return {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(Path(scripts_dir) / "hook-timestamp"),
                        },
                    ],
                },
            ],
        }

    def test_passes_when_commands_under_current_scripts_dir(self) -> None:
        pdir = self._make_profile("current")
        self._write_settings(pdir, {"hooks": self._timestamp_hooks(self._scripts_dir)})

        result = check_relocated_hook_paths(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("current scripts dir", result.detail)

    def test_fails_naming_profile_with_stale_root(self) -> None:
        pdir = self._make_profile("relocated")
        self._write_settings(
            pdir, {"hooks": self._timestamp_hooks("/old/home/.claudewheel/scripts")}
        )

        result = check_relocated_hook_paths(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("relocated", result.detail)
        self.assertIn("/old/home/.claudewheel/scripts/hook-timestamp", result.detail)
        self.assertIn("patch-profiles", result.detail)

    def test_profile_without_hooks_passes(self) -> None:
        pdir = self._make_profile("nohooks")
        self._write_settings(pdir, {"permissions": {}})

        result = check_relocated_hook_paths(self.ws)
        self.assertTrue(result.ok)

    def test_user_custom_hook_under_other_dir_passes(self) -> None:
        """A non-claudewheel hook command under any dir is ignored (not flagged)."""
        pdir = self._make_profile("custom")
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(self._scripts_dir / "hook-timestamp"),
                            },
                            {"type": "command", "command": "/opt/mine/my-own-hook"},
                        ],
                    },
                ]
            }
        }
        self._write_settings(pdir, settings)

        result = check_relocated_hook_paths(self.ws)
        self.assertTrue(result.ok)

    def test_shared_settings_stale_root_flagged(self) -> None:
        self._shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings.write_text(
            json.dumps({"hooks": self._timestamp_hooks("/stale/scripts")})
        )

        result = check_relocated_hook_paths(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("shared-settings.json", result.detail)
        self.assertIn("/stale/scripts/hook-timestamp", result.detail)


class DefaultProfileExemptionTests(_HomeDirTestCase):
    """Phase 4.5: the vanilla ~/.claude 'default' is exempt from guardrail checks.

    A bare ~/.claude (managed by Claude Code, read-only to cw) must produce ZERO
    warnings attributable to the default across a health run.
    """

    _GUARDRAIL_CHECKS = (
        check_shared_symlinks,
        check_hooks_wired,
        check_settings_defaults,
        check_shared_settings_drift,
        check_canonical_permissions_drift,
        check_relocated_hook_paths,
        check_tokens,
    )

    def _bare_default(self, *, with_settings: bool = True) -> Path:
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        if with_settings:
            (d / "settings.json").write_text("{}\n")
        return d

    def _canonical_shared_settings(self) -> None:
        from claudewheel.defaults import build_canonical_shared_settings
        from claudewheel.effects import write_json_atomic

        canonical = build_canonical_shared_settings(self.ws.scripts_dir)
        self.ws.shared_settings_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.ws.shared_settings_file, canonical)

    def test_default_is_discoverable_now(self) -> None:
        """Sanity: the lenient rule makes a bare ~/.claude discoverable as default."""
        self._bare_default()
        names = [p.name for p in _discover_profiles(self.ws)]
        self.assertEqual(names, ["default"])

    def test_guardrail_checks_never_mention_default(self) -> None:
        self._bare_default()
        self._canonical_shared_settings()
        for check in self._GUARDRAIL_CHECKS:
            r = check(self.ws)
            self.assertNotIn(
                "default", r.detail, f"{check.__name__} leaked the default profile"
            )

    def test_guardrail_checks_ok_with_only_bare_default(self) -> None:
        self._bare_default()
        self._canonical_shared_settings()
        for check in self._GUARDRAIL_CHECKS:
            self.assertTrue(check(self.ws).ok, f"{check.__name__} warned")

    def test_full_run_has_no_default_attributable_warning(self) -> None:
        self._bare_default()
        self._canonical_shared_settings()
        results = run_health_check(self.ws)
        for r in results:
            if r.ok:
                continue
            # None of the profile-warning formats may name the default.
            self.assertNotIn("default:", r.detail)
            self.assertNotIn("default/", r.detail)
            self.assertNotIn("shadowed: default", r.detail)
            self.assertNotIn("missing tokens: default", r.detail)

    def test_credentialed_default_not_flagged_missing_token(self) -> None:
        """A ~/.claude WITH .credentials.json but no cw token is not a warning.

        This is the common real case (Claude Code stores creds in ~/.claude).
        The default is exempt from the token check, so no 'missing tokens' warning.
        """
        d = self._bare_default()
        (d / ".credentials.json").write_text("{}")
        self._canonical_shared_settings()
        r = check_tokens(self.ws)
        self.assertTrue(r.ok)
        self.assertNotIn("default", r.detail)


if __name__ == "__main__":
    unittest.main()
