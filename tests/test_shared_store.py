"""Tests for the SharedStore path owner and its path codec."""

from __future__ import annotations

import unittest
from pathlib import Path

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


# Expected Claude-Code-style encodings, pinned inline (the codec replaces
# every "/" and "." with "-"). Formerly asserted by parity against the
# now-deleted constants.encode_path.
_CODEC_EXPECTATIONS = {
    "/": "-",
    "/home/m": "-home-m",
    "/home/m/Projects/claudewheel": "-home-m-Projects-claudewheel",
    "/home/m/.config/some.app/v1.2.3": "-home-m--config-some-app-v1-2-3",
    "/home/m/my-project_dir/sub.dir": "-home-m-my-project_dir-sub-dir",
    "/a/b.c/d-e_f/.hidden": "-a-b-c-d-e_f--hidden",
    "relative/path.here": "relative-path-here",
    "no-slash-just.dots": "no-slash-just-dots",
    "/trailing/slash/": "-trailing-slash-",
    "/multiple..dots...here": "-multiple--dots---here",
    "": "",
}


class EncodePathTests(unittest.TestCase):
    """SharedStore.encode_path replaces every / and . with - (pinned expectations)."""

    def test_encodes_representative_paths(self) -> None:
        for case in _CODEC_CASES:
            self.assertEqual(
                SharedStore.encode_path(case),
                _CODEC_EXPECTATIONS[case],
                msg=f"codec mismatch for {case!r}",
            )


class SharedSubdirsTests(unittest.TestCase):
    """SHARED_SUBDIRS pins the profile shared-store subdirectory list."""

    def test_subdirs_are_the_pinned_set(self) -> None:
        self.assertEqual(
            list(SharedStore.SHARED_SUBDIRS),
            [
                "projects",
                "session-env",
                "file-history",
                "tasks",
                "todos",
                "paste-cache",
            ],
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
