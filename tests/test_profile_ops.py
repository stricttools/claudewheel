"""Tests for profile_ops.py: fix_auth_shadow and _is_profile_running.

Profile create/delete/rename moved to claudewheel.profile_store; those paths
(and their old tests) were removed in the persisted-config_dir flip and are now
covered by tests/test_profile_store_write.py. What remains here is the fix-auth
flow and the running-state check that callers apply as policy.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


from claudewheel import profile_ops


class _ProfileOpsTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as home and patches paths."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher_home = patch.object(
            Path, "home", autospec=True, return_value=self.home
        )
        self._patcher_home.start()

        self.launcher_dir = self.home / ".claudewheel"
        self.launcher_dir.mkdir()
        self.tokens_file = self.launcher_dir / "tokens.json"
        self.profiles_dir = self.launcher_dir / "profiles"
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(self.launcher_dir, claude_dir=self.home / ".claude")

    def tearDown(self) -> None:
        self._patcher_home.stop()
        self._tmp.cleanup()

    def _write_tokens(self, tokens: dict[str, Any]) -> None:
        self.tokens_file.write_text(json.dumps(tokens, indent=2) + "\n")

    def _make_profile_dir(self, name: str) -> Path:
        pdir = self.profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        (pdir / "settings.json").write_text("{}")
        return pdir


# ---------------------------------------------------------------------------
# _is_profile_running
# ---------------------------------------------------------------------------


class IsProfileRunningTests(_ProfileOpsTestCase):
    """The running-state check drives CLI/TUI delete policy."""

    def test_no_sessions_dir_not_running(self) -> None:
        self._make_profile_dir("idle")
        self.assertFalse(profile_ops._is_profile_running(self.ws, "idle"))

    def test_live_pid_is_running(self) -> None:
        pdir = self._make_profile_dir("busy")
        sessions = pdir / "sessions"
        sessions.mkdir()
        (sessions / "sess.pid").write_text(str(os.getpid()))
        self.assertTrue(profile_ops._is_profile_running(self.ws, "busy"))

    def test_stale_pid_not_running(self) -> None:
        pdir = self._make_profile_dir("stale")
        sessions = pdir / "sessions"
        sessions.mkdir()
        # A PID that is almost certainly not alive.
        (sessions / "sess.pid").write_text("999999")
        self.assertFalse(profile_ops._is_profile_running(self.ws, "stale"))

    def test_missing_profile_not_running(self) -> None:
        self.assertFalse(profile_ops._is_profile_running(self.ws, "nonexistent"))


# ---------------------------------------------------------------------------
# fix_auth_shadow
# ---------------------------------------------------------------------------


class FixAuthShadowTests(_ProfileOpsTestCase):
    """Tests for fix_auth_shadow: remove claudeAiOauth from .credentials.json."""

    def _write_credentials(self, pdir: Path, data: dict[str, Any]) -> None:
        creds = pdir / ".credentials.json"
        creds.write_text(json.dumps(data))
        creds.chmod(0o600)

    def test_no_token_returns_reason(self) -> None:
        """When tokens.json has no entry for the profile, reason is 'no-token'."""
        self._make_profile_dir("orphan")
        self._write_tokens({})
        result = profile_ops.fix_auth_shadow(self.ws, "orphan")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-token")

    def test_no_credentials_file_returns_no_shadow(self) -> None:
        """When .credentials.json doesn't exist, reason is 'no-shadow'."""
        pdir = self._make_profile_dir("clean")
        (pdir / ".credentials.json").unlink()
        self._write_tokens({"clean": {"token": "tok-abc"}})
        result = profile_ops.fix_auth_shadow(self.ws, "clean")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-shadow")

    def test_no_claudeAiOauth_key_returns_no_shadow(self) -> None:
        """When .credentials.json exists but has no claudeAiOauth, reason is 'no-shadow'."""
        pdir = self._make_profile_dir("noshadow")
        self._write_credentials(pdir, {"mcpOAuth": {"x": "y"}})
        self._write_tokens({"noshadow": {"token": "tok-ns"}})
        result = profile_ops.fix_auth_shadow(self.ws, "noshadow")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-shadow")

    def test_unreadable_credentials_returns_reason(self) -> None:
        """When .credentials.json is corrupt JSON, reason is 'unreadable-creds'."""
        pdir = self._make_profile_dir("corrupt")
        (pdir / ".credentials.json").write_text("{not json at all")
        self._write_tokens({"corrupt": {"token": "tok-c"}})
        result = profile_ops.fix_auth_shadow(self.ws, "corrupt")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unreadable-creds")

    def test_strips_shadow_and_saves_tier(self) -> None:
        """Shadow is stripped, tier data saved to tokens.json."""
        pdir = self._make_profile_dir("work")
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {
                    "accessToken": "short-lived",
                    "rateLimitTier": "default_claude_pro",
                    "subscriptionType": "claude_pro",
                },
                "mcpOAuth": {"keep": "this"},
            },
        )
        self._write_tokens({"work": {"token": "tok-work"}})

        result = profile_ops.fix_auth_shadow(self.ws, "work")

        self.assertTrue(result.ok)
        self.assertIsNone(result.reason)
        self.assertEqual(result.tier_saved, "default_claude_pro")
        self.assertEqual(result.subscription_saved, "claude_pro")

        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)
        self.assertIn("mcpOAuth", creds)

        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["work"]["rateLimitTier"], "default_claude_pro")
        self.assertEqual(tokens["work"]["subscriptionType"], "claude_pro")
        self.assertEqual(tokens["work"]["token"], "tok-work")

    def test_strips_shadow_no_tier_data(self) -> None:
        """Shadow stripped even without tier fields; no tier saved."""
        pdir = self._make_profile_dir("notier")
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {"accessToken": "short"},
            },
        )
        self._write_tokens({"notier": {"token": "tok-nt"}})

        result = profile_ops.fix_auth_shadow(self.ws, "notier")

        self.assertTrue(result.ok)
        self.assertIsNone(result.tier_saved)
        self.assertIsNone(result.subscription_saved)

        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)

        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("rateLimitTier", tokens.get("notier", {}))

    def test_bare_string_token_upgraded_to_dict_with_tier(self) -> None:
        """When token entry is a bare string, it's upgraded to a dict to hold tier."""
        pdir = self._make_profile_dir("legacy")
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {
                    "accessToken": "ephemeral",
                    "rateLimitTier": "tier_max",
                },
            },
        )
        self._write_tokens({"legacy": "bare-tok-string"})

        result = profile_ops.fix_auth_shadow(self.ws, "legacy")

        self.assertTrue(result.ok)
        self.assertEqual(result.tier_saved, "tier_max")

        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["legacy"]["token"], "bare-tok-string")
        self.assertEqual(tokens["legacy"]["rateLimitTier"], "tier_max")

    def test_atomic_write_preserves_credentials_permissions(self) -> None:
        """The atomic write to .credentials.json preserves 0600 permissions."""
        pdir = self._make_profile_dir("perms")
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {"accessToken": "x"},
                "other": "keep",
            },
        )
        creds_path = pdir / ".credentials.json"
        creds_path.chmod(0o600)
        self._write_tokens({"perms": {"token": "tok-p"}})

        profile_ops.fix_auth_shadow(self.ws, "perms")

        mode = creds_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
