"""Tests for the cli.py one-shot handlers (--uninstall, --reset-options, --show).

These tests exercise the small standalone helpers `_do_uninstall`,
`_do_reset_options`, and `_do_show`, which are invoked by main() but easy to
unit-test in isolation -- avoiding the need to mock argparse + sys.exit.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claude_launcher import cli


class DoUninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.versions_dir = Path(self._tmp.name) / "versions"
        self.versions_dir.mkdir()
        # Sibling path the symlink can live at, kept off the real filesystem
        self.symlink_path = Path(self._tmp.name) / "claude"

        # Redirect VERSIONS_DIR and CLAUDE_SYMLINK to tmp paths
        self._patch_versions = mock.patch.object(cli, "VERSIONS_DIR", self.versions_dir)
        self._patch_symlink = mock.patch.object(cli, "CLAUDE_SYMLINK", self.symlink_path)
        self._patch_versions.start()
        self._patch_symlink.start()
        self.addCleanup(self._patch_versions.stop)
        self.addCleanup(self._patch_symlink.stop)

    def test_uninstall_removes_file(self) -> None:
        """A non-symlinked installed version is deleted and exit code is 0."""
        target = self.versions_dir / "2.1.999"
        target.write_bytes(b"fake binary")
        self.assertTrue(target.exists())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_uninstall("2.1.999")

        self.assertEqual(rc, 0)
        self.assertFalse(target.exists())
        self.assertIn("Uninstalled 2.1.999", buf.getvalue())

    def test_uninstall_refuses_active_symlink(self) -> None:
        """When the symlink resolves to the requested version, uninstall is refused."""
        target = self.versions_dir / "2.1.999"
        target.write_bytes(b"fake binary")
        # Create the symlink pointing at this version
        os.symlink(target, self.symlink_path)

        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._do_uninstall("2.1.999")

        self.assertNotEqual(rc, 0)
        self.assertTrue(target.exists(), "file should not be deleted when symlink points to it")
        msg = err.getvalue()
        self.assertIn("Refusing to uninstall", msg)
        self.assertIn("2.1.999", msg)

    def test_uninstall_missing_version_fails(self) -> None:
        """Uninstalling a version that doesn't exist returns non-zero with stderr message."""
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._do_uninstall("0.0.0")

        self.assertNotEqual(rc, 0)
        msg = err.getvalue()
        self.assertIn("0.0.0", msg)
        self.assertIn("not installed", msg)

    def test_uninstall_succeeds_when_symlink_points_elsewhere(self) -> None:
        """Symlink targeting a different version doesn't block uninstalling a sibling."""
        keeper = self.versions_dir / "2.1.999"
        keeper.write_bytes(b"keeper")
        victim = self.versions_dir / "2.1.998"
        victim.write_bytes(b"victim")
        os.symlink(keeper, self.symlink_path)

        with redirect_stdout(io.StringIO()):
            rc = cli._do_uninstall("2.1.998")

        self.assertEqual(rc, 0)
        self.assertFalse(victim.exists())
        self.assertTrue(keeper.exists())


class DoResetOptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.options_file = Path(self._tmp.name) / "options.json"

        self._patch = mock.patch.object(cli, "OPTIONS_FILE", self.options_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_reset_options_deletes_file(self) -> None:
        """An existing options.json is deleted and rc is 0."""
        self.options_file.write_text("{}\n")
        self.assertTrue(self.options_file.exists())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_reset_options()

        self.assertEqual(rc, 0)
        self.assertFalse(self.options_file.exists())
        self.assertIn("Deleted", buf.getvalue())

    def test_reset_options_handles_missing_file(self) -> None:
        """Missing options.json is not an error; rc is 0 with informative message."""
        self.assertFalse(self.options_file.exists())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_reset_options()

        self.assertEqual(rc, 0)
        self.assertIn("does not exist", buf.getvalue())


class _FakeCfg:
    """Minimal ConfigManager stand-in for _do_show -- only the attributes it touches."""

    def __init__(self, config: dict, segments_def: list[dict], state: dict) -> None:
        self.config = config
        self.segments_def = segments_def
        self.state = state


class DoShowTests(unittest.TestCase):
    def test_show_prints_summary(self) -> None:
        """The show output contains segment labels, last_config values, theme, flags, recents."""
        cfg = _FakeCfg(
            config={
                "theme": "dark",
                "enabled_segments": ["profile", "version", "model"],
                "default_flags": ["--strict-mcp-config", "--dangerously-skip-permissions"],
                "health_check_on_launch": True,
            },
            segments_def=[
                {"key": "profile", "label": "Profile"},
                {"key": "version", "label": "Ver"},
                {"key": "model", "label": "Model"},
                # mcp is defined but disabled -- should NOT appear
                {"key": "mcp", "label": "MCP"},
            ],
            state={
                "last_config": {
                    "profile": "personal",
                    "version": "2.1.116",
                    # model intentionally missing -> "<unset>"
                },
                "recent_dirs": ["~/a", "~/b", "~/c", "~/d", "~/e", "~/f"],
                "launch_count": 12,
            },
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_show(cfg)
        out = buf.getvalue()

        self.assertEqual(rc, 0)
        # Segment labels and values
        self.assertIn("Profile:", out)
        self.assertIn("personal", out)
        self.assertIn("Ver:", out)
        self.assertIn("2.1.116", out)
        self.assertIn("Model:", out)
        self.assertIn("<unset>", out)
        # Disabled segment must be hidden
        self.assertNotIn("MCP:", out)
        # Header / general state
        self.assertIn("claudewheel state:", out)
        self.assertIn("Theme: dark", out)
        self.assertIn("--strict-mcp-config", out)
        self.assertIn("Health check on launch: True", out)
        # Recent dirs: 5 of 6 shown
        self.assertIn("Recent dirs (5 of 6)", out)
        self.assertIn("~/a", out)
        self.assertIn("~/e", out)
        # 6th entry truncated
        self.assertNotIn("~/f", out)
        # Launch count
        self.assertIn("Launch count: 12", out)

    def test_show_handles_empty_state(self) -> None:
        """No recents and empty last_config still produces a valid summary."""
        cfg = _FakeCfg(
            config={
                "theme": "light",
                "enabled_segments": ["profile"],
                "default_flags": [],
                "health_check_on_launch": False,
            },
            segments_def=[{"key": "profile", "label": "Profile"}],
            state={"last_config": {}, "recent_dirs": [], "launch_count": 0},
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_show(cfg)
        out = buf.getvalue()

        self.assertEqual(rc, 0)
        self.assertIn("<unset>", out)
        self.assertIn("Theme: light", out)
        self.assertIn("Default flags: <none>", out)
        self.assertIn("Recent dirs: <none>", out)
        self.assertIn("Launch count: 0", out)


if __name__ == "__main__":
    unittest.main()
