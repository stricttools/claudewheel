"""Tests for the SharedStore path owner and its path codec."""

from __future__ import annotations

import unittest
from pathlib import Path

from claudewheel.shared_store import LIFECYCLE_DIRNAME, SharedStore


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


# Expected Claude-Code-style encodings, pinned inline (among other characters,
# the codec replaces every "/", "." and "_" with "-"). Formerly asserted by
# parity against the now-deleted constants.encode_path.
_CODEC_EXPECTATIONS = {
    "/": "-",
    "/home/m": "-home-m",
    "/home/m/Projects/claudewheel": "-home-m-Projects-claudewheel",
    "/home/m/.config/some.app/v1.2.3": "-home-m--config-some-app-v1-2-3",
    "/home/m/my-project_dir/sub.dir": "-home-m-my-project-dir-sub-dir",
    "/a/b.c/d-e_f/.hidden": "-a-b-c-d-e-f--hidden",
    "relative/path.here": "relative-path-here",
    "no-slash-just.dots": "no-slash-just-dots",
    "/trailing/slash/": "-trailing-slash-",
    "/multiple..dots...here": "-multiple--dots---here",
    "": "",
}


class EncodePathTests(unittest.TestCase):
    """SharedStore.encode_path replaces /, . and _ with - (pinned expectations)."""

    def test_encodes_representative_paths(self) -> None:
        for case in _CODEC_CASES:
            self.assertEqual(
                SharedStore.encode_path(case),
                _CODEC_EXPECTATIONS[case],
                msg=f"codec mismatch for {case!r}",
            )

    def test_underscores_collapse_to_dashes(self) -> None:
        # Claude Code encodes "_" as "-" too; a codec that kept the underscore
        # produced names that never exist on disk.
        self.assertEqual(
            SharedStore.encode_path("/home/m/my_project"), "-home-m-my-project"
        )
        self.assertEqual(SharedStore.encode_path("a_b_c"), "a-b-c")


# Expected names derived by running Claude Code 2.1.281's own project-dir
# sanitizer (its functions copied verbatim out of the installed binary and
# executed under node): every UTF-16 code unit outside [a-zA-Z0-9] becomes "-",
# and a result longer than 200 characters is cut to 200 and suffixed with "-"
# plus the base-36 absolute value of a 32-bit string hash of the raw path.
_LONG_PATH = "/home/m/Projects/" + "deep/" * 45 + "leaf"
_LONG_PATH_NAME = "-home-m-Projects-" + "deep-" * 36 + "dee-bf0agf"


class ClaudeCodeSanitizerTests(unittest.TestCase):
    """encode_path produces the store-dir name Claude Code itself creates."""

    def test_space_becomes_a_dash(self) -> None:
        self.assertEqual(
            SharedStore.encode_path("/home/m/Projects/my project"),
            "-home-m-Projects-my-project",
        )

    def test_at_sign_and_plus_become_dashes(self) -> None:
        self.assertEqual(
            SharedStore.encode_path("/home/m/Projects/a@b+c"),
            "-home-m-Projects-a-b-c",
        )

    def test_each_utf16_code_unit_outside_ascii_alnum_becomes_one_dash(self) -> None:
        # "\u00e9" is one UTF-16 code unit, the emoji is a surrogate pair (two).
        self.assertEqual(
            SharedStore.encode_path("/home/m/Projects/caf\u00e9/x\U0001f600y"),
            "-home-m-Projects-caf--x--y",
        )

    def test_over_long_path_is_truncated_and_hash_suffixed(self) -> None:
        self.assertEqual(SharedStore.encode_path(_LONG_PATH), _LONG_PATH_NAME)

    def test_two_hundred_characters_are_kept_whole(self) -> None:
        path = "/" + "a" * 199
        self.assertEqual(SharedStore.encode_path(path), "-" + "a" * 199)


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

    def test_lifecycle_is_not_a_profile_subdir(self) -> None:
        # The lifecycle store is machine-wide: one file per session, whichever
        # profile launched it. SHARED_SUBDIRS is the list of directories
        # symlinked INTO each profile, and it must not acquire this one.
        self.assertNotIn(LIFECYCLE_DIRNAME, SharedStore.SHARED_SUBDIRS)

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

    def test_lifecycle_dir(self) -> None:
        self.assertEqual(self.store.lifecycle_dir, self.shared / LIFECYCLE_DIRNAME)
        self.assertEqual(self.store.lifecycle_dir, self.shared / "lifecycle")

    def test_subdir(self) -> None:
        self.assertEqual(self.store.subdir("tasks"), self.shared / "tasks")

    def test_skills_dir_field(self) -> None:
        self.assertEqual(self.store.skills_dir, self.skills)

    def test_frozen(self) -> None:
        with self.assertRaises(Exception):
            self.store.shared_dir = Path("/other")  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
