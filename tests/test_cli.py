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
        strictcli's app.run() calls sys.exit(0) after the handler, so we catch that.
        """
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()

        with (
            mock.patch("sys.argv", argv),
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
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
        fake_cfg = self._make_cfg(last_config={})
        launch_mock = mock.MagicMock()

        with (
            mock.patch("sys.argv", [
                "c",
                "--cont",
                "--profile", "personal",
                "--github", "ghuser",
                "-s", "version=2.1.116",
                "--directory", "/some/dir",
            ]),
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("claudewheel.cli._check_cont_session"),
            mock.patch("os.getcwd", return_value="/test/dir"),
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
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
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
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit:
                pass

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


class BuildAppTests(unittest.TestCase):
    """Smoke test that _build_app() constructs successfully.

    Regression guard: strictcli >=0.16.0 requires every repeatable=True Flag
    to pass unique=True or unique=False explicitly. The `-s/--set` flag in
    _build_app() must comply, otherwise the entire binary crashes at startup
    with ValueError before main() can do anything useful.
    """

    def test_build_app_constructs_without_error(self) -> None:
        from strictcli import App

        app = cli._build_app()
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

    def _make_cfg(self, last_config: dict | None = None) -> _FakeCfg:
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
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()
        with (
            mock.patch("sys.argv", argv),
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
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
                ["c", "-s", "profile=work", "-s", "profile=personal", "--print-prompt", "x"],
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
                ["c", "-s", "profile=work", "-s", "profile=work", "--print-prompt", "x"],
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
                ["c", "--profile", "work", "-s", "profile=personal", "--print-prompt", "x"],
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

    def _make_cfg(self, last_config: dict | None = None) -> _FakeCfg:
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
        fake_cfg = self._make_cfg(last_config)
        launch_mock = mock.MagicMock()
        with (
            mock.patch("sys.argv", argv),
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", launch_mock),
            mock.patch("os.getcwd", return_value="/test/dir"),
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
                "c", "--picker",
                "--profile", "personal",
                "--github", "ghuser",
                "-s", "version=2.1.116",
                "--directory", "/some/dir",
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
                "--profile", "personal",
                "--github", "ghuser",
                "-s", "version=2.1.116",
                "--directory", "/some/dir",
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
        self.shared_dir = Path(self._tmp.name) / "shared"
        self.shared_dir.mkdir()
        (self.shared_dir / "projects").mkdir()

        self._patch_shared = mock.patch.object(cli, "SHARED_DIR", self.shared_dir)
        self._patch_shared.start()
        self.addCleanup(self._patch_shared.stop)

    # -- 5.1a: Session exists under current dir -> no interception --

    def test_resume_session_in_current_dir_no_interception(self) -> None:
        """When the session file exists under the current directory's encoded path,
        the function returns immediately without calling find_session."""
        from claudewheel.constants import encode_path

        current_dir = "/home/user/my-project"
        session_id = "abc-123-def"
        encoded = encode_path(os.path.abspath(current_dir))

        # Create the expected session file
        project_dir = self.shared_dir / "projects" / encoded
        project_dir.mkdir(parents=True)
        (project_dir / f"{session_id}.jsonl").write_text('{"cwd":"/home/user/my-project"}\n')

        with mock.patch("claudewheel.session.find_session") as mock_find:
            cli._check_resume_session(session_id, current_dir)

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
            mock.patch("claudewheel.session.find_session", return_value=info),
            mock.patch("claudewheel.mv.run_mv", side_effect=[dry_result, real_result]) as mock_mv,
            mock.patch("builtins.input", side_effect=["y", "y"]),
            mock.patch("os.path.isdir", return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            # Should return normally (no sys.exit)
            cli._check_resume_session(session_id, current_dir)

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
        (old_project_dir / f"{session_id}.jsonl").write_text('{"cwd":"/home/user/old-project"}\n')

        with (
            mock.patch("claudewheel.session.find_session", return_value=info),
            mock.patch("claudewheel.mv.run_mv") as mock_mv,
            mock.patch("builtins.input", return_value="n"),
            mock.patch("os.path.isdir", return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(session_id, current_dir)
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
        (old_project_dir / f"{session_id}.jsonl").write_text('{"cwd":"/home/user/old-project"}\n')

        dry_result = mock.MagicMock()
        dry_result.dirs_renamed = 1
        dry_result.files_rewritten = 2
        dry_result.lines_replaced = 5
        dry_result.project_keys_updated = 1

        with (
            mock.patch("claudewheel.session.find_session", return_value=info),
            mock.patch("claudewheel.mv.run_mv", return_value=dry_result) as mock_mv,
            mock.patch("builtins.input", side_effect=["y", "n"]),
            mock.patch("os.path.isdir", return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(session_id, current_dir)
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
            mock.patch("claudewheel.session.find_session", return_value=None),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(session_id, current_dir)
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
            mock.patch("claudewheel.session.find_session", return_value=info),
            mock.patch("os.path.isdir", return_value=True),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._check_resume_session(session_id, current_dir)
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
            mock.patch("sys.argv", [
                "c", "--resume", "",
                "--profile", "personal",
                "--github", "ghuser",
                "-s", "version=2.1.116",
                "--directory", "/some/dir",
            ]),
            mock.patch("claudewheel.config.ConfigManager", return_value=fake_cfg),
            mock.patch("claudewheel.cli._do_launch_sequence", mock.MagicMock()),
            mock.patch("claudewheel.cli._check_resume_session") as mock_check,
            mock.patch("os.getcwd", return_value="/test/dir"),
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
        self.shared_dir = Path(self._tmp.name) / "shared"
        self.shared_dir.mkdir()
        (self.shared_dir / "projects").mkdir()

        self._patch_shared = mock.patch.object(cli, "SHARED_DIR", self.shared_dir)
        self._patch_shared.start()
        self.addCleanup(self._patch_shared.stop)

    def _create_project(self, encoded_cwd: str, session_count: int = 1,
                        cwd: str = "/home/user/my-project") -> Path:
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
        from claudewheel.constants import encode_path

        current_dir = "/home/user/my-project"
        encoded = encode_path(os.path.abspath(current_dir))
        self._create_project(encoded, session_count=2, cwd=current_dir)

        with mock.patch("claudewheel.session.find_orphaned_project_dirs") as mock_find:
            cli._check_cont_session(current_dir)

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
            mock.patch("claudewheel.session.find_orphaned_project_dirs", return_value=[orphan]),
            mock.patch("claudewheel.mv.run_mv", side_effect=[dry_result, real_result]) as mock_mv,
            mock.patch("builtins.input", side_effect=["y", "y"]),
            redirect_stdout(io.StringIO()),
        ):
            cli._check_cont_session(current_dir)

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
            mock.patch("claudewheel.session.find_orphaned_project_dirs", return_value=[orphan]),
            mock.patch("claudewheel.mv.run_mv") as mock_mv,
            mock.patch("builtins.input", return_value="n"),
            redirect_stdout(io.StringIO()),
        ):
            # Should return normally (no sys.exit)
            cli._check_cont_session(current_dir)

        mock_mv.assert_not_called()

    # -- No sessions, no candidates --

    def test_cont_no_sessions_no_candidates(self) -> None:
        """No orphans found, returns silently."""
        current_dir = os.path.abspath("/home/user/new-project")

        with (
            mock.patch("claudewheel.session.find_orphaned_project_dirs", return_value=[]),
            mock.patch("claudewheel.mv.run_mv") as mock_mv,
            mock.patch("builtins.input") as mock_input,
        ):
            cli._check_cont_session(current_dir)

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
            mock.patch("claudewheel.session.find_orphaned_project_dirs", return_value=[orphan1, orphan2]),
            mock.patch("claudewheel.mv.run_mv", side_effect=[dry_result, real_result]) as mock_mv,
            mock.patch("builtins.input", side_effect=["2", "y"]),
            redirect_stdout(io.StringIO()),
        ):
            cli._check_cont_session(current_dir)

        self.assertEqual(mock_mv.call_count, 2)
        # Verify the selected orphan (orphan2) was passed to run_mv
        args1, _ = mock_mv.call_args_list[0]
        self.assertEqual(args1[0], "/home/user/beta")


class MvPostHocFlagTests(unittest.TestCase):
    """Verify --post-hoc flag is passed through to run_mv."""

    def test_post_hoc_flag_passed_to_run_mv(self) -> None:
        """When --post-hoc is given, run_mv is called with post_hoc=True."""
        with (
            mock.patch("sys.argv", ["c", "mv", "/old/path", "/new/path", "--post-hoc"]),
            mock.patch("claudewheel.mv.run_mv") as mock_run_mv,
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
            mock.patch("claudewheel.mv.run_mv") as mock_run_mv,
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

        self.terminal = mock.MagicMock()
        self.terminal._in_raw = False

        self._patches = {
            "terminal_cls": mock.patch(
                "claudewheel.terminal.Terminal", return_value=self.terminal),
            "config": mock.patch("claudewheel.config.ConfigManager"),
            "discover": mock.patch(
                "claudewheel.discovery.discover_profiles", return_value=[]),
            "wizard": mock.patch(
                "claudewheel.wizard.run_profile_wizard", autospec=True),
            "create": mock.patch(
                "claudewheel.wizard.create_profile",
                return_value=["Created profile 'p':", "  Config dir: /x"]),
            "auth": mock.patch(
                "claudewheel.wizard.run_auth_flow", autospec=True,
                return_value="authenticated"),
            "page": mock.patch("claudewheel.ui.show_page"),
        }
        self.mocks = {}
        for name, p in self._patches.items():
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)

        # parse_theme runs for real on the mocked ConfigManager's theme dict
        self.mocks["config"].return_value.theme = DEFAULT_THEME_DARK

        wizard_result = mock.MagicMock()
        wizard_result.cancelled = False
        wizard_result.name = "p"
        wizard_result.config_dir = "~/.claudewheel/profiles/p"
        self.wizard_result = wizard_result
        self.mocks["wizard"].return_value = wizard_result

    def _run(self) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli._handle_new_profile()
        return rc, buf.getvalue()

    def test_terminal_enters_alt_screen_raw_session(self) -> None:
        self._run()
        self.terminal.enter_raw.assert_called_once_with(alt_screen=True)
        self.terminal.exit_raw.assert_called_once()
        self.terminal.close.assert_called_once()

    def test_wizard_gets_theme_and_cli_terminal(self) -> None:
        self._run()
        args = self.mocks["wizard"].call_args.args
        self.assertEqual(args[0], [])
        self.assertIs(args[2], self.terminal)

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
        self.assertLess(call_names.index("run_auth_flow"),
                        call_names.index("show_page"))

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

    def _report(self, **overrides):
        from claudewheel.profile_info import ProfileReport
        from pathlib import Path as _P
        kwargs = dict(
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
        with mock.patch("claudewheel.profile_info.gather_profile_info",
                        return_value=report) as mock_gather, \
                redirect_stdout(buf):
            rc = cli._handle_show_profile("work")
        self.assertEqual(rc, 0)
        mock_gather.assert_called_once_with("work")
        out = buf.getvalue()
        self.assertIn("Profile: work", out)
        self.assertIn("Credentials file: present", out)

    def test_unknown_profile_exits_1(self) -> None:
        report = self._report(exists=False, registered=False,
                              has_credentials=False)
        err = io.StringIO()
        with mock.patch("claudewheel.profile_info.gather_profile_info",
                        return_value=report), \
                redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli._handle_show_profile("work")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("not found", err.getvalue())

    def test_token_only_profile_is_shown(self) -> None:
        """A profile known only via tokens.json is still inspectable."""
        report = self._report(exists=False, registered=False,
                              has_credentials=False, has_token=True)
        buf = io.StringIO()
        with mock.patch("claudewheel.profile_info.gather_profile_info",
                        return_value=report), \
                redirect_stdout(buf):
            rc = cli._handle_show_profile("work")
        self.assertEqual(rc, 0)
        self.assertIn("Profile: work", buf.getvalue())


class DeleteProfileHandlerTests(unittest.TestCase):
    """_handle_delete_profile wires both force flags to do_delete_profile."""

    def test_flags_wire_through(self) -> None:
        with mock.patch("claudewheel.profile_ops.do_delete_profile",
                        return_value=0) as mock_del:
            rc = cli._handle_delete_profile(
                "work", force_delete=True, force_delete_data=True)
        self.assertEqual(rc, 0)
        mock_del.assert_called_once_with("work", force=True, force_data=True)

    def test_default_flags_off(self) -> None:
        with mock.patch("claudewheel.profile_ops.do_delete_profile",
                        return_value=0) as mock_del:
            rc = cli._handle_delete_profile(
                "work", force_delete=False, force_delete_data=False)
        self.assertEqual(rc, 0)
        mock_del.assert_called_once_with("work", force=False, force_data=False)

    def test_nonzero_rc_exits(self) -> None:
        with mock.patch("claudewheel.profile_ops.do_delete_profile",
                        return_value=1):
            with self.assertRaises(SystemExit) as ctx:
                cli._handle_delete_profile(
                    "work", force_delete=False, force_delete_data=False)
        self.assertEqual(ctx.exception.code, 1)


class ProfileGroupDispatchTests(unittest.TestCase):
    """Verify 'profile create/delete/show' dispatch to the correct handlers."""

    def test_profile_create_dispatches(self) -> None:
        """'claudewheel profile create' calls _handle_new_profile."""
        with (
            mock.patch("sys.argv", ["c", "profile", "create"]),
            mock.patch.object(cli, "_handle_new_profile", return_value=0) as mock_handler,
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()

    def test_profile_delete_dispatches(self) -> None:
        """'claudewheel profile delete work' calls _handle_delete_profile."""
        with (
            mock.patch("sys.argv", ["c", "profile", "delete", "work"]),
            mock.patch.object(cli, "_handle_delete_profile", return_value=0) as mock_handler,
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        mock_handler.assert_called_once()
        # strictcli passes args as kwargs
        self.assertEqual(mock_handler.call_args.kwargs["name"], "work")

    def test_profile_show_dispatches(self) -> None:
        """'claudewheel profile show work' calls _handle_show_profile."""
        with (
            mock.patch("sys.argv", ["c", "profile", "show", "work"]),
            mock.patch.object(cli, "_handle_show_profile", return_value=0) as mock_handler,
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


if __name__ == "__main__":
    unittest.main()
