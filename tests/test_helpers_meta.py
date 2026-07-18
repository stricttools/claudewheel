"""Meta-tests: prove the shared sandbox-home mechanism actually contains I/O."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.wheelhelpers import REAL_HOME, SandboxHomeTestCase


class PoisonedHomeTests(SandboxHomeTestCase):
    """Path.home() and $HOME resolve into the sandbox, never the real home."""

    def test_path_home_returns_sandbox(self) -> None:
        """Inside the base class, Path.home() is the tmpdir home."""
        self.assertEqual(Path.home(), self.home)
        self.assertNotEqual(Path.home(), REAL_HOME)

    def test_home_env_points_at_sandbox(self) -> None:
        """The HOME env var is redirected so os.path.expanduser resolves here."""
        self.assertEqual(os.environ["HOME"], str(self.home))
        self.assertEqual(Path(os.path.expanduser("~")), self.home)

    def test_write_via_path_home_lands_in_sandbox_not_real_home(self) -> None:
        """A write addressed via Path.home() lands in the sandbox, not real home."""
        probe = Path.home() / ".claudewheel" / "poison_probe.txt"
        probe.write_text("sandboxed")

        # Landed in the sandbox.
        self.assertTrue((self.home / ".claudewheel" / "poison_probe.txt").is_file())
        # Did NOT land in the real home.
        self.assertFalse((REAL_HOME / ".claudewheel" / "poison_probe.txt").exists())

    def test_sandbox_structure_exists(self) -> None:
        """The fake ~/.claudewheel is populated with the expected structure."""
        ld = self.home / ".claudewheel"
        for sub in ("profiles", "shared", "skills", "themes", "scripts", "hooks"):
            self.assertTrue((ld / sub).is_dir(), f"missing {sub}/")
        for f in (
            "config.json",
            "state.json",
            "options.json",
            "segments.json",
            "tokens.json",
            "shared-settings.json",
        ):
            self.assertTrue((ld / f).is_file(), f"missing {f}")


class DefaultProfilePopulationTests(SandboxHomeTestCase):
    """populate_default_profile=True creates ~/.claude with credentials."""

    populate_default_profile = True

    def test_default_profile_created(self) -> None:
        default_dir = self.home / ".claude"
        self.assertTrue(default_dir.is_dir())
        self.assertTrue((default_dir / ".credentials.json").is_file())


if __name__ == "__main__":
    unittest.main()
