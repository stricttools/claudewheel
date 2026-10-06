"""Tests for the app-level flag routing in inject_launch().

main() injects the "launch" subcommand when the leading argv token is neither a
known subcommand nor an app-level flag. --dump-schema is a strictcli reserved
flag that must be handled at the app level -- strictcli refuses it there, naming
`claudewheel help --json`, while routed to "launch" it would only be reported as
an unknown launch flag. These tests assert all four routing cases by inspecting
how the injection logic rewrites argv.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import strictcli

from claudewheel import cli
from claudewheel.binaries import BinaryLocator
from claudewheel.workspace import Workspace
from tests.wheelhelpers import inject_launch


class InjectLaunchTests(unittest.TestCase):
    def test_dump_schema_not_rewritten(self) -> None:
        # (a) --dump-schema stays app-level (not routed to launch).
        argv = ["c", "--dump-schema"]
        self.assertEqual(inject_launch(argv), ["c", "--dump-schema"])

    def test_version_not_rewritten(self) -> None:
        # (b) --version / -v stay app-level.
        self.assertEqual(inject_launch(["c", "--version"]), ["c", "--version"])
        self.assertEqual(inject_launch(["c", "-v"]), ["c", "-v"])

    def test_help_not_rewritten(self) -> None:
        # --help / -h stay app-level.
        self.assertEqual(inject_launch(["c", "--help"]), ["c", "--help"])
        self.assertEqual(inject_launch(["c", "-h"]), ["c", "-h"])

    def test_launch_flags_rewritten_to_launch(self) -> None:
        # (c) launch segment flags still route to the launch subcommand.
        self.assertEqual(
            inject_launch(["c", "--profile", "work"]),
            ["c", "launch", "--profile", "work"],
        )
        self.assertEqual(
            inject_launch(["c", "--model", "opus"]),
            ["c", "launch", "--model", "opus"],
        )
        self.assertEqual(
            inject_launch(["c", "--mcp", "all"]),
            ["c", "launch", "--mcp", "all"],
        )

    def test_bare_profile_arg_rewritten_to_launch(self) -> None:
        # (d) a bare positional (a profile name) routes to launch.
        self.assertEqual(
            inject_launch(["c", "someprofile"]),
            ["c", "launch", "someprofile"],
        )

    def test_no_args_rewritten_to_launch(self) -> None:
        # No args at all -> launch the TUI.
        self.assertEqual(inject_launch(["c"]), ["c", "launch"])

    def test_known_subcommand_not_rewritten(self) -> None:
        # A genuine subcommand is left untouched.
        self.assertEqual(inject_launch(["c", "health"]), ["c", "health"])

    def test_every_registered_name_is_routed_as_itself(self) -> None:
        # Commands, groups, and deprecated names all come from the app itself.
        app = cli._build_app(Workspace.default(), BinaryLocator.default())
        schema = app.dump_schema_dict()
        names = [*schema["commands"], *schema["groups"], *schema["deprecated"]]
        self.assertIn("profile", names)
        self.assertIn("new-profile", names)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(inject_launch(["c", name]), ["c", name])

    def test_newly_registered_command_is_never_rewritten_to_launch(self) -> None:
        # A command registered on the app is routed as itself with no second
        # list to keep in step.
        app = cli._build_app(Workspace.default(), BinaryLocator.default())
        app.command(
            "brand-new-command",
            effect="read_only",
            help="a command registered only by this test, to prove routing follows the app",
        )(lambda: 0)
        names = cli._routing_names(app)
        self.assertEqual(
            cli._inject_launch(["c", "brand-new-command"], names),
            ["c", "brand-new-command"],
        )
        self.assertEqual(
            cli._inject_launch(["c", "--dry-run", "brand-new-command"], names),
            ["c", "--dry-run", "brand-new-command"],
        )

    def test_framework_commands_not_rewritten(self) -> None:
        # strictcli answers `help` and `version` itself; neither is in the
        # app's schema, so they are named apart from it.
        for argv in (
            ["c", "help"],
            ["c", "help", "--json"],
            ["c", "help", "mv"],
            ["c", "version"],
            ["c", "--dry-run", "help", "--json"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(inject_launch(argv), argv)

    def test_framework_commands_are_strictcli_s(self) -> None:
        # The routing names exactly the commands strictcli reserves for itself,
        # so a framework command a later strictcli adds fails here first.
        self.assertEqual(
            cli._FRAMEWORK_COMMANDS,
            strictcli._FRAMEWORK_COMMAND_NAMES,
        )

    def test_help_json_reaches_strictcli_help(self) -> None:
        # `claudewheel help --json` through main() prints the help document,
        # the same one the app's own help command prints, and exits 0.
        out, err = io.StringIO(), io.StringIO()
        code: object = 0
        with (
            mock.patch("sys.argv", ["claudewheel", "help", "--json"]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        self.assertIn(code, (0, None), err.getvalue())
        app = cli._build_app(Workspace.default(), BinaryLocator.default())
        expected = app.test(["help", "--json"])
        self.assertEqual(expected.exit_code, 0, expected.stderr)
        self.assertEqual(json.loads(out.getvalue()), json.loads(expected.stdout))
        envelope = json.loads(err.getvalue())
        self.assertEqual(envelope["command"], "help")

    def test_dump_schema_in_app_level_flags(self) -> None:
        # Guard the root-cause set directly so a future edit can't silently drop it.
        self.assertIn("--dump-schema", cli._APP_LEVEL_FLAGS)


if __name__ == "__main__":
    unittest.main()
