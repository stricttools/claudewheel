"""Tests for launch.py resolve_launch_config -- disallowed tools CLI flags."""

from __future__ import annotations

import unittest
from unittest import mock

from claudewheel.constants import CLAUDE_SYMLINK
from claudewheel.defaults import DISALLOWED_TOOLS
from claudewheel.launch import resolve_launch_config


class ResolveDisallowedToolsTests(unittest.TestCase):
    """Verify --disallowedTools flag and its tool list appear correctly in argv."""

    def _resolve(
        self,
        selections: dict | None = None,
        options_def: dict | None = None,
        default_flags: list[str] | None = None,
        extra_flags: list[str] | None = None,
    ) -> tuple[str, list[str], dict[str, str]]:
        """Call resolve_launch_config with fetch_gh_token mocked out."""
        if selections is None:
            selections = {"profile": None}
        if options_def is None:
            options_def = {}
        if default_flags is None:
            default_flags = []

        with mock.patch("claudewheel.launch.fetch_gh_token", return_value=None):
            return resolve_launch_config(
                selections, options_def, default_flags, extra_flags
            )

    def test_argv_includes_disallowed_tools_flag(self) -> None:
        """argv contains --disallowedTools and every tool in DISALLOWED_TOOLS."""
        _, argv, _ = self._resolve()

        self.assertIn("--disallowedTools", argv)
        for tool in DISALLOWED_TOOLS:
            self.assertIn(tool, argv)

    def test_disallowed_tools_follow_flag(self) -> None:
        """The tool names immediately follow --disallowedTools in argv order."""
        _, argv, _ = self._resolve()

        idx = argv.index("--disallowedTools")
        actual = argv[idx + 1 : idx + 1 + len(DISALLOWED_TOOLS)]
        self.assertEqual(actual, DISALLOWED_TOOLS)

    def test_disallowed_tools_after_other_flags(self) -> None:
        """--disallowedTools appears after default_flags and permission flags."""
        _, argv, _ = self._resolve(
            selections={"profile": None, "permissions": "bypass"},
            default_flags=["--verbose"],
        )

        idx_verbose = argv.index("--verbose")
        idx_bypass = argv.index("--dangerously-skip-permissions")
        idx_disallowed = argv.index("--disallowedTools")

        self.assertGreater(
            idx_disallowed, idx_verbose,
            "--disallowedTools should appear after --verbose",
        )
        self.assertGreater(
            idx_disallowed, idx_bypass,
            "--disallowedTools should appear after --dangerously-skip-permissions",
        )

    def test_argv_starts_with_binary_path(self) -> None:
        """When no version is selected, argv[0] is CLAUDE_SYMLINK."""
        _, argv, _ = self._resolve()
        self.assertEqual(argv[0], str(CLAUDE_SYMLINK))


if __name__ == "__main__":
    unittest.main()
