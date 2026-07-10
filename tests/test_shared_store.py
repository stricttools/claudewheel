"""Tests for the SharedStore path owner and its path codec parity with constants."""

from __future__ import annotations

import unittest
from pathlib import Path

from claudewheel import constants
from claudewheel.shared_store import SharedStore


# Representative paths exercising the codec: absolute, nested, dotfiles,
# dashes, underscores, dots inside segments, trailing slashes, and relatives.
_CODEC_CASES = [
    "/",
    "/home/m",
    "/home/m/Projects/claudewheel",
    "/home/m/.config/some.app/v1.2.3",
    "/home/m/my-project_dir/sub.dir",
    "/a/b.c/d-e_f/.hidden",
    "relative/path.here",
    "no-slash-just.dots",
    "/trailing/slash/",
    "/multiple..dots...here",
    "",
]


class EncodePathParityTests(unittest.TestCase):
    """SharedStore.encode_path must match constants.encode_path exactly."""

    def test_parity_across_representative_paths(self) -> None:
        for case in _CODEC_CASES:
            self.assertEqual(
                SharedStore.encode_path(case),
                constants.encode_path(case),
                msg=f"codec mismatch for {case!r}",
            )


class SharedSubdirsTests(unittest.TestCase):
    """SHARED_SUBDIRS must mirror constants.PROFILE_SHARED_DIRS exactly."""

    def test_subdirs_match_constants(self) -> None:
        self.assertEqual(
            list(SharedStore.SHARED_SUBDIRS),
            constants.PROFILE_SHARED_DIRS,
        )

    def test_no_constants_import(self) -> None:
        # Guard: shared_store must remain a leaf that does not import constants.
        import claudewheel.shared_store as ss_mod

        self.assertFalse(hasattr(ss_mod, "constants"))


class PathPropertyTests(unittest.TestCase):
    """Path properties resolve relative to shared_dir."""

    def setUp(self) -> None:
        self.shared = Path("/tmp/wheel/shared")
        self.skills = Path("/tmp/wheel/skills")
        self.store = SharedStore(shared_dir=self.shared, skills_dir=self.skills)

    def test_projects_dir(self) -> None:
        self.assertEqual(self.store.projects_dir, self.shared / "projects")

    def test_inodes_file(self) -> None:
        self.assertEqual(self.store.inodes_file, self.shared / "inodes.json")

    def test_subdir(self) -> None:
        self.assertEqual(self.store.subdir("tasks"), self.shared / "tasks")

    def test_skills_dir_field(self) -> None:
        self.assertEqual(self.store.skills_dir, self.skills)

    def test_frozen(self) -> None:
        with self.assertRaises(Exception):
            self.store.shared_dir = Path("/other")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
