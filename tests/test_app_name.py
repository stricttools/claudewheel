"""The app's name is the installed command's name, and every rendering uses it.

The command a user types is ``claudewheel``: the console script declared in
pyproject.toml and the npm package's ``bin`` entry. The strictcli App's own
name is what help headers, ``--version``, parse errors, and the ``--json``
envelope print, so it has to be the same string. argv[0] plays no part in it:
strictcli ignores the program name it was invoked under.
"""

from __future__ import annotations

import io
import json
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.binaries import BinaryLocator
from claudewheel.workspace import Workspace

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _app_name() -> str:
    return cli._build_app(Workspace.default(), BinaryLocator.default()).name


def _run(argv: list[str]) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main()
        except SystemExit:
            pass
    return out.getvalue(), err.getvalue()


class AppNameMatchesTheInstalledCommandTests(unittest.TestCase):
    def test_name_is_the_pyproject_console_script(self) -> None:
        pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
        self.assertEqual(list(pyproject["project"]["scripts"]), [_app_name()])

    def test_name_is_the_npm_bin_entry(self) -> None:
        package = json.loads((_REPO_ROOT / "package.json").read_text())
        self.assertEqual(list(package["bin"]), [_app_name()])


class RenderingsNameTheCommandTests(unittest.TestCase):
    def test_help_header_and_footer_name_claudewheel(self) -> None:
        out, _ = _run(["c", "--help"])
        lines = out.strip().splitlines()
        self.assertTrue(lines[0].startswith("claudewheel v"), lines[0])
        self.assertEqual(
            lines[-1], "Use 'claudewheel help <command>' for more information."
        )

    def test_command_help_names_claudewheel(self) -> None:
        out, _ = _run(["c", "mv", "--help"])
        self.assertTrue(out.startswith("claudewheel mv -- "), out.splitlines()[0])

    def test_version_names_claudewheel(self) -> None:
        out, _ = _run(["c", "--version"])
        self.assertRegex(out.strip(), r"^claudewheel \d+\.\d+\.\d+")

    def test_parse_error_names_claudewheel(self) -> None:
        _, err = _run(["c", "mv"])
        self.assertIn("try 'claudewheel mv --help'", err)


if __name__ == "__main__":
    unittest.main()
