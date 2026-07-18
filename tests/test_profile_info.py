"""Tests for profile_info: gathering and formatting profile inspection reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from claudewheel import profile_info
from claudewheel.shared_store import SharedStore


class ProfileInfoFixture(unittest.TestCase):
    """Shared tmp-dir fixture: fake profiles dir, shared store, tokens/options."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.profiles_dir = root / ".claudewheel" / "profiles"
        self.shared_dir = root / ".claudewheel" / "shared"
        self.skills_dir = root / ".claudewheel" / "skills"
        self.tokens_file = root / ".claudewheel" / "tokens.json"
        self.options_file = root / ".claudewheel" / "options.json"
        self.profile = self.profiles_dir / "work"
        self.profile.mkdir(parents=True)
        self.shared_dir.mkdir(parents=True)
        self.skills_dir.mkdir(parents=True)
        for d in SharedStore.SHARED_SUBDIRS:
            (self.shared_dir / d).mkdir()

        from claudewheel.workspace import Workspace
        self.ws = Workspace.open(root / ".claudewheel",
                                 claude_dir=Path.home() / ".claude")

    def _link_all(self) -> None:
        """Create intact shared-store symlinks in the profile dir."""
        for d in SharedStore.SHARED_SUBDIRS:
            (self.profile / d).symlink_to(self.shared_dir / d)
        (self.profile / "skills").symlink_to(self.skills_dir)

    def _write_tokens(self, entries: dict) -> None:
        self.tokens_file.write_text(json.dumps(entries))

    def _write_options(self, values: list[str], pinned: list[str]) -> None:
        self.options_file.write_text(json.dumps(
            {"profile": {"values": values, "pinned": pinned}}))


class GatherAuthTests(ProfileInfoFixture):
    """Auth state: credentials file, token entry, expiry math."""

    def test_no_credentials_no_token(self) -> None:
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.exists)
        self.assertFalse(report.has_credentials)
        self.assertFalse(report.has_token)
        self.assertIsNone(report.token_expiry)

    def test_credentials_present(self) -> None:
        (self.profile / ".credentials.json").write_text("{}")
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.has_credentials)

    def test_token_expiry_math(self) -> None:
        created = date.today() - timedelta(days=100)
        expires = created + timedelta(days=365)
        self._write_tokens({"work": {
            "token": "tok",
            "created": created.isoformat(),
            "expires_at": expires.isoformat(),
        }})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.has_token)
        assert report.token_expiry is not None
        self.assertEqual(report.token_expiry.created, created)
        self.assertEqual(report.token_expiry.expires, expires)
        self.assertEqual(report.token_expiry.remaining_days, 265)

    def test_token_for_other_profile_not_picked_up(self) -> None:
        self._write_tokens({"other": {"token": "tok"}})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.has_token)
        self.assertIsNone(report.token_expiry)


class GatherRegistrationTests(ProfileInfoFixture):
    """registered/pinned flags come from options.json."""

    def test_registered_and_pinned(self) -> None:
        self._write_options(values=["work"], pinned=["work"])
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.registered)
        self.assertTrue(report.pinned)

    def test_registered_only(self) -> None:
        self._write_options(values=["work"], pinned=[])
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.registered)
        self.assertFalse(report.pinned)

    def test_missing_options_file_tolerated(self) -> None:
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.registered)
        self.assertFalse(report.pinned)


class GatherSharedDirTests(ProfileInfoFixture):
    """Shared-dir classification and the danger flag."""

    def test_all_intact_no_danger(self) -> None:
        self._link_all()
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.danger)
        for name in list(SharedStore.SHARED_SUBDIRS) + ["skills"]:
            self.assertEqual(report.shared_dirs[name], "intact", name)

    def test_real_dir_sets_danger(self) -> None:
        self._link_all()
        (self.profile / "todos").unlink()
        (self.profile / "todos").mkdir()
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.danger)
        self.assertEqual(report.shared_dirs["todos"], "real-dir")

    def test_wrong_target_is_not_danger(self) -> None:
        self._link_all()
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.profile / "projects").unlink()
        (self.profile / "projects").symlink_to(elsewhere)
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.danger)
        self.assertEqual(report.shared_dirs["projects"], "wrong-target")


class GatherSettingsTests(ProfileInfoFixture):
    """settings.json summary: permission counts and behavior flags."""

    def test_settings_summary(self) -> None:
        (self.profile / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash", "Read"], "deny": ["WebFetch"]},
            "awaySummaryEnabled": False,
            "cleanupPeriodDays": 3650,
        }))
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.settings_found)
        self.assertEqual(report.permission_counts,
                         {"allow": 2, "deny": 1, "ask": 0})
        self.assertIs(report.away_summary_enabled, False)
        self.assertEqual(report.cleanup_period_days, 3650)
        self.assertIsNone(report.auto_memory_enabled)  # missing key tolerated

    def test_missing_settings_file_tolerated(self) -> None:
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.settings_found)
        self.assertEqual(report.permission_counts, {})
        self.assertIsNone(report.away_summary_enabled)
        self.assertIsNone(report.cleanup_period_days)
        self.assertIsNone(report.auto_memory_enabled)

    def test_corrupt_settings_file_tolerated(self) -> None:
        (self.profile / "settings.json").write_text("{not json")
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.settings_found)


class GatherSessionsTests(ProfileInfoFixture):
    """Active session count from sessions/*.pid liveness checks."""

    def test_live_and_stale_pids(self) -> None:
        sessions = self.profile / "sessions"
        sessions.mkdir()
        (sessions / "a.pid").write_text("111")
        (sessions / "b.pid").write_text("222")
        (sessions / "c.pid").write_text("garbage")  # unparseable -> stale
        (sessions / "notes.txt").write_text("999")  # not a .pid file

        def fake_kill(pid: int, sig: int) -> None:
            if pid != 111:
                raise OSError("no such process")

        with mock.patch("claudewheel.profile_info.os.kill",
                        side_effect=fake_kill):
            report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.active_sessions, 1)

    def test_no_sessions_dir(self) -> None:
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.active_sessions, 0)


class GatherDiskUsageTests(ProfileInfoFixture):
    """Disk usage sums only real files, never symlinked content."""

    def test_symlinked_content_excluded(self) -> None:
        (self.profile / "settings.json").write_text("x" * 100)
        (self.profile / "data").mkdir()
        (self.profile / "data" / "blob").write_text("y" * 50)

        # A big directory outside the profile, reachable only via symlink.
        big = Path(self._tmp.name) / "big-shared"
        big.mkdir()
        (big / "huge.bin").write_bytes(b"z" * 100_000)
        (self.profile / "projects").symlink_to(big)
        # A symlinked file is also excluded.
        (self.profile / "link.bin").symlink_to(big / "huge.bin")

        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.disk_usage_bytes, 150)


class GatherUnknownAndDefaultTests(ProfileInfoFixture):
    """Unknown profile flags and the "default" config-dir special case."""

    def test_unknown_profile(self) -> None:
        report = profile_info.gather_profile_info(self.ws, "nope")
        self.assertFalse(report.exists)
        self.assertFalse(report.registered)
        self.assertFalse(report.has_token)
        self.assertFalse(report.has_credentials)
        self.assertEqual(report.disk_usage_bytes, 0)
        self.assertEqual(report.shared_dirs, {})

    def test_default_config_dir(self) -> None:
        # Name->dir resolution now lives in ProfileStore.path_for; profile_info
        # builds the store from its patched module constants at call time.
        store = self.ws.profiles
        self.assertEqual(store.path_for("default"), Path.home() / ".claude")

    def test_named_config_dir(self) -> None:
        store = self.ws.profiles
        self.assertEqual(store.path_for("work"), self.profiles_dir / "work")


class GatherTierTests(ProfileInfoFixture):
    """Tier data from tokens.json entry."""

    def test_tier_from_tokens(self) -> None:
        self._write_tokens({"work": {
            "token": "tok",
            "rateLimitTier": "default_claude_pro",
            "subscriptionType": "claude_pro",
        }})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.rate_limit_tier, "default_claude_pro")
        self.assertEqual(report.subscription_type, "claude_pro")

    def test_no_tier_returns_none(self) -> None:
        self._write_tokens({"work": {"token": "tok"}})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertIsNone(report.rate_limit_tier)
        self.assertIsNone(report.subscription_type)

    def test_tier_only_entry_no_token(self) -> None:
        """Tier-only entry (from session login) has tier but no token."""
        self._write_tokens({"work": {
            "rateLimitTier": "default_claude_max_5x",
            "subscriptionType": "claude_max_5x",
        }})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.has_token)  # entry exists
        self.assertEqual(report.rate_limit_tier, "default_claude_max_5x")

    def test_bare_string_entry_no_tier(self) -> None:
        """Legacy bare-string entries have no tier fields."""
        self._write_tokens({"work": "tok-legacy"})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertIsNone(report.rate_limit_tier)
        self.assertIsNone(report.subscription_type)


class GatherAuthShadowTests(ProfileInfoFixture):
    """has_auth_shadow: both token AND claudeAiOauth in credentials needed."""

    def test_shadow_when_both_present(self) -> None:
        """has_auth_shadow is True when token exists AND .credentials.json has claudeAiOauth."""
        (self.profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "short-lived"}})
        )
        self._write_tokens({"work": {"token": "tok-long-lived"}})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertTrue(report.has_auth_shadow)

    def test_no_shadow_when_only_token(self) -> None:
        """has_auth_shadow is False when token exists but no claudeAiOauth in credentials."""
        (self.profile / ".credentials.json").write_text(
            json.dumps({"mcpOAuth": {"x": "y"}})
        )
        self._write_tokens({"work": {"token": "tok-long"}})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.has_auth_shadow)

    def test_no_shadow_when_only_credentials(self) -> None:
        """has_auth_shadow is False when claudeAiOauth exists but no valid token."""
        (self.profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "x"}})
        )
        # No entry in tokens.json
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.has_auth_shadow)

    def test_no_shadow_when_no_credentials_file(self) -> None:
        """has_auth_shadow is False when .credentials.json doesn't exist."""
        self._write_tokens({"work": {"token": "tok"}})
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertFalse(report.has_auth_shadow)

    def test_shadow_shown_in_format_report(self) -> None:
        """format_report includes the auth shadow line when has_auth_shadow is True."""
        (self.profile / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "short"}})
        )
        self._write_tokens({"work": {"token": "tok"}})
        report = profile_info.gather_profile_info(self.ws, "work")
        lines = profile_info.format_report(report)
        text = "\n".join(lines)
        self.assertIn("Auth shadow: yes", text)
        self.assertIn("session credentials override token", text)

    def test_no_shadow_line_when_not_shadowed(self) -> None:
        """format_report omits the auth shadow line when has_auth_shadow is False."""
        report = profile_info.gather_profile_info(self.ws, "work")
        lines = profile_info.format_report(report)
        text = "\n".join(lines)
        self.assertNotIn("Auth shadow", text)


class FormatReportTests(ProfileInfoFixture):
    """format_report renders the report fields as readable lines."""

    def test_full_report_lines(self) -> None:
        self._link_all()
        (self.profile / "todos").unlink()
        (self.profile / "todos").mkdir()  # danger
        (self.profile / ".credentials.json").write_text("{}")
        (self.profile / "settings.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash"], "ask": ["Write"]},
            "awaySummaryEnabled": False,
        }))
        created = date.today() - timedelta(days=10)
        expires = created + timedelta(days=365)
        self._write_tokens({"work": {
            "token": "tok",
            "created": created.isoformat(),
            "expires_at": expires.isoformat(),
        }})
        self._write_options(values=["work"], pinned=["work"])

        report = profile_info.gather_profile_info(self.ws, "work")
        lines = profile_info.format_report(report)
        text = "\n".join(lines)

        self.assertIn("Profile: work", text)
        self.assertIn(str(self.profile), text)
        self.assertIn("Registered: yes (pinned)", text)
        self.assertIn("Credentials file: present", text)
        self.assertIn(f"created {created.isoformat()}", text)
        self.assertIn(f"expires {expires.isoformat()}", text)
        self.assertIn("355 days left", text)
        self.assertIn("todos: real-dir", text)
        self.assertIn("DANGER", text)
        self.assertIn("1 allow, 0 deny, 1 ask", text)
        self.assertIn("awaySummaryEnabled: False", text)
        self.assertIn("Active sessions: 0", text)
        self.assertIn("Disk usage:", text)
        # Tier should show "unknown" since no tier in tokens entry
        self.assertIn("Tier: unknown", text)

    def test_minimal_report_lines(self) -> None:
        report = profile_info.gather_profile_info(self.ws, "nope")
        text = "\n".join(profile_info.format_report(report))
        self.assertIn("(missing)", text)
        self.assertIn("Registered: no", text)
        self.assertIn("Token: none", text)
        self.assertIn("Settings: no settings.json", text)
        self.assertIn("Tier: unknown", text)

    def test_tier_display_with_subscription(self) -> None:
        """When tier and subscription are present, both appear."""
        self._write_tokens({"work": {
            "token": "tok",
            "rateLimitTier": "default_claude_pro",
            "subscriptionType": "claude_pro",
        }})
        report = profile_info.gather_profile_info(self.ws, "work")
        lines = profile_info.format_report(report)
        text = "\n".join(lines)
        self.assertIn("Tier: default_claude_pro (claude_pro)", text)

    def test_tier_display_without_subscription(self) -> None:
        """When only tier is present, no parenthetical."""
        self._write_tokens({"work": {
            "token": "tok",
            "rateLimitTier": "default_claude_max_20x",
        }})
        report = profile_info.gather_profile_info(self.ws, "work")
        lines = profile_info.format_report(report)
        text = "\n".join(lines)
        self.assertIn("Tier: default_claude_max_20x", text)
        # Should not have empty parentheses
        self.assertNotIn("()", text)

    def test_format_size_units(self) -> None:
        self.assertEqual(profile_info._format_size(0), "0 B")
        self.assertEqual(profile_info._format_size(512), "512 B")
        self.assertEqual(profile_info._format_size(2048), "2.0 KB")
        self.assertEqual(profile_info._format_size(3 * 1024 * 1024), "3.0 MB")
        self.assertEqual(profile_info._format_size(5 * 1024 ** 3), "5.0 GB")


if __name__ == "__main__":
    unittest.main()
