"""End-to-end: create a profile, discover it, resolve a launch against it.

Exercises the full persisted-config_dir contract in a sandbox home:

1. A profile created via the ProfileStore is discovered by segment discovery
   (values include it; metadata carries auth fields, never a config_dir).
2. resolve_launch_config with that profile selected yields its on-disk config
   dir and OAuth token in the launch env.
3. resolve_launch_config with a stale selection raises the hard error (no
   silent ~/.claude fallback).
"""

from __future__ import annotations

import unittest
from unittest import mock

from claudewheel.binaries import BinaryLocator
from claudewheel.launch import resolve_launch_config
from claudewheel.segment import _discover_profiles
from claudewheel.workspace import Workspace
from tests.wheelhelpers import SandboxHomeTestCase


class LaunchIntegrationTests(SandboxHomeTestCase):
    """A created profile flows through discovery and into a resolved launch env."""

    def setUp(self) -> None:
        super().setUp()
        # Workspace.default() honors the poisoned Path.home -> sandbox root.
        self.ws = Workspace.default()
        self.profiles = self.ws.profiles
        # A real, discoverable profile with a token.
        self.profiles.create("work", {"model": "claude-opus-4-8"})
        self.ws.tokens.add("work", "tok-xyz")

        # A tmpdir-backed locator so no version is required (fallback symlink).
        self.locator = BinaryLocator(
            versions_dir=self.home / "versions",
            claude_symlink=self.home / "claude",
        )

    def _resolve(self, selections):
        with mock.patch("claudewheel.launch.fetch_gh_token", return_value=None):
            return resolve_launch_config(
                selections,
                {},
                [],
                locator=self.locator,
                profiles=self.profiles,
            )

    def test_created_profile_is_discovered(self) -> None:
        """Segment discovery enumerates the created profile with auth metadata."""
        result = _discover_profiles({}, {}, self.ws)
        self.assertIn("work", result.values)
        # Metadata carries auth-presence fields, never a config_dir.
        self.assertNotIn("config_dir", result.metadata["work"])
        self.assertTrue(result.metadata["work"]["has_token"])

    def test_resolve_yields_profile_dir_and_token(self) -> None:
        """Selecting the profile puts its config dir + token into the env."""
        _, _, env = self._resolve({"profile": "work"})
        self.assertEqual(
            env["CLAUDE_CONFIG_DIR"],
            str(self.sandbox_paths["PROFILES_DIR"] / "work"),
        )
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-xyz")

    def test_stale_selection_raises(self) -> None:
        """A selection naming a non-existent profile raises, listing the real one."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve({"profile": "ghost"})
        msg = str(ctx.exception)
        self.assertIn("ghost", msg)
        self.assertIn("work", msg)


if __name__ == "__main__":
    unittest.main()
