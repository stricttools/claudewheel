"""Tests for the deploy-hooks CLI command."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.hook_scripts import HOOK_SCRIPTS


class DeployHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scripts_dir = Path(self._tmp.name) / "scripts"
        # Patch SCRIPTS_DIR in both cli and hook_scripts modules
        self._patch_cli = mock.patch.object(cli, "SCRIPTS_DIR", self.scripts_dir)
        self._patch_cli.start()
        self.addCleanup(self._patch_cli.stop)

    def _run_deploy(self, argv: list[str]) -> tuple[str, str, bool]:
        """Run deploy-hooks with the given argv.

        Returns (stdout, stderr, exited).
        """
        out = io.StringIO()
        err = io.StringIO()
        exited = False
        with (
            mock.patch("sys.argv", argv),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit:
                exited = True
        return out.getvalue(), err.getvalue(), exited

    def test_deploy_all_creates_all_scripts(self) -> None:
        """deploy-hooks --all creates every known script."""
        stdout, _, _ = self._run_deploy(["c", "deploy-hooks", "--all"])

        for name in HOOK_SCRIPTS:
            dest = self.scripts_dir / name
            self.assertTrue(dest.exists(), f"{name} should exist after --all")
            self.assertIn(f"created: {dest}", stdout)

    def test_deploy_single_creates_one_script(self) -> None:
        """deploy-hooks <name> creates only the named script."""
        name = "hook-timestamp"
        stdout, _, _ = self._run_deploy(["c", "deploy-hooks", name])

        dest = self.scripts_dir / name
        self.assertTrue(dest.exists(), f"{name} should exist")
        self.assertIn(f"created: {dest}", stdout)
        # Other scripts should not exist
        for other in HOOK_SCRIPTS:
            if other != name:
                self.assertFalse(
                    (self.scripts_dir / other).exists(),
                    f"{other} should NOT exist when deploying only {name}",
                )

    def test_deploy_skips_existing(self) -> None:
        """deploy-hooks <name> skips if the script already exists."""
        name = "hook-timestamp"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        existing = self.scripts_dir / name
        existing.write_text("existing content")

        stdout, _, _ = self._run_deploy(["c", "deploy-hooks", name])

        self.assertIn(f"already exists: {existing}", stdout)
        # Content must be preserved (not overwritten)
        self.assertEqual(existing.read_text(), "existing content")

    def test_deploy_unknown_name_errors(self) -> None:
        """deploy-hooks with an unknown script name prints an error."""
        _, stderr, exited = self._run_deploy(["c", "deploy-hooks", "nonexistent"])

        self.assertTrue(exited, "should exit with error")
        self.assertIn("unknown hook script", stderr)
        self.assertIn("nonexistent", stderr)

    def test_scripts_are_executable(self) -> None:
        """Deployed scripts have the executable bit set."""
        self._run_deploy(["c", "deploy-hooks", "--all"])

        for name in HOOK_SCRIPTS:
            dest = self.scripts_dir / name
            mode = dest.stat().st_mode
            self.assertTrue(
                mode & stat.S_IXUSR,
                f"{name} should be owner-executable (mode={oct(mode)})",
            )

    def test_no_args_no_all_errors(self) -> None:
        """deploy-hooks with neither a name nor --all prints an error."""
        _, stderr, exited = self._run_deploy(["c", "deploy-hooks"])

        self.assertTrue(exited, "should exit with error")
        self.assertIn("provide a script name or --all", stderr)

    def test_name_and_all_mutually_exclusive(self) -> None:
        """deploy-hooks <name> --all is rejected."""
        _, stderr, exited = self._run_deploy(
            ["c", "deploy-hooks", "hook-timestamp", "--all"]
        )

        self.assertTrue(exited, "should exit with error")
        self.assertIn("mutually exclusive", stderr)

    def test_creates_scripts_dir_if_missing(self) -> None:
        """SCRIPTS_DIR is created automatically when it doesn't exist."""
        self.assertFalse(self.scripts_dir.exists())
        self._run_deploy(["c", "deploy-hooks", "hook-timestamp"])
        self.assertTrue(self.scripts_dir.exists())

    def test_script_content_matches_registry(self) -> None:
        """Deployed script content matches the registry template."""
        name = "hook-timestamp"
        self._run_deploy(["c", "deploy-hooks", name])

        dest = self.scripts_dir / name
        self.assertEqual(dest.read_text(), HOOK_SCRIPTS[name])


if __name__ == "__main__":
    unittest.main()
