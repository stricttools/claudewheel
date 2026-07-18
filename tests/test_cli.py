"""Tests for the cli.py one-shot handlers (--uninstall, --reset-options, --show).

These tests exercise the small standalone helpers `_do_uninstall`,
`_do_reset_options`, and `_do_show`, which are invoked by main() but easy to
unit-test in isolation -- avoiding the need to mock argparse + sys.exit.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from unittest import mock
from unittest.mock import MagicMock

from claudewheel import cli
from claudewheel.config import AppConfigStore

if TYPE_CHECKING:
    from claudewheel.profile_info import ProfileReport
    from claudewheel.profile_store import DeletionResult, Profile


class DoUninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.versions_dir = Path(self._tmp.name) / "versions"
        self.versions_dir.mkdir()
        # Sibling path the symlink can live at, kept off the real filesystem
        self.symlink_path = Path(self._tmp.name) / "claude"

        # A locator pointing at the tmp version/symlink paths.
        from claudewheel.binaries import BinaryLocator

        self.locator = BinaryLocator(
            versions_dir=self.versions_dir, claude_symlink=self.symlink_path
        )

    def test_uninstall_removes_file(self) -> None:
        """A non-symlinked installed version is deleted and exit code is 0."""
        target = self.versions_dir / "2.1.999"
        target.write_bytes(b"fake binary")
        self.assertTrue(target.exists())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_uninstall(self.locator, "2.1.999")

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
            rc = cli._do_uninstall(self.locator, "2.1.999")

        self.assertNotEqual(rc, 0)
        self.assertTrue(
            target.exists(), "file should not be deleted when symlink points to it"
        )
        msg = err.getvalue()
        self.assertIn("Refusing to uninstall", msg)
        self.assertIn("2.1.999", msg)

    def test_uninstall_missing_version_fails(self) -> None:
        """Uninstalling a version that doesn't exist returns non-zero with stderr message."""
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._do_uninstall(self.locator, "0.0.0")

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
            rc = cli._do_uninstall(self.locator, "2.1.998")

        self.assertEqual(rc, 0)
        self.assertFalse(victim.exists())
        self.assertTrue(keeper.exists())


class DoResetOptionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            Path(self._tmp.name), claude_dir=Path(self._tmp.name) / ".claude"
        )
        self.options_file = self.ws.options_file

    def test_reset_options_deletes_file(self) -> None:
        """An existing options.json is deleted and rc is 0."""
        self.options_file.write_text("{}\n")
        self.assertTrue(self.options_file.exists())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_reset_options(self.ws)

        self.assertEqual(rc, 0)
        self.assertFalse(self.options_file.exists())
        self.assertIn("Deleted", buf.getvalue())

    def test_reset_options_handles_missing_file(self) -> None:
        """Missing options.json is not an error; rc is 0 with informative message."""
        self.assertFalse(self.options_file.exists())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._do_reset_options(self.ws)

        self.assertEqual(rc, 0)
        self.assertIn("does not exist", buf.getvalue())


class _FakeCfg(AppConfigStore):
    """Minimal AppConfigStore stand-in -- only the attributes touched by tested helpers."""

    def __init__(
        self,
        config: dict[str, Any],
        segments_def: list[dict[str, Any]],
        state: dict[str, Any],
        options_def: dict[str, Any] | None = None,
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
                "default_flags": [
                    "--strict-mcp-config",
                    "--dangerously-skip-permissions",
                ],
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
        {
            "key": "permissions",
            "label": "Perms",
            "required": False,
            "print_mode": False,
        },
    ]

    ALL_ENABLED = [
        "profile",
        "github",
        "version",
        "model",
        "directory",
        "mcp",
        "permissions",
    ]

    FULL_LAST_CONFIG = {
        "profile": "personal",
        "github": "ghuser",
        "version": "2.1.116",
        "model": "claude-opus-4-6",
        "directory": "/home/user/project",
        "mcp": "default",
        "permissions": "bypass",
    }

    def _make_cfg(self, last_config: dict[str, Any] | None = None) -> _FakeCfg:
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

    def _run_main(
        self, argv: list[str], last_config: dict[str, Any] | None = None
    ) -> mock.MagicMock:
        """Invoke cli.main() with patched argv, AppConfigStore, and _do_launch_sequence.

        Returns the mock for _do_launch_sequence so callers can inspect call args.
        strictcli's app.run() calls sys.exit(0) after the handler, so we catch that.
        """
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()

        with (
            mock.patch("sys.argv", argv),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
        ):
            try:
                cli.main()
            except SystemExit:
                pass

        return launch_mock

    # -- 1. Print mode filters out non-print segments --

    def test_print_mode_filters_non_print_segments(self) -> None:
        launch_mock = self._run_main(
            ["c", "-p", "test"],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        merged = (
            launch_mock.call_args[1].get("selections") or launch_mock.call_args[0][3]
        )

        # print_mode: True segments must be present
        for key in ("profile", "version", "model", "directory"):
            self.assertIn(key, merged, f"expected print_mode segment '{key}' in merged")

        # print_mode: False segments must NOT be present
        for key in ("github", "mcp", "permissions"):
            self.assertNotIn(
                key, merged, f"non-print segment '{key}' should be filtered out"
            )

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
        fake_cfg = self._make_cfg(last_config={})
        launch_mock = mock.MagicMock()

        with (
            mock.patch(
                "sys.argv",
                [
                    "c",
                    "--cont",
                    "--profile",
                    "personal",
                    "--github",
                    "ghuser",
                    "-s",
                    "version=2.1.116",
                    "--directory",
                    "/some/dir",
                ],
            ),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("claudewheel.cli._check_cont_session", autospec=True),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
        ):
            try:
                cli.main()
            except SystemExit:
                pass

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
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit:
                pass

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
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit:
                pass

        self.assertEqual(
            err.getvalue(), "", "no warning expected when all segments are present"
        )

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


class ClientSelectionCliTests(unittest.TestCase):
    """Client-step resolution in _handle_launch (non-interactive + wiring)."""

    SEGMENTS_DEF = PrintModeTests.SEGMENTS_DEF
    ALL_ENABLED = PrintModeTests.ALL_ENABLED

    def _make_cfg(
        self,
        *,
        default_client: str | None = None,
        last_config: dict[str, Any] | None = None,
    ) -> _FakeCfg:
        config = {
            "theme": "dark",
            "enabled_segments": list(self.ALL_ENABLED),
            "default_flags": [],
            "health_check_on_launch": False,
        }
        if default_client is not None:
            config["default_client"] = default_client
        return _FakeCfg(
            config=config,
            segments_def=list(self.SEGMENTS_DEF),
            state={
                "last_config": dict(last_config) if last_config else {},
                "recent_dirs": [],
                "launch_count": 0,
            },
            options_def={},
        )

    def _run(
        self, argv: list[str], cfg: _FakeCfg, app_mock: object | None = None
    ) -> tuple[MagicMock, str]:
        """Run cli.main() with AppConfigStore + _do_launch_sequence patched.

        Returns (launch_mock, stderr_text). When *app_mock* is given, the TUI
        App class is patched with it so the interactive path is exercised
        without a real terminal.
        """
        from contextlib import ExitStack

        launch_mock = mock.MagicMock()
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch("sys.argv", argv))
            stack.enter_context(
                mock.patch(
                    "claudewheel.config.AppConfigStore",
                    autospec=True,
                    return_value=cfg,
                )
            )
            stack.enter_context(
                mock.patch("claudewheel.cli._do_launch_sequence", launch_mock)
            )
            stack.enter_context(
                mock.patch("os.getcwd", autospec=True, return_value="/test/dir")
            )
            stack.enter_context(redirect_stderr(stderr))
            if app_mock is not None:
                stack.enter_context(mock.patch("claudewheel.app.App", app_mock))
            try:
                cli.main()
            except SystemExit:
                pass
        return launch_mock, stderr.getvalue()

    # -- Non-interactive: default_client used, ambient version dropped --

    def test_non_interactive_uses_default_client(self) -> None:
        cfg = self._make_cfg(
            default_client="miniclaude", last_config={"version": "2.1.202"}
        )
        launch_mock, _ = self._run(["c", "-p", "hi"], cfg)
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        self.assertEqual(kwargs["client"], "miniclaude")

    def test_non_interactive_drops_ambient_version_for_non_claude(self) -> None:
        cfg = self._make_cfg(
            default_client="miniclaude", last_config={"version": "2.1.202"}
        )
        launch_mock, _ = self._run(["c", "-p", "hi"], cfg)
        merged = launch_mock.call_args[0][3]
        self.assertNotIn("version", merged)

    def test_explicit_client_wins_over_default(self) -> None:
        cfg = self._make_cfg(default_client="claude")
        launch_mock, _ = self._run(["c", "--client", "miniclaude", "-p", "hi"], cfg)
        launch_mock.assert_called_once()
        self.assertEqual(launch_mock.call_args[1]["client"], "miniclaude")

    # -- Explicit version + non-claude client is a hard error --

    def test_explicit_version_with_miniclaude_hard_errors(self) -> None:
        cfg = self._make_cfg()
        launch_mock, err = self._run(
            [
                "c",
                "--client",
                "miniclaude",
                "-s",
                "version=2.1.116",
                "--profile",
                "p",
                "--directory",
                "/d",
            ],
            cfg,
        )
        launch_mock.assert_not_called()
        self.assertIn("claude-client-only", err)
        self.assertIn("2.1.116", err)

    # -- Unknown default_client is a hard error at launch --

    def test_unknown_default_client_hard_errors(self) -> None:
        cfg = self._make_cfg(default_client="bogus")
        launch_mock, err = self._run(["c", "-p", "hi"], cfg)
        launch_mock.assert_not_called()
        self.assertIn("bogus", err)
        self.assertIn("unknown client", err)

    # -- Interactive path threads the client inputs into the App --

    def _make_app_mock(
        self, selections: dict[str, str], selected_client: str
    ) -> tuple[MagicMock, MagicMock]:
        app_instance = mock.MagicMock()
        app_instance.run_tui.return_value = selections
        app_instance.selected_client = selected_client
        app_instance.cfg = None
        app_instance.bar.segments = []
        app_cls = mock.MagicMock(return_value=app_instance)
        return app_cls, app_instance

    def test_interactive_passes_no_explicit_client_to_app(self) -> None:
        cfg = self._make_cfg(default_client="claude")
        app_cls, _ = self._make_app_mock({"profile": "p", "directory": "/d"}, "claude")
        launch_mock, _ = self._run(["c", "--profile", "p"], cfg, app_mock=app_cls)
        app_cls.assert_called_once()
        kwargs = app_cls.call_args[1]
        self.assertIsNone(kwargs["explicit_client"])
        self.assertEqual(kwargs["default_client"], "claude")

    def test_interactive_threads_explicit_client_to_app(self) -> None:
        cfg = self._make_cfg(default_client="claude")
        app_cls, _ = self._make_app_mock(
            {"profile": "p", "directory": "/d"}, "miniclaude"
        )
        launch_mock, _ = self._run(
            ["c", "--client", "miniclaude", "--profile", "p"], cfg, app_mock=app_cls
        )
        app_cls.assert_called_once()
        self.assertEqual(app_cls.call_args[1]["explicit_client"], "miniclaude")
        # The client the App resolved is what gets launched.
        self.assertEqual(launch_mock.call_args[1]["client"], "miniclaude")


class LaunchCorruptTokensTests(unittest.TestCase):
    """A corrupt tokens.json on the launch path fails cleanly.

    Exercises the real _do_launch_sequence -> resolve_launch_config path (only
    AppConfigStore and run_hooks are stubbed) so the TokenStoreError raised while
    reading tokens.json propagates to the _handle_launch boundary, which must
    print a clean, actionable message and exit nonzero -- never a traceback.
    """

    SEGMENTS_DEF = PrintModeTests.SEGMENTS_DEF
    ALL_ENABLED = PrintModeTests.ALL_ENABLED

    def _make_cfg(self) -> _FakeCfg:
        return _FakeCfg(
            config={
                "theme": "dark",
                "enabled_segments": list(self.ALL_ENABLED),
                "default_flags": [],
                "health_check_on_launch": False,
            },
            segments_def=list(self.SEGMENTS_DEF),
            state={"last_config": {}, "recent_dirs": [], "launch_count": 0},
            options_def={},
        )

    def test_corrupt_tokens_clean_error_no_traceback(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tokens_file = Path(tmp.name) / "tokens.json"
        tokens_file.write_text("{ not valid json")

        fake_cfg = self._make_cfg()
        err = io.StringIO()
        with (
            mock.patch("sys.argv", ["c", "--profile", "work", "-p", "hi"]),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.hooks.run_hooks", autospec=True, return_value=True),
            mock.patch.dict(
                "os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(Path(tmp.name))}
            ),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()

        self.assertNotEqual(ctx.exception.code, 0)
        msg = err.getvalue()
        # The actionable TokenStoreError message (path + reason) must be shown,
        self.assertIn(str(tokens_file), msg)
        self.assertIn("corrupt", msg)
        # and no Python traceback should leak to the user.
        self.assertNotIn("Traceback", msg)


class LaunchStaleProfileTests(unittest.TestCase):
    """A launch selecting a profile that no longer exists fails cleanly.

    Exercises the real _do_launch_sequence -> resolve_launch_config path so the
    ValueError raised by ProfileStore.env for an unknown name is mapped to a
    clean stderr message + nonzero exit -- the hard-error contract that replaced
    the silent ~/.claude fallback, never a traceback.
    """

    SEGMENTS_DEF = PrintModeTests.SEGMENTS_DEF
    ALL_ENABLED = PrintModeTests.ALL_ENABLED

    def _make_cfg(self) -> _FakeCfg:
        return _FakeCfg(
            config={
                "theme": "dark",
                "enabled_segments": list(self.ALL_ENABLED),
                "default_flags": [],
                "health_check_on_launch": False,
            },
            segments_def=list(self.SEGMENTS_DEF),
            state={"last_config": {}, "recent_dirs": [], "launch_count": 0},
            options_def={},
        )

    def test_stale_profile_clean_error_no_traceback(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Empty profiles dir + no tokens file: "work" is not discoverable.
        profiles_dir = Path(tmp.name) / "profiles"
        profiles_dir.mkdir()

        fake_cfg = self._make_cfg()
        err = io.StringIO()
        with (
            mock.patch("sys.argv", ["c", "--profile", "work", "-p", "hi"]),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.hooks.run_hooks", autospec=True, return_value=True),
            mock.patch.dict(
                "os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(Path(tmp.name))}
            ),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()

        self.assertNotEqual(ctx.exception.code, 0)
        msg = err.getvalue()
        self.assertIn("Launch failed", msg)
        self.assertIn("work", msg)
        self.assertNotIn("Traceback", msg)


class BuildAppTests(unittest.TestCase):
    """Smoke test that _build_app() constructs successfully.

    Regression guard: strictcli >=0.16.0 requires every repeatable=True Flag
    to pass unique=True or unique=False explicitly. The `-s/--set` flag in
    _build_app() must comply, otherwise the entire binary crashes at startup
    with ValueError before main() can do anything useful.
    """

    def test_build_app_constructs_without_error(self) -> None:
        from strictcli import App
        from claudewheel.workspace import Workspace
        from claudewheel.binaries import BinaryLocator

        app = cli._build_app(Workspace.default(), BinaryLocator.default())
        self.assertIsInstance(app, App)


class DuplicateSetKeyTests(unittest.TestCase):
    """Regression guard for silent overwrite of segment overrides.

    Today, _handle_launch builds segment_overrides[key] from individual flags
    (--profile, --github, --model, --directory, --mcp, --permissions) and then
    from -s key=value entries, with later assignments silently overwriting
    earlier ones. The fix rejects any duplicate from ANY source -- both -s
    vs -s and flag vs -s -- with a diagnostic error naming both conflicting
    values and their sources.

    Mirrors PrintModeTests._run_main rather than subclassing it: inheriting
    from PrintModeTests would cause unittest to re-run every parent test
    method under this class's name, polluting the failure count.
    """

    # Mirror PrintModeTests.SEGMENTS_DEF / ALL_ENABLED / FULL_LAST_CONFIG
    # so _run_main below is a faithful copy of the parent helper.
    SEGMENTS_DEF = PrintModeTests.SEGMENTS_DEF
    ALL_ENABLED = PrintModeTests.ALL_ENABLED
    FULL_LAST_CONFIG = PrintModeTests.FULL_LAST_CONFIG

    def _make_cfg(self, last_config: dict[str, Any] | None = None) -> _FakeCfg:
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

    def _run_main(
        self, argv: list[str], last_config: dict[str, Any] | None = None
    ) -> mock.MagicMock:
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()
        with (
            mock.patch("sys.argv", argv),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        return launch_mock

    def test_duplicate_s_key_conflicting_values_rejected(self) -> None:
        """Two -s entries for the same key with different values must be rejected."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch_mock = self._run_main(
                [
                    "c",
                    "-s",
                    "profile=work",
                    "-s",
                    "profile=personal",
                    "--print-prompt",
                    "x",
                ],
                last_config=self.FULL_LAST_CONFIG,
            )

        msg = err.getvalue()
        self.assertIn("Duplicate", msg)
        self.assertIn("profile", msg)
        self.assertIn("work", msg)
        self.assertIn("personal", msg)
        # Error must fire BEFORE launch.
        launch_mock.assert_not_called()

    def test_duplicate_s_key_identical_values_rejected(self) -> None:
        """Two -s entries for the same key, even with identical values, must be rejected."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch_mock = self._run_main(
                [
                    "c",
                    "-s",
                    "profile=work",
                    "-s",
                    "profile=work",
                    "--print-prompt",
                    "x",
                ],
                last_config=self.FULL_LAST_CONFIG,
            )

        msg = err.getvalue()
        self.assertIn("Duplicate", msg)
        self.assertIn("profile", msg)
        launch_mock.assert_not_called()

    def test_flag_and_s_conflict_rejected(self) -> None:
        """A --profile flag plus a conflicting -s profile= must be rejected, with both sources named."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch_mock = self._run_main(
                [
                    "c",
                    "--profile",
                    "work",
                    "-s",
                    "profile=personal",
                    "--print-prompt",
                    "x",
                ],
                last_config=self.FULL_LAST_CONFIG,
            )

        msg = err.getvalue()
        self.assertIn("Duplicate", msg)
        self.assertIn("profile", msg)
        self.assertIn("work", msg)
        self.assertIn("personal", msg)
        # Both source markers must appear in the diagnostic.
        self.assertIn("--profile", msg)
        self.assertIn("-s", msg)
        launch_mock.assert_not_called()


class PickerFlagTests(unittest.TestCase):
    """Tests for the --picker flag in the launch subcommand.

    --picker passes bare ``--resume`` (no session ID) to Claude Code,
    opening the session resume picker.  It is mutually exclusive with
    --cont, --resume, and --print-prompt.
    """

    SEGMENTS_DEF = PrintModeTests.SEGMENTS_DEF
    ALL_ENABLED = PrintModeTests.ALL_ENABLED
    FULL_LAST_CONFIG = PrintModeTests.FULL_LAST_CONFIG

    def _make_cfg(self, last_config: dict[str, Any] | None = None) -> _FakeCfg:
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

    def _run_main(
        self, argv: list[str], last_config: dict[str, Any] | None = None
    ) -> mock.MagicMock:
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()
        with (
            mock.patch("sys.argv", argv),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        return launch_mock

    # -- 1. Basic behavior: --picker adds bare --resume --

    def test_picker_adds_bare_resume_flag(self) -> None:
        """--picker should produce extra_flags containing only '--resume' (no session ID)."""
        launch_mock = self._run_main(
            [
                "c",
                "--picker",
                "--profile",
                "personal",
                "--github",
                "ghuser",
                "-s",
                "version=2.1.116",
                "--directory",
                "/some/dir",
            ],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        extra_flags = kwargs["extra_flags"]
        self.assertEqual(extra_flags, ["--resume"])

    # -- 2. Mutual exclusivity with --resume --

    def test_picker_and_resume_mutually_exclusive(self) -> None:
        """Passing both --picker and --resume must error, naming all four flags."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch_mock = self._run_main(
                ["c", "--picker", "--resume", "abc123"],
                last_config=self.FULL_LAST_CONFIG,
            )
        msg = err.getvalue()
        self.assertIn("--cont", msg)
        self.assertIn("--resume", msg)
        self.assertIn("--print-prompt", msg)
        self.assertIn("--picker", msg)
        self.assertIn("mutually exclusive", msg)
        launch_mock.assert_not_called()

    # -- 3. Mutual exclusivity with --cont --

    def test_picker_and_cont_mutually_exclusive(self) -> None:
        """Passing both --picker and --cont must error, naming all four flags."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch_mock = self._run_main(
                ["c", "--picker", "--cont"],
                last_config=self.FULL_LAST_CONFIG,
            )
        msg = err.getvalue()
        self.assertIn("--cont", msg)
        self.assertIn("--resume", msg)
        self.assertIn("--print-prompt", msg)
        self.assertIn("--picker", msg)
        self.assertIn("mutually exclusive", msg)
        launch_mock.assert_not_called()

    # -- 4. Mutual exclusivity with --print-prompt --

    def test_picker_and_print_prompt_mutually_exclusive(self) -> None:
        """Passing both --picker and --print-prompt must error, naming all four flags."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch_mock = self._run_main(
                ["c", "--picker", "--print-prompt", "hello"],
                last_config=self.FULL_LAST_CONFIG,
            )
        msg = err.getvalue()
        self.assertIn("--cont", msg)
        self.assertIn("--resume", msg)
        self.assertIn("--print-prompt", msg)
        self.assertIn("--picker", msg)
        self.assertIn("mutually exclusive", msg)
        launch_mock.assert_not_called()

    # -- 5. Picker not set: no --resume from picker path --

    def test_no_picker_no_resume_in_extra_flags(self) -> None:
        """When --picker is not passed and no session flag is set, extra_flags is empty."""
        launch_mock = self._run_main(
            [
                "c",
                "--profile",
                "personal",
                "--github",
                "ghuser",
                "-s",
                "version=2.1.116",
                "--directory",
                "/some/dir",
            ],
            last_config=self.FULL_LAST_CONFIG,
        )
        launch_mock.assert_called_once()
        _, kwargs = launch_mock.call_args
        extra_flags = kwargs["extra_flags"]
        self.assertEqual(extra_flags, [])


class CheckResumeSessionTests(unittest.TestCase):
    """Tests for _check_resume_session: resume interception on directory renames."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            Path(self._tmp.name), claude_dir=Path(self._tmp.name) / ".claude"
        )
        self.shared_dir = self.ws.shared_dir
        self.shared_dir.mkdir()
        (self.shared_dir / "projects").mkdir()

    # -- 5.1a: Session exists under current dir -> no interception --

    def test_resume_session_in_current_dir_no_interception(self) -> None:
        """When the session file exists under the current directory's encoded path,
        the function returns immediately without calling find_session."""
        from claudewheel.shared_store import SharedStore

        current_dir = "/home/user/my-project"
        session_id = "abc-123-def"
        encoded = SharedStore.encode_path(os.path.abspath(current_dir))

        # Create the expected session file
        project_dir = self.shared_dir / "projects" / encoded
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_id}.jsonl").write_text(
            '{"cwd":"/home/user/my-project"}\n'
        )

        with mock.patch("claudewheel.session.find_session", autospec=True) as mock_find:
            cli._check_resume_session(self.ws, session_id, current_dir)

        mock_find.assert_not_called()

    # -- 5.1b: Session found elsewhere, user confirms both prompts --

    def test_resume_session_found_elsewhere_user_confirms(self) -> None:
        """When session is found under a different dir whose old path is gone,
        and user says 'y' twice, run_mv is called for dry-run then real."""
        from claudewheel.session import SessionInfo

        session_id = "abc-123-def"
        current_dir = os.path.abspath("/home/user/new-project")
        old_cwd = "/home/user/old-project"

        info = SessionInfo(
            session_id=session_id,
            jsonl_path=Path("/fake/path/abc-123-def.jsonl"),
            encoded_cwd="encoded-old",
            cwd=old_cwd,
        )

        dry_result = mock.MagicMock()
        dry_result.dirs_renamed = 1
        dry_result.files_rewritten = 2
        dry_result.lines_replaced = 5
        dry_result.project_keys_updated = 1

        real_result = mock.MagicMock()
        real_result.files_rewritten = 2

        with (
            mock.patch(
                "claudewheel.session.find_session", autospec=True, return_value=info
            ),
            mock.patch(
                "claudewheel.mv.run_mv",
                autospec=True,
                side_effect=[dry_result, real_result],
            ) as mock_mv,
            mock.patch("builtins.input", autospec=True, side_effect=["y", "y"]),
            mock.patch("os.path.isdir", autospec=True, return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            # Should return normally (no sys.exit)
            cli._check_resume_session(self.ws, session_id, current_dir)

        self.assertEqual(mock_mv.call_count, 2)
        # First call: dry_run=True
        call1_args, call1_kwargs = mock_mv.call_args_list[0]
        self.assertTrue(call1_kwargs.get("dry_run", True))
        # Second call: dry_run=False
        call2_args, call2_kwargs = mock_mv.call_args_list[1]
        self.assertFalse(call2_kwargs.get("dry_run", False))

    # -- 5.1c: User declines first prompt --

    def test_resume_session_found_elsewhere_user_declines_first_prompt(self) -> None:
        """When user says 'n' at the move confirmation, sys.exit(1) is called
        and run_mv is never invoked."""
        from claudewheel.session import SessionInfo

        session_id = "abc-123-def"
        current_dir = os.path.abspath("/home/user/new-project")
        old_cwd = "/home/user/old-project"

        info = SessionInfo(
            session_id=session_id,
            jsonl_path=Path("/fake/path/abc-123-def.jsonl"),
            encoded_cwd="encoded-old",
            cwd=old_cwd,
        )

        # Create old project dir in shared to satisfy glob counting
        old_project_dir = self.shared_dir / "projects" / "encoded-old"
        old_project_dir.mkdir(parents=True)
        (old_project_dir / f"{session_id}.jsonl").write_text(
            '{"cwd":"/home/user/old-project"}\n'
        )

        with (
            mock.patch(
                "claudewheel.session.find_session", autospec=True, return_value=info
            ),
            mock.patch("claudewheel.mv.run_mv", autospec=True) as mock_mv,
            mock.patch("builtins.input", autospec=True, return_value="n"),
            mock.patch("os.path.isdir", autospec=True, return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(self.ws, session_id, current_dir)
            self.assertEqual(ctx.exception.code, 1)

        mock_mv.assert_not_called()

    # -- 5.1d: User confirms first prompt, declines second --

    def test_resume_session_found_elsewhere_user_declines_second_prompt(self) -> None:
        """When user says 'y' then 'n', run_mv is called once (dry_run=True only)."""
        from claudewheel.session import SessionInfo

        session_id = "abc-123-def"
        current_dir = os.path.abspath("/home/user/new-project")
        old_cwd = "/home/user/old-project"

        info = SessionInfo(
            session_id=session_id,
            jsonl_path=Path("/fake/path/abc-123-def.jsonl"),
            encoded_cwd="encoded-old",
            cwd=old_cwd,
        )

        old_project_dir = self.shared_dir / "projects" / "encoded-old"
        old_project_dir.mkdir(parents=True)
        (old_project_dir / f"{session_id}.jsonl").write_text(
            '{"cwd":"/home/user/old-project"}\n'
        )

        dry_result = mock.MagicMock()
        dry_result.dirs_renamed = 1
        dry_result.files_rewritten = 2
        dry_result.lines_replaced = 5
        dry_result.project_keys_updated = 1

        with (
            mock.patch(
                "claudewheel.session.find_session", autospec=True, return_value=info
            ),
            mock.patch(
                "claudewheel.mv.run_mv", autospec=True, return_value=dry_result
            ) as mock_mv,
            mock.patch("builtins.input", autospec=True, side_effect=["y", "n"]),
            mock.patch("os.path.isdir", autospec=True, return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(self.ws, session_id, current_dir)
            self.assertEqual(ctx.exception.code, 1)

        # run_mv called once for dry-run only
        mock_mv.assert_called_once()
        _, kwargs = mock_mv.call_args
        self.assertTrue(kwargs.get("dry_run", True))

    # -- 5.1e: Session not found anywhere --

    def test_resume_session_not_found_anywhere(self) -> None:
        """When find_session returns None, sys.exit(1) is called and
        the error message mentions the session ID."""
        session_id = "nonexistent-session-id"
        current_dir = os.path.abspath("/home/user/some-project")

        err = io.StringIO()
        with (
            mock.patch(
                "claudewheel.session.find_session", autospec=True, return_value=None
            ),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(self.ws, session_id, current_dir)
            self.assertEqual(ctx.exception.code, 1)

        self.assertIn(session_id, err.getvalue())

    # -- 5.1f: Old path still exists on disk --

    def test_resume_old_path_still_exists(self) -> None:
        """When the old cwd still exists, sys.exit(1) is called and
        the message tells the user to run from the old path."""
        from claudewheel.session import SessionInfo

        session_id = "abc-123-def"
        current_dir = os.path.abspath("/home/user/new-project")
        old_cwd = "/home/user/old-project"

        info = SessionInfo(
            session_id=session_id,
            jsonl_path=Path("/fake/path/abc-123-def.jsonl"),
            encoded_cwd="encoded-old",
            cwd=old_cwd,
        )

        err = io.StringIO()
        with (
            mock.patch(
                "claudewheel.session.find_session", autospec=True, return_value=info
            ),
            mock.patch("os.path.isdir", autospec=True, return_value=True),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(self.ws, session_id, current_dir)
            self.assertEqual(ctx.exception.code, 1)

        msg = err.getvalue()
        self.assertIn(old_cwd, msg)
        self.assertIn("still exists", msg)
        self.assertIn("Run from that directory instead", msg)

    # -- 5.1g: Bare resume (empty string) does NOT call _check_resume_session --

    def test_bare_resume_no_interception(self) -> None:
        """When resume_val is empty string (bare --resume with no ID),
        _check_resume_session is NOT called."""
        # Use the _run_main pattern from other test classes
        SEGMENTS_DEF = PrintModeTests.SEGMENTS_DEF
        ALL_ENABLED = PrintModeTests.ALL_ENABLED
        FULL_LAST_CONFIG = PrintModeTests.FULL_LAST_CONFIG

        fake_cfg = _FakeCfg(
            config={
                "theme": "dark",
                "enabled_segments": list(ALL_ENABLED),
                "default_flags": [],
                "health_check_on_launch": False,
            },
            segments_def=list(SEGMENTS_DEF),
            state={
                "last_config": dict(FULL_LAST_CONFIG),
                "recent_dirs": [],
                "launch_count": 0,
            },
            options_def={},
        )

        with (
            mock.patch(
                "sys.argv",
                [
                    "c",
                    "--resume",
                    "",
                    "--profile",
                    "personal",
                    "--github",
                    "ghuser",
                    "-s",
                    "version=2.1.116",
                    "--directory",
                    "/some/dir",
                ],
            ),
            mock.patch(
                "claudewheel.config.AppConfigStore",
                autospec=True,
                return_value=fake_cfg,
            ),
            mock.patch("claudewheel.cli._do_launch_sequence", mock.MagicMock()),
            mock.patch(
                "claudewheel.cli._check_resume_session", autospec=True
            ) as mock_check,
            mock.patch("os.getcwd", autospec=True, return_value="/test/dir"),
        ):
            try:
                cli.main()
            except SystemExit:
                pass

        mock_check.assert_not_called()


class CheckContSessionTests(unittest.TestCase):
    """Tests for _check_cont_session: --cont interception on directory renames."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            Path(self._tmp.name), claude_dir=Path(self._tmp.name) / ".claude"
        )
        self.shared_dir = self.ws.shared_dir
        self.shared_dir.mkdir()
        (self.shared_dir / "projects").mkdir()

    def _create_project(
        self,
        encoded_cwd: str,
        session_count: int = 1,
        cwd: str = "/home/user/my-project",
    ) -> Path:
        """Create a fake project dir with session JSONL files."""
        project_dir = self.shared_dir / "projects" / encoded_cwd
        project_dir.mkdir(parents=True, exist_ok=True)
        for i in range(session_count):
            p = project_dir / f"session-{i}.jsonl"
            p.write_text(f'{{"type":"user","cwd":"{cwd}","message":"hi"}}\n')
        return project_dir

    # -- Sessions exist under current dir -> no interception --

    def test_cont_sessions_exist_no_interception(self) -> None:
        """When sessions exist under the current directory, return immediately."""
        from claudewheel.shared_store import SharedStore

        current_dir = "/home/user/my-project"
        encoded = SharedStore.encode_path(os.path.abspath(current_dir))
        self._create_project(encoded, session_count=2, cwd=current_dir)

        with mock.patch(
            "claudewheel.session.find_orphaned_project_dirs", autospec=True
        ) as mock_find:
            cli._check_cont_session(self.ws, current_dir)

        mock_find.assert_not_called()

    # -- No sessions, one candidate, user confirms --

    def test_cont_no_sessions_one_candidate_user_confirms(self) -> None:
        """One orphaned dir found, user says yes twice, move happens."""
        from claudewheel.session import OrphanedProject

        current_dir = os.path.abspath("/home/user/new-project")
        old_cwd = "/home/user/old-project"
        orphan = OrphanedProject(
            encoded_cwd="-home-user-old-project",
            cwd=old_cwd,
            session_count=3,
            total_size_bytes=1024 * 1024 * 2,  # 2 MB
            projects_dir=self.shared_dir / "projects" / "-home-user-old-project",
        )

        dry_result = mock.MagicMock()
        dry_result.dirs_renamed = 1
        dry_result.files_rewritten = 3
        dry_result.lines_replaced = 7
        dry_result.project_keys_updated = 1

        real_result = mock.MagicMock()
        real_result.files_rewritten = 3

        with (
            mock.patch(
                "claudewheel.session.find_orphaned_project_dirs",
                autospec=True,
                return_value=[orphan],
            ),
            mock.patch(
                "claudewheel.mv.run_mv",
                autospec=True,
                side_effect=[dry_result, real_result],
            ) as mock_mv,
            mock.patch("builtins.input", autospec=True, side_effect=["y", "y"]),
            redirect_stdout(io.StringIO()),
        ):
            cli._check_cont_session(self.ws, current_dir)

        self.assertEqual(mock_mv.call_count, 2)
        # First call: dry_run=True
        _, kwargs1 = mock_mv.call_args_list[0]
        self.assertTrue(kwargs1.get("dry_run", True))
        # Second call: dry_run=False
        _, kwargs2 = mock_mv.call_args_list[1]
        self.assertFalse(kwargs2.get("dry_run", False))

    # -- No sessions, one candidate, user declines --

    def test_cont_no_sessions_one_candidate_user_declines(self) -> None:
        """User says no at first prompt, returns silently."""
        from claudewheel.session import OrphanedProject

        current_dir = os.path.abspath("/home/user/new-project")
        orphan = OrphanedProject(
            encoded_cwd="-home-user-old-project",
            cwd="/home/user/old-project",
            session_count=2,
            total_size_bytes=512,
            projects_dir=self.shared_dir / "projects" / "-home-user-old-project",
        )

        with (
            mock.patch(
                "claudewheel.session.find_orphaned_project_dirs",
                autospec=True,
                return_value=[orphan],
            ),
            mock.patch("claudewheel.mv.run_mv", autospec=True) as mock_mv,
            mock.patch("builtins.input", autospec=True, return_value="n"),
            redirect_stdout(io.StringIO()),
        ):
            # Should return normally (no sys.exit)
            cli._check_cont_session(self.ws, current_dir)

        mock_mv.assert_not_called()

    # -- No sessions, no candidates --

    def test_cont_no_sessions_no_candidates(self) -> None:
        """No orphans found, returns silently."""
        current_dir = os.path.abspath("/home/user/new-project")

        with (
            mock.patch(
                "claudewheel.session.find_orphaned_project_dirs",
                autospec=True,
                return_value=[],
            ),
            mock.patch("claudewheel.mv.run_mv", autospec=True) as mock_mv,
            mock.patch("builtins.input", autospec=True) as mock_input,
        ):
            cli._check_cont_session(self.ws, current_dir)

        mock_mv.assert_not_called()
        mock_input.assert_not_called()

    # -- No sessions, multiple candidates, user picks one --

    def test_cont_no_sessions_multiple_candidates(self) -> None:
        """Two orphaned dirs, user picks #2, move happens."""
        from claudewheel.session import OrphanedProject

        current_dir = os.path.abspath("/home/user/new-project")
        orphan1 = OrphanedProject(
            encoded_cwd="-home-user-alpha",
            cwd="/home/user/alpha",
            session_count=1,
            total_size_bytes=100,
            projects_dir=self.shared_dir / "projects" / "-home-user-alpha",
        )
        orphan2 = OrphanedProject(
            encoded_cwd="-home-user-beta",
            cwd="/home/user/beta",
            session_count=5,
            total_size_bytes=1024 * 1024,
            projects_dir=self.shared_dir / "projects" / "-home-user-beta",
        )

        dry_result = mock.MagicMock()
        dry_result.dirs_renamed = 1
        dry_result.files_rewritten = 5
        dry_result.lines_replaced = 10
        dry_result.project_keys_updated = 1

        real_result = mock.MagicMock()
        real_result.files_rewritten = 5

        with (
            mock.patch(
                "claudewheel.session.find_orphaned_project_dirs",
                autospec=True,
                return_value=[orphan1, orphan2],
            ),
            mock.patch(
                "claudewheel.mv.run_mv",
                autospec=True,
                side_effect=[dry_result, real_result],
            ) as mock_mv,
            mock.patch("builtins.input", autospec=True, side_effect=["2", "y"]),
            redirect_stdout(io.StringIO()),
        ):
            cli._check_cont_session(self.ws, current_dir)

        self.assertEqual(mock_mv.call_count, 2)
        # Verify the selected orphan (orphan2) was passed to run_mv (arg 0 is
        # the threaded workspace; the old_cwd is arg 1).
        args1, _ = mock_mv.call_args_list[0]
        self.assertEqual(args1[1], "/home/user/beta")


class MvPostHocFlagTests(unittest.TestCase):
    """Verify --post-hoc flag is passed through to run_mv."""

    def test_post_hoc_flag_passed_to_run_mv(self) -> None:
        """When --post-hoc is given, run_mv is called with post_hoc=True."""
        with (
            mock.patch("sys.argv", ["c", "mv", "/old/path", "/new/path", "--post-hoc"]),
            mock.patch("claudewheel.mv.run_mv", autospec=True) as mock_run_mv,
        ):
            try:
                cli.main()
            except SystemExit:
                pass

        mock_run_mv.assert_called_once()
        _, kwargs = mock_run_mv.call_args
        self.assertTrue(kwargs.get("post_hoc", False))

    def test_no_post_hoc_flag_defaults_false(self) -> None:
        """When --post-hoc is not given, run_mv is called with post_hoc=False."""
        with (
            mock.patch("sys.argv", ["c", "mv", "/old/path", "/new/path"]),
            mock.patch("claudewheel.mv.run_mv", autospec=True) as mock_run_mv,
        ):
            try:
                cli.main()
            except SystemExit:
                pass

        mock_run_mv.assert_called_once()
        _, kwargs = mock_run_mv.call_args
        self.assertFalse(kwargs.get("post_hoc", True))


class NewProfileFlowTests(unittest.TestCase):
    """_handle_new_profile runs the continuous alt-screen create-profile flow
    on a CLI-owned terminal, then prints the summary and outcome."""

    def setUp(self) -> None:
        from claudewheel.defaults import DEFAULT_THEME_DARK
        from claudewheel.workspace import Workspace
        from claudewheel.binaries import BinaryLocator

        self._ws_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._ws_tmp.cleanup)
        # Empty sandbox workspace -> enumerate() yields no profiles; claude_dir
        # points off the real home so no "default" profile leaks in.
        self.ws = Workspace.open(
            Path(self._ws_tmp.name), claude_dir=Path(self._ws_tmp.name) / "claude"
        )
        self.locator = BinaryLocator.default()

        self.terminal = mock.MagicMock()
        self.terminal._in_raw = False

        self._patches = {
            "terminal_cls": mock.patch(
                "claudewheel.terminal.Terminal",
                autospec=True,
                return_value=self.terminal,
            ),
            "config": mock.patch("claudewheel.config.AppConfigStore", autospec=True),
            "wizard": mock.patch(
                "claudewheel.wizard.run_profile_wizard", autospec=True
            ),
            "create": mock.patch(
                "claudewheel.wizard.create_profile",
                autospec=True,
                return_value=["Created profile 'p':", "  Config dir: /x"],
            ),
            "auth": mock.patch(
                "claudewheel.wizard.run_auth_flow",
                autospec=True,
                return_value="authenticated",
            ),
            "page": mock.patch("claudewheel.ui.show_page", autospec=True),
        }
        self.mocks = {}
        for name, p in self._patches.items():
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)

        # _handle_new_profile resolves theme at the boundary: an explicit
        # "dark" avoids a terminal query, and load_theme feeds parse_theme
        # (which runs for real) the default dark theme dict.
        self.mocks["config"].return_value.config = {"theme": "dark"}
        self.mocks["config"].return_value.load_theme.return_value = DEFAULT_THEME_DARK

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "p"
        wizard_result.config_dir = "~/.claudewheel/profiles/p"
        self.wizard_result = wizard_result
        self.mocks["wizard"].return_value = wizard_result

    def _run(self) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._handle_new_profile(self.ws, self.locator)
        return rc, buf.getvalue()

    def test_terminal_enters_alt_screen_raw_session(self) -> None:
        self._run()
        self.terminal.enter_raw.assert_called_once_with(alt_screen=True)
        self.terminal.exit_raw.assert_called_once()
        self.terminal.close.assert_called_once()

    def test_wizard_gets_theme_and_cli_terminal(self) -> None:
        self._run()
        # run_profile_wizard(ws, existing, theme, terminal)
        args = self.mocks["wizard"].call_args.args
        self.assertIs(args[0], self.ws)
        self.assertEqual(args[1], [])
        self.assertIs(args[3], self.terminal)

    def test_summary_page_shown_then_summary_printed(self) -> None:
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.mocks["page"].assert_called_once()
        page_args = self.mocks["page"].call_args.args
        self.assertEqual(page_args[0], "Profile created")
        self.assertEqual(page_args[1], ["Created profile 'p':", "  Config dir: /x"])
        self.assertIn("Created profile 'p':", out)
        self.assertIn("Profile authenticated.", out)

    def test_auth_runs_before_summary_page(self) -> None:
        manager = mock.MagicMock()
        manager.attach_mock(self.mocks["auth"], "run_auth_flow")
        manager.attach_mock(self.mocks["page"], "show_page")
        self._run()
        call_names = [c[0] for c in manager.mock_calls]
        self.assertLess(
            call_names.index("run_auth_flow"), call_names.index("show_page")
        )

    def test_cancelled_wizard_prints_cancelled(self) -> None:
        self.wizard_result.cancelled = True
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("Cancelled.", out)
        self.mocks["create"].assert_not_called()
        self.mocks["page"].assert_not_called()
        # Session is still torn down cleanly
        self.terminal.exit_raw.assert_called_once()
        self.terminal.close.assert_called_once()

    def test_auth_cancel_outcome_printed(self) -> None:
        self.mocks["auth"].return_value = "cancel"
        _rc, out = self._run()
        self.assertIn("Auth setup cancelled", out)

    def test_auth_failed_outcome_printed(self) -> None:
        self.mocks["auth"].return_value = "failed"
        _rc, out = self._run()
        self.assertIn("Auth setup failed", out)

    def test_auth_unverified_outcome_printed(self) -> None:
        self.mocks["auth"].return_value = "unverified"
        _rc, out = self._run()
        self.assertIn("Token saved without validation (API unreachable).", out)

    def test_terminal_closed_even_when_wizard_raises(self) -> None:
        self.mocks["wizard"].side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self._run()
        self.terminal.exit_raw.assert_called_once()
        self.terminal.close.assert_called_once()

    def test_headless_terminal_error_propagates(self) -> None:
        """No degraded mode: a missing TTY fails loudly before any form runs."""
        self.mocks["terminal_cls"].side_effect = OSError("no /dev/tty")
        with self.assertRaises(OSError):
            self._run()
        self.mocks["wizard"].assert_not_called()


class ShowProfileCommandTests(unittest.TestCase):
    """The profile show subcommand: routing, report output, unknown names."""

    def test_profile_in_subcommands(self) -> None:
        """'profile' group must be routed as a subcommand, not launch args."""
        self.assertIn("profile", cli._SUBCOMMANDS)

    def test_deprecated_names_in_subcommands(self) -> None:
        """Deprecated top-level names must remain in _SUBCOMMANDS so main()
        doesn't rewrite them to 'launch <name>' before the deprecation fires."""
        for name in ("new-profile", "delete-profile", "show-profile"):
            self.assertIn(name, cli._SUBCOMMANDS)

    def _report(self, **overrides: Any) -> "ProfileReport":
        from claudewheel.profile_info import ProfileReport
        from pathlib import Path as _P

        kwargs: dict[str, Any] = dict(
            name="work",
            config_dir=_P("/fake/profiles/work"),
            exists=True,
            registered=True,
            pinned=False,
            has_credentials=True,
            has_token=False,
            token_expiry=None,
        )
        kwargs.update(overrides)
        return ProfileReport(**kwargs)

    def test_handler_prints_report(self) -> None:
        report = self._report()
        buf = io.StringIO()
        with (
            mock.patch(
                "claudewheel.profile_info.gather_profile_info",
                autospec=True,
                return_value=report,
            ) as mock_gather,
            redirect_stdout(buf),
        ):
            rc = cli._handle_show_profile(mock.MagicMock(), "work")
        self.assertEqual(rc, 0)
        mock_gather.assert_called_once_with(mock.ANY, "work")
        out = buf.getvalue()
        self.assertIn("Profile: work", out)
        self.assertIn("Credentials file: present", out)

    def test_unknown_profile_exits_1(self) -> None:
        report = self._report(exists=False, registered=False, has_credentials=False)
        err = io.StringIO()
        with (
            mock.patch(
                "claudewheel.profile_info.gather_profile_info",
                autospec=True,
                return_value=report,
            ),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._handle_show_profile(mock.MagicMock(), "work")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", err.getvalue())

    def test_token_only_profile_is_shown(self) -> None:
        """A profile known only via tokens.json is still inspectable."""
        report = self._report(
            exists=False, registered=False, has_credentials=False, has_token=True
        )
        buf = io.StringIO()
        with (
            mock.patch(
                "claudewheel.profile_info.gather_profile_info",
                autospec=True,
                return_value=report,
            ),
            redirect_stdout(buf),
        ):
            rc = cli._handle_show_profile(mock.MagicMock(), "work")
        self.assertEqual(rc, 0)
        self.assertIn("Profile: work", buf.getvalue())

    def test_corrupt_tokens_clean_error_no_traceback(self) -> None:
        """A corrupt tokens.json makes 'profile show' fail cleanly: nonzero exit,
        actionable message on stderr, no traceback (mirrors check-tokens)."""
        from claudewheel.tokens import TokenStoreError

        err = io.StringIO()
        with (
            mock.patch(
                "claudewheel.profile_info.gather_profile_info",
                autospec=True,
                side_effect=TokenStoreError("/x/tokens.json is corrupt; retry."),
            ),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._handle_show_profile(mock.MagicMock(), "work")
        self.assertEqual(ctx.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("corrupt", msg)
        self.assertNotIn("Traceback", msg)


class DeleteProfileHandlerTests(unittest.TestCase):
    """_handle_delete_profile: running check (CLI policy) + ProfileStore.delete."""

    def _ok_result(self) -> "DeletionResult":
        from claudewheel.profile_store import DeletionResult

        return DeletionResult(
            removed_symlinks=1,
            removed_real=2,
            removed_from_options=True,
            removed_from_tokens=True,
            last_config_purged=False,
        )

    def test_flags_wire_through(self) -> None:
        """--force-delete skips the running check; --force-delete-data maps to
        allow_data_destruction on ProfileStore.delete."""
        mock_store = mock.MagicMock()
        mock_store.delete.return_value = self._ok_result()
        ws = mock.MagicMock()
        ws.profiles = mock_store
        with (
            mock.patch(
                "claudewheel.profile_ops._is_profile_running", autospec=True
            ) as mock_run,
            redirect_stdout(io.StringIO()),
        ):
            rc = cli._handle_delete_profile(
                ws, "work", force_delete=True, force_delete_data=True
            )
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()  # force-delete skips the running check
        mock_store.delete.assert_called_once_with("work", allow_data_destruction=True)

    def test_default_flags_off(self) -> None:
        """No force flags: running check runs, allow_data_destruction is False."""
        mock_store = mock.MagicMock()
        mock_store.delete.return_value = self._ok_result()
        ws = mock.MagicMock()
        ws.profiles = mock_store
        with (
            mock.patch(
                "claudewheel.profile_ops._is_profile_running",
                autospec=True,
                return_value=False,
            ) as mock_run,
            redirect_stdout(io.StringIO()),
        ):
            rc = cli._handle_delete_profile(
                ws, "work", force_delete=False, force_delete_data=False
            )
        self.assertEqual(rc, 0)
        mock_run.assert_called_once_with(ws, "work")
        mock_store.delete.assert_called_once_with("work", allow_data_destruction=False)

    def test_running_profile_blocked_without_force(self) -> None:
        """A running profile is refused (CLI policy) unless --force-delete."""
        mock_store = mock.MagicMock()
        err = io.StringIO()
        ws = mock.MagicMock()
        ws.profiles = mock_store
        with (
            mock.patch(
                "claudewheel.profile_ops._is_profile_running",
                autospec=True,
                return_value=True,
            ),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._handle_delete_profile(
                    ws, "work", force_delete=False, force_delete_data=False
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("active sessions", err.getvalue())
        mock_store.delete.assert_not_called()

    def test_store_refusal_exits_1(self) -> None:
        """A ValueError refusal from the store prints and exits 1."""
        mock_store = mock.MagicMock()
        mock_store.delete.side_effect = ValueError("Profile 'work' not found")
        err = io.StringIO()
        ws = mock.MagicMock()
        ws.profiles = mock_store
        with (
            mock.patch(
                "claudewheel.profile_ops._is_profile_running",
                autospec=True,
                return_value=False,
            ),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._handle_delete_profile(
                    ws, "work", force_delete=False, force_delete_data=False
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", err.getvalue())


class ProfileGroupDispatchTests(unittest.TestCase):
    """Verify 'profile create/delete/show' dispatch to the correct handlers."""

    def test_profile_create_dispatches(self) -> None:
        """'claudewheel profile create' calls _handle_new_profile."""
        with (
            mock.patch("sys.argv", ["c", "profile", "create"]),
            mock.patch.object(
                cli, "_handle_new_profile", autospec=True, return_value=0
            ) as mock_handler,
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()

    def test_profile_delete_dispatches(self) -> None:
        """'claudewheel profile delete work' calls _handle_delete_profile."""
        # _build_app reads the handler's strictcli flag/arg metadata to register
        # the command (including the required --force-delete / --force-delete-data
        # bool flags). autospec mirrors those private attributes as strings rather
        # than the real Flag lists, so capture and restore them on the mock; the
        # invocation then supplies both required flags for a valid dispatch.
        handler: Any = cli._handle_delete_profile
        real_flags = handler._strictcli_flags
        real_args = getattr(handler, "_strictcli_args", [])
        with (
            mock.patch(
                "sys.argv",
                [
                    "c",
                    "profile",
                    "delete",
                    "work",
                    "--force-delete",
                    "--force-delete-data",
                ],
            ),
            mock.patch.object(
                cli, "_handle_delete_profile", autospec=True, return_value=0
            ) as mock_handler,
        ):
            mock_handler._strictcli_flags = real_flags
            mock_handler._strictcli_args = real_args
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()
        # strictcli passes args as kwargs
        self.assertEqual(mock_handler.call_args.kwargs["name"], "work")
        self.assertTrue(mock_handler.call_args.kwargs["force_delete"])
        self.assertTrue(mock_handler.call_args.kwargs["force_delete_data"])

    def test_profile_show_dispatches(self) -> None:
        """'claudewheel profile show work' calls _handle_show_profile."""
        with (
            mock.patch("sys.argv", ["c", "profile", "show", "work"]),
            mock.patch.object(
                cli, "_handle_show_profile", autospec=True, return_value=0
            ) as mock_handler,
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()
        self.assertEqual(mock_handler.call_args.kwargs["name"], "work")


class DeprecatedProfileCommandTests(unittest.TestCase):
    """Verify old top-level names exit 1 with deprecation messages on stderr."""

    def test_new_profile_deprecated(self) -> None:
        """'claudewheel new-profile' exits 1 with migration guidance on stderr."""
        err = io.StringIO()
        with (
            mock.patch("sys.argv", ["c", "new-profile"]),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("deprecated", msg)
        self.assertIn("profile create", msg)

    def test_delete_profile_deprecated(self) -> None:
        """'claudewheel delete-profile work' exits 1 with migration guidance on stderr."""
        err = io.StringIO()
        with (
            mock.patch("sys.argv", ["c", "delete-profile", "work"]),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("deprecated", msg)
        self.assertIn("profile delete", msg)

    def test_show_profile_deprecated(self) -> None:
        """'claudewheel show-profile work' exits 1 with migration guidance on stderr."""
        err = io.StringIO()
        with (
            mock.patch("sys.argv", ["c", "show-profile", "work"]),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertEqual(ctx.exception.code, 1)
        msg = err.getvalue()
        self.assertIn("deprecated", msg)
        self.assertIn("profile show", msg)


class FixAuthTests(unittest.TestCase):
    """Tests for _handle_fix_auth: remove session credentials shadowing tokens."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.profiles_dir = self.home / ".claudewheel" / "profiles"
        self.profiles_dir.mkdir(parents=True)
        self.tokens_file = self.home / ".claudewheel" / "tokens.json"
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )

    def _make_profile(self, name: str) -> Path:
        pdir = self.profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir

    def _write_tokens(self, data: dict[str, Any]) -> None:
        import json

        self.tokens_file.parent.mkdir(parents=True, exist_ok=True)
        self.tokens_file.write_text(json.dumps(data))
        self.tokens_file.chmod(0o600)

    def _write_credentials(self, pdir: Path, data: dict[str, Any]) -> None:
        import json

        creds = pdir / ".credentials.json"
        creds.write_text(json.dumps(data))
        creds.chmod(0o600)

    def _run_fix_auth(self, name: str) -> tuple[int | str | None, str, str]:
        """Run _handle_fix_auth with patched constants. Returns (rc, stdout, stderr)."""
        out = io.StringIO()
        err = io.StringIO()
        rc: int | str | None = None
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli._handle_fix_auth(self.ws, name)
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_fix_auth_strips_shadow_and_saves_tier(self) -> None:
        """With shadow present and tier data, key is stripped and tier saved."""
        import json

        pdir = self._make_profile("work")
        self._write_tokens(
            {
                "work": {
                    "token": "tok-abc",
                    "created": "2025-01-01",
                    "expires_at": "2026-01-01",
                }
            }
        )
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {
                    "accessToken": "short",
                    "rateLimitTier": "tier4",
                    "subscriptionType": "pro",
                },
                "mcpOAuth": {"keep": "this"},
            },
        )

        rc, out, err = self._run_fix_auth("work")
        self.assertEqual(rc, 0)
        self.assertIn("Removed session credentials from work", out)
        self.assertIn("Saved rate-limit tier: tier4", out)

        # Verify .credentials.json no longer has claudeAiOauth
        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)
        self.assertIn("mcpOAuth", creds)

        # Verify tokens.json has tier fields
        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["work"]["rateLimitTier"], "tier4")
        self.assertEqual(tokens["work"]["subscriptionType"], "pro")

    def test_fix_auth_no_shadow_exits_0(self) -> None:
        """When no claudeAiOauth key exists, prints 'no auth shadow' and exits 0."""
        pdir = self._make_profile("clean")
        self._write_tokens({"clean": "tok-xyz"})
        self._write_credentials(pdir, {"mcpOAuth": {"x": "y"}})

        rc, out, err = self._run_fix_auth("clean")
        self.assertEqual(rc, 0)
        self.assertIn("No auth shadow detected", out)

    def test_fix_auth_no_token_exits_1(self) -> None:
        """When profile has no tokens.json entry, exits 1 with error."""
        self._make_profile("orphan")
        self._write_tokens({})

        rc, out, err = self._run_fix_auth("orphan")
        self.assertEqual(rc, 1)
        self.assertIn("No long-lived token", err)

    def test_fix_auth_shadow_no_tier_data(self) -> None:
        """When shadow exists but has no tier fields, key is stripped without tier save."""
        import json

        pdir = self._make_profile("notier")
        self._write_tokens(
            {
                "notier": {
                    "token": "tok-nt",
                    "created": "2025-01-01",
                    "expires_at": "2026-01-01",
                }
            }
        )
        self._write_credentials(
            pdir,
            {
                "claudeAiOauth": {"accessToken": "short-lived"},
            },
        )

        rc, out, err = self._run_fix_auth("notier")
        self.assertEqual(rc, 0)
        self.assertIn("Removed session credentials from notier", out)
        self.assertNotIn("Saved rate-limit tier", out)

        # Verify .credentials.json no longer has claudeAiOauth
        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)

        # Verify tokens.json was NOT modified (no tier fields)
        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("rateLimitTier", tokens.get("notier", {}))


class WriteTierStubTests(unittest.TestCase):
    """Tests for _write_tier_stub: writing rateLimitTier into .credentials.json at launch."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tokens_file = self.root / "tokens.json"
        self.config_dir = self.root / "profiles" / "work"
        self.config_dir.mkdir(parents=True)

        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(self.root, claude_dir=self.root / ".claude")

    def _write_tokens(self, data: dict[str, Any]) -> None:
        import json

        self.tokens_file.write_text(json.dumps(data))

    def _read_creds(self) -> dict[str, Any]:
        import json

        data: dict[str, Any] = json.loads(
            (self.config_dir / ".credentials.json").read_text()
        )
        return data

    def test_writes_tier_stub(self) -> None:
        """When tokens.json has tier data, .credentials.json gets a stub."""
        self._write_tokens(
            {
                "work": {
                    "token": "tok-1",
                    "rateLimitTier": "default_claude_pro",
                    "subscriptionType": "claude_pro",
                }
            }
        )

        cli._write_tier_stub(self.ws, "work", str(self.config_dir))

        creds = self._read_creds()
        self.assertEqual(creds["claudeAiOauth"]["rateLimitTier"], "default_claude_pro")
        self.assertEqual(creds["claudeAiOauth"]["subscriptionType"], "claude_pro")

    def test_no_tier_no_write(self) -> None:
        """When tokens.json has no tier field, .credentials.json is not created."""
        self._write_tokens({"work": {"token": "tok-2"}})

        cli._write_tier_stub(self.ws, "work", str(self.config_dir))

        self.assertFalse((self.config_dir / ".credentials.json").exists())

    def test_short_circuits_when_matching(self) -> None:
        """When .credentials.json already has the matching tier, no write occurs."""
        import json

        self._write_tokens(
            {
                "work": {
                    "token": "tok-3",
                    "rateLimitTier": "default_claude_pro",
                }
            }
        )
        # Pre-populate matching credentials
        creds_path = self.config_dir / ".credentials.json"
        creds_path.write_text(
            json.dumps({"claudeAiOauth": {"rateLimitTier": "default_claude_pro"}})
        )
        original_mtime = creds_path.stat().st_mtime

        import time

        time.sleep(0.01)  # ensure timestamp differs if rewritten

        cli._write_tier_stub(self.ws, "work", str(self.config_dir))

        # File should not have been rewritten
        self.assertEqual(creds_path.stat().st_mtime, original_mtime)

    def test_overwrites_when_tier_differs(self) -> None:
        """When .credentials.json has a different tier, it is updated."""
        import json

        self._write_tokens(
            {
                "work": {
                    "token": "tok-4",
                    "rateLimitTier": "default_claude_max_5x",
                }
            }
        )
        creds_path = self.config_dir / ".credentials.json"
        creds_path.write_text(
            json.dumps({"claudeAiOauth": {"rateLimitTier": "default_claude_pro"}})
        )

        cli._write_tier_stub(self.ws, "work", str(self.config_dir))

        creds = self._read_creds()
        self.assertEqual(
            creds["claudeAiOauth"]["rateLimitTier"], "default_claude_max_5x"
        )

    def test_no_profile_no_write(self) -> None:
        """When profile is None, nothing happens."""
        self._write_tokens(
            {
                "work": {
                    "token": "tok-5",
                    "rateLimitTier": "default_claude_pro",
                }
            }
        )

        cli._write_tier_stub(self.ws, None, str(self.config_dir))

        self.assertFalse((self.config_dir / ".credentials.json").exists())

    def test_no_config_dir_no_write(self) -> None:
        """When config_dir is None, nothing happens."""
        self._write_tokens(
            {
                "work": {
                    "token": "tok-6",
                    "rateLimitTier": "default_claude_pro",
                }
            }
        )

        cli._write_tier_stub(self.ws, "work", None)

    def test_merges_into_existing_credentials(self) -> None:
        """Existing keys in .credentials.json are preserved."""
        import json

        self._write_tokens(
            {
                "work": {
                    "token": "tok-7",
                    "rateLimitTier": "default_claude_pro",
                }
            }
        )
        creds_path = self.config_dir / ".credentials.json"
        creds_path.write_text(json.dumps({"otherKey": "preserved"}))

        cli._write_tier_stub(self.ws, "work", str(self.config_dir))

        creds = self._read_creds()
        self.assertEqual(creds["otherKey"], "preserved")
        self.assertEqual(creds["claudeAiOauth"]["rateLimitTier"], "default_claude_pro")

    def test_missing_tokens_file_no_crash(self) -> None:
        """When tokens.json doesn't exist, function silently returns."""
        self.assertFalse(self.tokens_file.exists())
        cli._write_tier_stub(self.ws, "work", str(self.config_dir))
        self.assertFalse((self.config_dir / ".credentials.json").exists())


class CheckTokensTests(unittest.TestCase):
    """Tests for the 'profile check-tokens' subcommand."""

    # A realistic-length token (108 chars) for testing truncation
    FULL_TOKEN = "sk-ant-oat01-" + "A" * 95

    def test_all_valid_exit_0(self) -> None:
        """When all profiles have valid tokens, exit code is 0."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="personal",
                path=Path("/fake/personal"),
                has_credentials=True,
                has_token=True,
            ),
            Profile(
                name="work",
                path=Path("/fake/work"),
                has_credentials=True,
                has_token=True,
            ),
        ]
        tokens_data = {
            "personal": self.FULL_TOKEN,
            "work": {"token": self.FULL_TOKEN.replace("A", "B")},
        }

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            return "valid"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        self.assertEqual(rc, 0)
        self.assertIn("valid", out)
        self.assertIn("personal", out)
        self.assertIn("work", out)

    def test_one_invalid_exit_1(self) -> None:
        """When one profile has an invalid token, exit code is 1."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="good",
                path=Path("/fake/good"),
                has_credentials=True,
                has_token=True,
            ),
            Profile(
                name="bad", path=Path("/fake/bad"), has_credentials=True, has_token=True
            ),
        ]
        tokens_data = {
            "good": self.FULL_TOKEN,
            "bad": self.FULL_TOKEN.replace("A", "C"),
        }
        token_good = self.FULL_TOKEN

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            if token == token_good:
                return "valid"
            return "invalid"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        self.assertEqual(rc, 1)
        self.assertIn("valid", out)
        self.assertIn("invalid", out)

    def test_unreachable_exit_1(self) -> None:
        """UNREACHABLE status causes exit 1."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="offline",
                path=Path("/fake/offline"),
                has_credentials=True,
                has_token=True,
            ),
        ]
        tokens_data = {"offline": self.FULL_TOKEN}

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            return "unreachable"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", out)

    def test_no_token_shows_no_token(self) -> None:
        """Profile with no token entry shows 'no token' status."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="empty",
                path=Path("/fake/empty"),
                has_credentials=True,
                has_token=False,
            ),
        ]
        tokens_data: dict[str, Any] = {}

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            raise AssertionError(
                "validate_token should not be called for no-token profiles"
            )

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        self.assertEqual(rc, 0)
        self.assertIn("no token", out)
        self.assertIn("-", out)

    def test_token_never_fully_printed(self) -> None:
        """The full 108-char token must never appear in output."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="secret",
                path=Path("/fake/secret"),
                has_credentials=True,
                has_token=True,
            ),
        ]
        tokens_data = {"secret": self.FULL_TOKEN}

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            return "valid"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        # The full token (108 chars) must not be in output
        self.assertNotIn(self.FULL_TOKEN, out)
        # But the first 20 chars + "..." should be there
        self.assertIn(self.FULL_TOKEN[:20] + "...", out)

    def test_indeterminate_exit_1(self) -> None:
        """INDETERMINATE status causes exit 1."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="weird",
                path=Path("/fake/weird"),
                has_credentials=True,
                has_token=True,
            ),
        ]
        tokens_data = {"weird": self.FULL_TOKEN}

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            return "indeterminate"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        self.assertEqual(rc, 1)
        self.assertIn("indeterminate", out)

    def test_tabular_output_format(self) -> None:
        """Output has header row with Profile, Status, Token columns."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="test",
                path=Path("/fake/test"),
                has_credentials=True,
                has_token=True,
            ),
        ]
        tokens_data = {"test": self.FULL_TOKEN}

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            return "valid"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        lines = out.strip().split("\n")
        self.assertGreaterEqual(len(lines), 2)  # header + at least one row
        self.assertIn("Profile", lines[0])
        self.assertIn("Status", lines[0])
        self.assertIn("Token", lines[0])

    def test_command_registered_and_dispatches(self) -> None:
        """'claudewheel profile check-tokens' dispatches to _handle_check_tokens."""
        with (
            mock.patch("sys.argv", ["c", "profile", "check-tokens"]),
            mock.patch.object(
                cli, "_handle_check_tokens", autospec=True, return_value=0
            ) as mock_handler,
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()

    def test_no_token_profile_among_valid_still_exit_0(self) -> None:
        """Mix of valid tokens and no-token profiles still exits 0."""
        from claudewheel.profile_store import Profile

        profiles = [
            Profile(
                name="has_tok",
                path=Path("/fake/has_tok"),
                has_credentials=True,
                has_token=True,
            ),
            Profile(
                name="no_tok",
                path=Path("/fake/no_tok"),
                has_credentials=True,
                has_token=False,
            ),
        ]
        tokens_data = {"has_tok": self.FULL_TOKEN}

        def fake_validate(token: str, timeout: float = 5.0) -> str:
            return "valid"

        rc, out = self._run_with_patches(profiles, tokens_data, fake_validate)
        self.assertEqual(rc, 0)
        self.assertIn("valid", out)
        self.assertIn("no token", out)

    def test_corrupt_tokens_clean_error_no_traceback(self) -> None:
        """A corrupt tokens.json makes check-tokens fail cleanly: nonzero exit,
        actionable message on stderr, no traceback (mirrors the launch path)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from claudewheel.workspace import Workspace

        ws = Workspace.open(Path(tmp.name), claude_dir=Path(tmp.name) / ".claude")
        tokens_file = ws.tokens_file
        tokens_file.write_text("{ not valid json")

        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._handle_check_tokens(ws)

        self.assertNotEqual(rc, 0)
        msg = err.getvalue()
        self.assertIn(str(tokens_file), msg)
        self.assertIn("corrupt", msg)
        self.assertNotIn("Traceback", msg)

    def _run_with_patches(
        self,
        profiles: list["Profile"],
        tokens_data: dict[str, Any],
        validate_fn: Callable[..., str],
    ) -> tuple[int, str]:
        """Helper to run _handle_check_tokens with clean patches."""

        buf = io.StringIO()
        ws = mock.MagicMock()
        ws.tokens.load.return_value = tokens_data
        ws.profiles.enumerate.return_value = profiles

        with (
            mock.patch(
                "claudewheel.auth.validate_token",
                autospec=True,
                side_effect=validate_fn,
            ),
            redirect_stdout(buf),
        ):
            rc = cli._handle_check_tokens(ws)

        return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Profile rename command
# ---------------------------------------------------------------------------


class RenameProfileDispatchTests(unittest.TestCase):
    """'profile rename old new' dispatches to the handler with correct args."""

    def test_dispatches(self) -> None:
        with (
            mock.patch("sys.argv", ["c", "profile", "rename", "alpha", "beta"]),
            mock.patch.object(
                cli, "_handle_rename_profile", autospec=True, return_value=0
            ) as mock_handler,
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()
        self.assertEqual(mock_handler.call_args.kwargs["old"], "alpha")
        self.assertEqual(mock_handler.call_args.kwargs["new"], "beta")


class RenameProfileHandlerTests(unittest.TestCase):
    """_handle_rename_profile validates inputs and delegates to rename_profile."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.profiles_dir = self.home / ".claudewheel" / "profiles"
        self.profiles_dir.mkdir(parents=True)
        self.options_file = self.home / ".claudewheel" / "options.json"
        self.tokens_file = self.home / ".claudewheel" / "tokens.json"
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )

    def _write_options(
        self, values: list[str], pinned: list[str] | None = None
    ) -> None:
        data = {"profile": {"values": values}}
        if pinned:
            data["profile"]["pinned"] = pinned
        self.options_file.write_text(json.dumps(data))

    def test_old_not_found_exits(self) -> None:
        self._write_options([])
        with (
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._handle_rename_profile(self.ws, "ghost", "newname")
        self.assertEqual(ctx.exception.code, 1)

    def test_bad_chars_exits(self) -> None:
        (self.profiles_dir / "old").mkdir()
        self._write_options(["old"])
        with (
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._handle_rename_profile(self.ws, "old", "UPPER")
        self.assertEqual(ctx.exception.code, 1)

    def test_default_name_rejected(self) -> None:
        (self.profiles_dir / "old").mkdir()
        self._write_options(["old"])
        with (
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._handle_rename_profile(self.ws, "old", "default")
        self.assertEqual(ctx.exception.code, 1)

    def test_target_exists_exits(self) -> None:
        (self.profiles_dir / "src").mkdir()
        (self.profiles_dir / "dst").mkdir()
        self._write_options(["src", "dst"])
        with (
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._handle_rename_profile(self.ws, "src", "dst")
        self.assertEqual(ctx.exception.code, 1)

    def test_running_profile_exits(self) -> None:
        (self.profiles_dir / "active").mkdir()
        self._write_options(["active"])
        self.tokens_file.write_text("{}")
        with (
            mock.patch(
                "claudewheel.profile_ops._is_profile_running",
                autospec=True,
                return_value=True,
            ),
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._handle_rename_profile(self.ws, "active", "newname")
        self.assertEqual(ctx.exception.code, 1)

    def test_success_calls_store_rename(self) -> None:
        (self.profiles_dir / "old").mkdir()
        self._write_options(["old"])
        self.tokens_file.write_text("{}")
        buf = io.StringIO()
        with (
            mock.patch(
                "claudewheel.profile_ops._is_profile_running",
                autospec=True,
                return_value=False,
            ),
            mock.patch(
                "claudewheel.profile_store.ProfileStore.rename", autospec=True
            ) as mock_rename,
            redirect_stdout(buf),
        ):
            rc = cli._handle_rename_profile(self.ws, "old", "new-name")
        self.assertEqual(rc, 0)
        # autospec on a class method records the instance as the first positional
        # arg (self); assert the remaining args match the rename request exactly.
        mock_rename.assert_called_once()
        self.assertEqual(mock_rename.call_args.args[1:], ("old", "new-name"))
        self.assertIn("Renamed", buf.getvalue())

    def test_token_conflict_exits(self) -> None:
        (self.profiles_dir / "src").mkdir()
        self._write_options(["src"])
        self.tokens_file.write_text(json.dumps({"dst": "tok-conflict"}))
        with (
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._handle_rename_profile(self.ws, "src", "dst")
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
