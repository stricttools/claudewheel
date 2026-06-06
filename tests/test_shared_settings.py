"""Tests for shared-settings.json drift detection and canonical source logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from claudewheel.health import HealthResult, check_shared_settings_drift


class _HomeDirTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as Path.home() and patches it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher = patch.object(Path, "home", return_value=self.home)
        self._patcher.start()
        self._profiles_dir = self.home / ".claudewheel" / "profiles"
        self._shared_settings = self.home / ".claudewheel" / "shared-settings.json"
        self._scripts_dir = self.home / ".claudewheel" / "scripts"
        self._dir_patches = [
            patch("claudewheel.health.PROFILES_DIR", self._profiles_dir),
            patch("claudewheel.health.SHARED_SETTINGS_FILE", self._shared_settings),
            patch("claudewheel.health.SCRIPTS_DIR", self._scripts_dir),
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

    def _write_shared_settings(self, data: dict) -> None:
        """Write shared-settings.json in the temp home."""
        self._shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings.write_text(json.dumps(data, indent=2) + "\n")

    def _write_profile_settings(self, pdir: Path, settings: dict) -> None:
        """Write settings.json into a profile directory."""
        (pdir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    def _canonical(self) -> dict:
        """Return canonical shared settings using the test scripts dir."""
        return build_canonical_shared_settings(self._scripts_dir)


# ---------------------------------------------------------------------------
# check_shared_settings_drift
# ---------------------------------------------------------------------------


class CheckSharedSettingsDriftTests(_HomeDirTestCase):
    """Tests for check_shared_settings_drift()."""

    def test_all_profiles_in_sync(self) -> None:
        """Returns OK when all profiles match shared-settings.json exactly."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("alpha")
        self._write_profile_settings(pdir, {
            "hooks": canonical["hooks"],
            "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
        })
        pdir2 = self._make_profile("beta")
        self._write_profile_settings(pdir2, {
            "hooks": canonical["hooks"],
            "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
        })

        result = check_shared_settings_drift()
        self.assertTrue(result.ok)
        self.assertIn("2 profiles in sync", result.detail)

    def test_profile_missing_hook(self) -> None:
        """Drift detected when a profile is missing a hook entry."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("drifted")
        # Write hooks with only one of the two hook commands
        hooks = {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        canonical["hooks"]["UserPromptSubmit"][0]["hooks"][0],
                        # Missing the second hook (hook-stamp-origin)
                    ],
                }
            ]
        }
        self._write_profile_settings(pdir, {
            "hooks": hooks,
            "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
        })

        result = check_shared_settings_drift()
        self.assertFalse(result.ok)
        self.assertIn("drifted", result.detail)

    def test_profile_extra_hook(self) -> None:
        """Drift detected when a profile has an extra hook not in canonical."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("extra")
        # Add an extra hook command
        hooks = {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        *canonical["hooks"]["UserPromptSubmit"][0]["hooks"],
                        {"type": "command", "command": "/usr/bin/extra-hook"},
                    ],
                }
            ]
        }
        self._write_profile_settings(pdir, {
            "hooks": hooks,
            "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
        })

        result = check_shared_settings_drift()
        self.assertFalse(result.ok)
        self.assertIn("extra", result.detail)

    def test_disallowed_tools_mismatch(self) -> None:
        """Drift detected when disallowedTools differs from canonical."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("tools-off")
        # Remove one tool from the profile's disallowed list
        partial_tools = canonical["disallowedTools"][:-1]
        self._write_profile_settings(pdir, {
            "hooks": canonical["hooks"],
            "claudewheel": {"disallowedTools": partial_tools},
        })

        result = check_shared_settings_drift()
        self.assertFalse(result.ok)
        self.assertIn("tools-off", result.detail)
        self.assertIn("missing", result.detail)

    def test_shared_settings_missing(self) -> None:
        """Handles gracefully when shared-settings.json doesn't exist."""
        self._make_profile("lonely")

        result = check_shared_settings_drift()
        self.assertTrue(result.ok)
        self.assertIn("not found", result.detail)

    def test_no_profiles(self) -> None:
        """Returns OK when no profiles exist."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        result = check_shared_settings_drift()
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)

    def test_profile_no_settings_json(self) -> None:
        """Drift reported when a profile has no settings.json."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)
        self._make_profile("bare")

        result = check_shared_settings_drift()
        self.assertFalse(result.ok)
        self.assertIn("bare: no settings.json", result.detail)

    def test_profile_extra_disallowed_tool(self) -> None:
        """Drift detected when a profile has extra tools not in canonical."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("surplus")
        extra_tools = canonical["disallowedTools"] + ["ExtraTool"]
        self._write_profile_settings(pdir, {
            "hooks": canonical["hooks"],
            "claudewheel": {"disallowedTools": extra_tools},
        })

        result = check_shared_settings_drift()
        self.assertFalse(result.ok)
        self.assertIn("surplus", result.detail)
        self.assertIn("ExtraTool", result.detail)

    def test_corrupt_shared_settings(self) -> None:
        """Returns failure when shared-settings.json is corrupt JSON."""
        self._shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings.write_text("not valid json{{{")
        self._make_profile("victim")

        result = check_shared_settings_drift()
        self.assertFalse(result.ok)
        self.assertIn("unreadable", result.detail)


# ---------------------------------------------------------------------------
# build_canonical_shared_settings
# ---------------------------------------------------------------------------


class BuildCanonicalSharedSettingsTests(unittest.TestCase):
    """Tests for build_canonical_shared_settings()."""

    def test_has_hooks_key(self) -> None:
        """Result contains a hooks dict with UserPromptSubmit."""
        result = build_canonical_shared_settings(Path("/scripts"))
        self.assertIn("hooks", result)
        self.assertIn("UserPromptSubmit", result["hooks"])

    def test_has_disallowed_tools(self) -> None:
        """Result contains the full DISALLOWED_TOOLS list."""
        result = build_canonical_shared_settings(Path("/scripts"))
        self.assertEqual(result["disallowedTools"], DISALLOWED_TOOLS)

    def test_hooks_reference_scripts_dir(self) -> None:
        """Hook commands reference the provided scripts_dir."""
        scripts = Path("/my/scripts")
        result = build_canonical_shared_settings(scripts)
        hooks = result["hooks"]["UserPromptSubmit"][0]["hooks"]
        self.assertTrue(any("hook-timestamp" in h["command"] for h in hooks))
        self.assertTrue(any("hook-stamp-origin" in h["command"] for h in hooks))
        for h in hooks:
            self.assertTrue(h["command"].startswith(str(scripts)))

    def test_disallowed_tools_is_copy(self) -> None:
        """Returned disallowedTools is a copy, not the original list."""
        result = build_canonical_shared_settings(Path("/scripts"))
        self.assertIsNot(result["disallowedTools"], DISALLOWED_TOOLS)


if __name__ == "__main__":
    unittest.main()
