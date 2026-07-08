"""Tests for the app-level flag routing in cli._inject_launch().

main() injects the "launch" subcommand when the leading argv token is neither a
known subcommand nor an app-level flag. --dump-schema is a strictcli reserved
flag that must be handled at the app level -- if it were routed to "launch" the
schema dump would never fire. These tests assert all four routing cases by
inspecting how the injection logic rewrites argv.
"""

from __future__ import annotations

import unittest

from claudewheel import cli


class InjectLaunchTests(unittest.TestCase):
    def test_dump_schema_not_rewritten(self) -> None:
        # (a) --dump-schema stays app-level (not routed to launch).
        argv = ["c", "--dump-schema"]
        self.assertEqual(cli._inject_launch(argv), ["c", "--dump-schema"])

    def test_version_not_rewritten(self) -> None:
        # (b) --version / -v stay app-level.
        self.assertEqual(cli._inject_launch(["c", "--version"]), ["c", "--version"])
        self.assertEqual(cli._inject_launch(["c", "-v"]), ["c", "-v"])

    def test_help_not_rewritten(self) -> None:
        # --help / -h stay app-level.
        self.assertEqual(cli._inject_launch(["c", "--help"]), ["c", "--help"])
        self.assertEqual(cli._inject_launch(["c", "-h"]), ["c", "-h"])

    def test_launch_flags_rewritten_to_launch(self) -> None:
        # (c) launch segment flags still route to the launch subcommand.
        self.assertEqual(
            cli._inject_launch(["c", "--profile", "work"]),
            ["c", "launch", "--profile", "work"],
        )
        self.assertEqual(
            cli._inject_launch(["c", "--model", "opus"]),
            ["c", "launch", "--model", "opus"],
        )
        self.assertEqual(
            cli._inject_launch(["c", "--mcp", "all"]),
            ["c", "launch", "--mcp", "all"],
        )

    def test_bare_profile_arg_rewritten_to_launch(self) -> None:
        # (d) a bare positional (a profile name) routes to launch.
        self.assertEqual(
            cli._inject_launch(["c", "someprofile"]),
            ["c", "launch", "someprofile"],
        )

    def test_no_args_rewritten_to_launch(self) -> None:
        # No args at all -> launch the TUI.
        self.assertEqual(cli._inject_launch(["c"]), ["c", "launch"])

    def test_known_subcommand_not_rewritten(self) -> None:
        # A genuine subcommand is left untouched.
        self.assertEqual(cli._inject_launch(["c", "health"]), ["c", "health"])

    def test_dump_schema_in_app_level_flags(self) -> None:
        # Guard the root-cause set directly so a future edit can't silently drop it.
        self.assertIn("--dump-schema", cli._APP_LEVEL_FLAGS)


if __name__ == "__main__":
    unittest.main()
