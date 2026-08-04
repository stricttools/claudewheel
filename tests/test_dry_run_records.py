"""``--dry-run`` records; it never touches ``~/.claudewheel/``.

The claim the whole effects migration exists to make: a preview of a
profile-mutating command writes NOTHING and still tells the operator exactly
what it would have written.  These tests drive the real CLI entry point --
argv in, ``main()`` -- against a sandboxed config directory, then assert on
both halves: the sandbox is byte-identical afterwards, and the framework's
would-do log names the files.

They are deliberately end-to-end rather than unit tests of the chokepoint. A
chokepoint that records correctly but is never reached is exactly the failure
mode this file is here to catch.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli


def _tree(root: Path) -> dict[str, bytes]:
    """Every regular file under *root*, keyed by relative path."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


class DryRunRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.launcher = Path(self._tmp.name) / "cw"
        (self.launcher / "profiles" / "work").mkdir(parents=True)
        self.settings = self.launcher / "profiles" / "work" / "settings.json"
        self.settings.write_text(
            json.dumps({"permissions": {"deny": ["Bash(bogus:*)"]}}, indent=2)
        )
        patcher = mock.patch.dict(
            "os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(self.launcher)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv: list[str]) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit:
                pass
        return out.getvalue(), err.getvalue()

    def test_reconcile_permissions_dry_run_writes_nothing(self) -> None:
        """The drifted profile is byte-identical after a preview."""
        before = _tree(self.launcher)
        stdout, _ = self._run(["c", "reconcile-permissions", "--dry-run"])
        self.assertEqual(_tree(self.launcher), before)
        # And the deny rule that a real run would prune is still there.
        data = json.loads(self.settings.read_text())
        self.assertIn("Bash(bogus:*)", data["permissions"]["deny"])
        self.assertIn("DRY RUN", stdout)

    def test_reconcile_permissions_dry_run_names_the_write(self) -> None:
        """The would-do log lists the settings file the run would rewrite."""
        stdout, _ = self._run(["c", "reconcile-permissions", "--dry-run"])
        self.assertIn("DRY RUN — no changes were made. Would do:", stdout)
        self.assertIn(f"write: {self.settings}", stdout)

    def test_deploy_hooks_dry_run_writes_nothing(self) -> None:
        """A preview of deploy-hooks creates no scripts directory at all."""
        before = _tree(self.launcher)
        stdout, _ = self._run(["c", "deploy-hooks", "--all", "--dry-run"])
        self.assertEqual(_tree(self.launcher), before)
        self.assertFalse((self.launcher / "scripts").exists())
        self.assertIn("mkdir: ", stdout)
        self.assertIn("chmod: ", stdout)

    def test_read_only_command_never_prompts_and_logs_an_empty_body(self) -> None:
        """A read_only command accepts --dry-run and records nothing.

        Its log is header-only by construction: read-only commands produce no
        recorded effects, so there is nothing for the body to list.
        """
        stdout, stderr = self._run(["c", "versions", "--dry-run"])
        self.assertNotIn("Proceed?", stderr)
        self.assertIn("DRY RUN — no changes were made. Would do:", stdout)
        self.assertNotIn("write:", stdout)


class RealConfigDirIsAbsentTests(unittest.TestCase):
    """The suite cannot see the developer's real ``~/.claudewheel/``.

    stricttest repoints ``HOME`` at a throwaway directory before any conftest
    module is imported, so ``Workspace.default()`` -- which derives every path
    from ``Path.home()`` -- resolves into the sandbox. This pins that: if the
    floor is ever removed or misconfigured, the whole suite's blast radius goes
    back to the real profile store, and this is the test that says so.
    """

    def test_home_is_a_throwaway(self) -> None:
        home = Path(os.environ["HOME"])
        self.assertNotEqual(home, Path("/home/m"), "HOME is the real home")
        self.assertIn("stricttest", str(home).lower())

    def test_workspace_default_resolves_inside_the_throwaway_home(self) -> None:
        from claudewheel.workspace import Workspace

        env = dict(os.environ)
        env.pop("CLAUDEWHEEL_CONFIG_DIR", None)
        with mock.patch.dict("os.environ", env, clear=True):
            ws = Workspace.default()
        self.assertTrue(
            str(ws.root).startswith(os.environ["HOME"]),
            f"workspace root {ws.root} escapes the throwaway HOME",
        )

    def test_the_real_claudewheel_store_is_unreachable(self) -> None:
        """Nothing in the sandbox HOME is the developer's profile store."""
        self.assertFalse((Path(os.environ["HOME"]) / ".claudewheel" / "tokens.json").exists())


if __name__ == "__main__":
    unittest.main()
