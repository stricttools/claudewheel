"""Tests for profile_ops.py: fix_auth_shadow and _is_profile_running.

Profile create/delete/rename moved to claudewheel.profile_store; those paths
(and their old tests) were removed in the persisted-config_dir flip and are now
covered by tests/test_profile_store_write.py. What remains here is the fix-auth
flow and the running-state check that callers apply as policy.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


from claudewheel import profile_ops
from claudewheel.profile_data import ProfileDataStore
from tests.wheelhelpers import (
    build_profile_dir,
    write_token_entry,
    live_record,
    stale_record,
)


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
        self.profiles_dir = self.launcher_dir / "profiles"
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(self.launcher_dir, claude_dir=self.home / ".claude")

    def tearDown(self) -> None:
        self._patcher_home.stop()
        self._tmp.cleanup()

    def _write_token(self, name: str, entry: dict[str, Any]) -> None:
        """Store *name*'s token entry inside its own profile directory."""
        write_token_entry(self.profiles_dir / name, entry)

    def _read_token(self, name: str) -> dict[str, Any]:
        return ProfileDataStore(self.profiles_dir / name).load()

    def _make_profile_dir(self, name: str) -> Path:
        return build_profile_dir(
            self.profiles_dir,
            name,
            parents=True,
            exist_ok=True,
            credentials=True,
            settings_text="{}",
        )


# ---------------------------------------------------------------------------
# _is_profile_running
# ---------------------------------------------------------------------------


class IsProfileRunningTests(_ProfileOpsTestCase):
    """The running-state check drives CLI/TUI delete policy.

    It is a delegate to :mod:`claudewheel.session_registry`; the registry
    parsing, phantom filtering and kind classification are covered in
    ``tests/test_session_registry.py``.  What is asserted here is the wiring:
    the profile's own config dir is what gets read, and a live interactive
    record in it answers True.
    """

    def test_no_sessions_dir_not_running(self) -> None:
        self._make_profile_dir("idle")
        self.assertFalse(profile_ops._is_profile_running(self.ws, "idle"))

    def test_live_interactive_record_is_running(self) -> None:
        pdir = self._make_profile_dir("busy")
        live_record(pdir / "sessions")
        self.assertTrue(profile_ops._is_profile_running(self.ws, "busy"))

    def test_stale_record_not_running(self) -> None:
        pdir = self._make_profile_dir("stale")
        stale_record(pdir / "sessions")
        self.assertFalse(profile_ops._is_profile_running(self.ws, "stale"))

    def test_background_record_not_running(self) -> None:
        pdir = self._make_profile_dir("daemonised")
        live_record(pdir / "sessions", kind="daemon")
        self.assertFalse(profile_ops._is_profile_running(self.ws, "daemonised"))

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
        """When the profile stores no token entry, reason is 'no-token'."""
        self._make_profile_dir("orphan")
        result = profile_ops.fix_auth_shadow(self.ws, "orphan")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-token")

    def test_no_credentials_file_returns_no_shadow(self) -> None:
        """When .credentials.json doesn't exist, reason is 'no-shadow'."""
        pdir = self._make_profile_dir("clean")
        (pdir / ".credentials.json").unlink()
        self._write_token("clean", {"token": "tok-abc"})
        result = profile_ops.fix_auth_shadow(self.ws, "clean")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-shadow")

    def test_no_claudeAiOauth_key_returns_no_shadow(self) -> None:
        """When .credentials.json exists but has no claudeAiOauth, reason is 'no-shadow'."""
        pdir = self._make_profile_dir("noshadow")
        self._write_credentials(pdir, {"mcpOAuth": {"x": "y"}})
        self._write_token("noshadow", {"token": "tok-ns"})
        result = profile_ops.fix_auth_shadow(self.ws, "noshadow")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-shadow")

    def test_unreadable_credentials_returns_reason(self) -> None:
        """When .credentials.json is corrupt JSON, reason is 'unreadable-creds'."""
        pdir = self._make_profile_dir("corrupt")
        (pdir / ".credentials.json").write_text("{not json at all")
        self._write_token("corrupt", {"token": "tok-c"})
        result = profile_ops.fix_auth_shadow(self.ws, "corrupt")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unreadable-creds")

    def test_strips_shadow_and_discards_its_tier_fields(self) -> None:
        """Shadow is stripped; its tier fields never reach the token entry.

        The declared plan is claudewheel's own, written through the plan picker.
        Harvesting it back out of Claude Code's credential file is the
        back-channel that kept the launch-time tier stub alive.
        """
        pdir = self._make_profile_dir("work")
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {
                    "accessToken": "short-lived",
                    "rateLimitTier": "default_claude_max_5x",
                    "subscriptionType": "pro",
                },
                "mcpOAuth": {"keep": "this"},
            },
        )
        self._write_token("work", {"token": "tok-work"})

        result = profile_ops.fix_auth_shadow(self.ws, "work")

        self.assertTrue(result.ok)
        self.assertIsNone(result.reason)

        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)
        self.assertIn("mcpOAuth", creds)

        entry = self._read_token("work")
        self.assertNotIn("rateLimitTier", entry)
        self.assertNotIn("subscriptionType", entry)
        self.assertEqual(entry["token"], "tok-work")

    def test_strips_shadow_no_tier_data(self) -> None:
        """Shadow stripped even without tier fields; the token entry is untouched."""
        pdir = self._make_profile_dir("notier")
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {"accessToken": "short"},
                "mcpOAuth": {"keep": "this"},
            },
        )
        self._write_token("notier", {"token": "tok-nt"})

        result = profile_ops.fix_auth_shadow(self.ws, "notier")

        self.assertTrue(result.ok)

        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)

        self.assertNotIn("rateLimitTier", self._read_token("notier"))

    def test_removes_a_credentials_file_left_holding_nothing(self) -> None:
        """A file whose only content was the shadow is removed, not left as ``{}``.

        An empty file goes on answering "this profile has credentials" to
        discovery, the inspection report and the file-permission check.
        """
        pdir = self._make_profile_dir("only-shadow")
        self._write_credentials(pdir, {"claudeAiOauth": {"accessToken": "short"}})
        self._write_token("only-shadow", {"token": "tok-os"})

        result = profile_ops.fix_auth_shadow(self.ws, "only-shadow")

        self.assertTrue(result.ok)
        self.assertFalse((pdir / ".credentials.json").exists())

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
        self._write_token("perms", {"token": "tok-p"})

        profile_ops.fix_auth_shadow(self.ws, "perms")

        mode = creds_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
