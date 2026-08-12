"""Meta-tests: prove the shared sandbox-home mechanism actually contains I/O."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.wheelhelpers import REAL_HOME, SandboxHomeTestCase, build_profile_dir


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


class BuildProfileDirTests(unittest.TestCase):
    """The shared profile-directory builder every make_profile delegates to.

    The suite's builders differ deliberately on which marker file a profile
    carries and whether a missing parent or an existing directory is tolerated;
    tests elsewhere check behaviour when a marker is absent. These pin each
    difference to a parameter so a later edit cannot quietly homogenize them.
    """

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.parent = Path(self._tmp.name) / "profiles"
        self.parent.mkdir()

    def test_bare_dir_has_no_marker_files(self) -> None:
        pdir = build_profile_dir(
            self.parent, "work", parents=True, exist_ok=True, credentials=False
        )
        self.assertEqual(pdir, self.parent / "work")
        self.assertEqual(sorted(p.name for p in pdir.iterdir()), [])

    def test_credentials_only(self) -> None:
        pdir = build_profile_dir(
            self.parent, "work", parents=True, exist_ok=True, credentials=True
        )
        self.assertEqual(sorted(p.name for p in pdir.iterdir()), [".credentials.json"])
        self.assertEqual((pdir / ".credentials.json").read_text(), "{}")

    def test_settings_mapping_is_pretty_json_with_trailing_newline(self) -> None:
        settings = {"permissions": {"allow": ["Bash"]}}
        pdir = build_profile_dir(
            self.parent,
            "work",
            parents=True,
            exist_ok=True,
            credentials=True,
            settings=settings,
        )
        text = (pdir / "settings.json").read_text()
        self.assertEqual(text, json.dumps(settings, indent=2) + "\n")

    def test_settings_text_is_written_verbatim(self) -> None:
        pdir = build_profile_dir(
            self.parent,
            "work",
            parents=True,
            exist_ok=True,
            credentials=False,
            settings_text="{}",
        )
        self.assertEqual(sorted(p.name for p in pdir.iterdir()), ["settings.json"])
        self.assertEqual((pdir / "settings.json").read_text(), "{}")

    def test_both_settings_forms_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            build_profile_dir(
                self.parent,
                "work",
                parents=True,
                exist_ok=True,
                credentials=False,
                settings={},
                settings_text="{}",
            )

    def test_exist_ok_false_raises_on_second_build(self) -> None:
        build_profile_dir(
            self.parent, "work", parents=False, exist_ok=False, credentials=False
        )
        with self.assertRaises(FileExistsError):
            build_profile_dir(
                self.parent, "work", parents=False, exist_ok=False, credentials=False
            )

    def test_parents_false_raises_on_missing_parent(self) -> None:
        with self.assertRaises(FileNotFoundError):
            build_profile_dir(
                self.parent / "nope",
                "work",
                parents=False,
                exist_ok=False,
                credentials=False,
            )

    def test_default_file_modes_are_the_process_defaults(self) -> None:
        """No builder chmods: files land at the umask default, dirs at 0o755."""
        pdir = build_profile_dir(
            self.parent,
            "work",
            parents=True,
            exist_ok=True,
            credentials=True,
            settings_text="{}",
        )
        expected_file = 0o666 & ~_umask()
        self.assertEqual(pdir.stat().st_mode & 0o777, 0o777 & ~_umask())
        for name in (".credentials.json", "settings.json"):
            self.assertEqual((pdir / name).stat().st_mode & 0o777, expected_file)


def _umask() -> int:
    """Read the current umask without leaving it changed."""
    current = os.umask(0o022)
    os.umask(current)
    return current


if __name__ == "__main__":
    unittest.main()
