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

from claudewheel import cli


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
    """Minimal ConfigManager stand-in -- only the attributes touched by tested helpers."""

    def __init__(
        self,
        config: dict,
        segments_def: list[dict],
        state: dict,
        options_def: dict | None = None,
    ) -> None:
        self.config = config
        self.segments_def = segments_def
        self.state = state
        self.options_def = options_def if options_def is not None else {}


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


class PrintModeTests(unittest.TestCase):
    """Tests for the print-mode (-p) branch in main()."""

    # Full segment definitions matching DEFAULT_SEGMENTS
    SEGMENTS_DEF = [
        {"key": "profile", "label": "Profile", "required": True, "print_mode": True},
        {"key": "github", "label": "GH", "required": True, "print_mode": False},
        {"key": "version", "label": "Ver", "required": True, "print_mode": True},
        {"key": "model", "label": "Model", "required": False, "print_mode": True},
        {"key": "directory", "label": "Dir", "required": True, "print_mode": True},
        {"key": "mcp", "label": "MCP", "required": False, "print_mode": False},
        {"key": "permissions", "label": "Perms", "required": False, "print_mode": False},
    ]

    ALL_ENABLED = ["profile", "github", "version", "model", "directory", "mcp", "permissions"]

    FULL_LAST_CONFIG = {
        "profile": "personal",
        "github": "ghuser",
        "version": "2.1.116",
        "model": "claude-opus-4-6",
        "directory": "/home/user/project",
        "mcp": "default",
        "permissions": "bypass",
    }

    def _make_cfg(self, last_config: dict | None = None) -> _FakeCfg:
        """Build a _FakeCfg pre-loaded with all segments enabled."""
        return _FakeCfg(
            config={
                "theme": "dark",
                "enabled_segments": list(self.ALL_ENABLED),
                "default_flags": [],
                "health_check_on_launch": False,
            },
            segments_def=list(self.SEGMENTS_DEF),
            state={
                "last_config": dict(last_config) if last_config is not None else {},
                "recent_dirs": [],
                "launch_count": 0,
            },
            options_def={},
        )

    def _run_main(self, argv: list[str], last_config: dict | None = None) -> mock.MagicMock:
        """Invoke cli.main() with patched argv, ConfigManager, and _do_launch_sequence.

        Returns the mock for _do_launch_sequence so callers can inspect call args.
        """
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()

        with (
            mock.patch("sys.argv", argv),
            mock.patch("claudewheel.cli.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
        ):
            cli.main()

        return launch_mock

    # -- 1. Print mode filters out non-print segments --

    def test_print_mode_filters_non_print_segments(self) -> None:
        launch_mock = self._run_main(
            ["c", "-p", "test"],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        merged = launch_mock.call_args[1].get("selections") or launch_mock.call_args[0][1]

        # print_mode: True segments must be present
        for key in ("profile", "version", "model", "directory"):
            self.assertIn(key, merged, f"expected print_mode segment '{key}' in merged")

        # print_mode: False segments must NOT be present
        for key in ("github", "mcp", "permissions"):
            self.assertNotIn(key, merged, f"non-print segment '{key}' should be filtered out")

    # -- 2. Print mode sets interactive=False --

    def test_print_mode_sets_interactive_false(self) -> None:
        launch_mock = self._run_main(
            ["c", "-p", "test"],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        self.assertFalse(kwargs["interactive"])

    # -- 3. Non-print skip_tui sets interactive=True --

    def test_non_print_skip_tui_sets_interactive_true(self) -> None:
        """When all required segments are provided via flags (no -p), interactive defaults to True."""
        launch_mock = self._run_main(
            [
                "c",
                "--profile", "personal",
                "--github", "ghuser",
                "-s", "version=2.1.116",
                "--directory", "/some/dir",
            ],
            last_config={},
        )
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        self.assertTrue(kwargs["interactive"])

    # -- 4. Print mode adds --print to extra_flags --

    def test_print_mode_adds_print_flag(self) -> None:
        launch_mock = self._run_main(
            ["c", "-p", "test prompt"],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        extra_flags = kwargs["extra_flags"]
        self.assertIn("--print", extra_flags)
        self.assertIn("test prompt", extra_flags)
        # The two should be adjacent
        idx = extra_flags.index("--print")
        self.assertEqual(extra_flags[idx + 1], "test prompt")

    # -- 5. Print mode warns on missing required segments --

    def test_print_mode_warns_on_missing_required_segments(self) -> None:
        """Empty last_config + no overrides -> warns about profile and version."""
        err = io.StringIO()
        fake_cfg = self._make_cfg(last_config={})
        launch_mock = mock.MagicMock()

        with (
            mock.patch("sys.argv", ["c", "-p", "test"]),
            mock.patch("claudewheel.cli.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
            redirect_stderr(err),
        ):
            cli.main()

        warning = err.getvalue()
        # directory auto-fills from cwd, so only profile and version should be warned
        self.assertIn("profile", warning)
        self.assertIn("version", warning)
        self.assertNotIn("directory", warning)

    # -- 6. Print mode no warning when segments are present --

    def test_print_mode_no_warning_when_segments_present(self) -> None:
        err = io.StringIO()
        fake_cfg = self._make_cfg(last_config=self.FULL_LAST_CONFIG)
        launch_mock = mock.MagicMock()

        with (
            mock.patch("sys.argv", ["c", "-p", "test"]),
            mock.patch("claudewheel.cli.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
            redirect_stderr(err),
        ):
            cli.main()

        self.assertEqual(err.getvalue(), "", "no warning expected when all segments are present")

    # -- 7. Passthrough args after -- work --

    def test_passthrough_args_after_double_dash(self) -> None:
        launch_mock = self._run_main(
            ["c", "-p", "test", "--", "--output-format", "json"],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        extra_flags = kwargs["extra_flags"]
        # Should contain both --print args and passthrough args
        self.assertEqual(extra_flags, ["--print", "test", "--output-format", "json"])


if __name__ == "__main__":
    unittest.main()
