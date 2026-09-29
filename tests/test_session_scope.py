"""Tests for the systemd user scope every launched session runs in.

claudewheel starts each session through ``systemd-run --user --scope`` with a
memory ceiling from config.json, so a command that runs away inside the session
(one the heavy-unwrapped guardrail misses, such as a suite inside ``bash -c``)
is killed inside that scope instead of taking the whole terminal down.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.defaults import DEFAULT_CONFIG
from claudewheel.launch import SessionScope, do_launch
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    FakeAppConfigStore,
    inert_workspace,
    setup_temp_config_dir,
)

_SYSTEMD_RUN = "/usr/bin/systemd-run"


class SessionScopeConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["session_memory_max"], "4G")
        self.assertEqual(DEFAULT_CONFIG["session_memory_swap_max"], "1G")
        scope = SessionScope.from_config(DEFAULT_CONFIG)
        self.assertEqual(scope, SessionScope("4G", "1G"))

    def test_configured_values_are_used(self) -> None:
        scope = SessionScope.from_config(
            {"session_memory_max": "6G", "session_memory_swap_max": "512M"}
        )
        self.assertEqual(scope, SessionScope("6G", "512M"))

    def test_swap_may_be_zero(self) -> None:
        scope = SessionScope.from_config(
            {"session_memory_max": "6G", "session_memory_swap_max": "0"}
        )
        self.assertEqual(scope.memory_swap_max, "0")

    def test_malformed_sizes_are_refused(self) -> None:
        for key, value in (
            ("session_memory_max", "0"),
            ("session_memory_max", "4GB"),
            ("session_memory_max", "4g"),
            ("session_memory_max", "1.5G"),
            ("session_memory_max", "infinity"),
            ("session_memory_max", 4),
            ("session_memory_swap_max", "-1G"),
            ("session_memory_swap_max", ""),
            ("session_memory_swap_max", None),
        ):
            with self.subTest(key=key, value=value):
                config = {**DEFAULT_CONFIG, key: value}
                with self.assertRaises(ValueError) as ctx:
                    SessionScope.from_config(config)
                self.assertIn(f"config.json {key}", str(ctx.exception))
                self.assertIn("K, M, G, or T suffix", str(ctx.exception))

    def test_a_missing_key_is_refused(self) -> None:
        for key in ("session_memory_max", "session_memory_swap_max"):
            with self.subTest(key=key):
                config = {k: v for k, v in DEFAULT_CONFIG.items() if k != key}
                with self.assertRaises(ValueError) as ctx:
                    SessionScope.from_config(config)
                self.assertIn(f"config.json has no {key}", str(ctx.exception))
                self.assertIn("restart claudewheel", str(ctx.exception))

    def test_restarting_claudewheel_adds_a_missing_key(self) -> None:
        # The fix the missing-key error names: claudewheel's startup adds the
        # keys with their defaults, and keeps a value already set.
        with tempfile.TemporaryDirectory() as tmp:
            paths = setup_temp_config_dir(
                Path(tmp), config={"theme": "dark", "session_memory_max": "6G"}
            )
            config = Workspace.open(paths["CONFIG_DIR"]).appconfig().config
        self.assertEqual(SessionScope.from_config(config), SessionScope("6G", "1G"))


class DoLaunchScopeTests(unittest.TestCase):
    """do_launch execs systemd-run, which execs the client inside the scope."""

    def _launch(
        self, argv: list[str], scope: SessionScope, env: dict[str, str] | None = None
    ) -> tuple[mock.MagicMock, str]:
        err = io.StringIO()
        with (
            mock.patch("shutil.which", autospec=True, return_value=_SYSTEMD_RUN),
            mock.patch("os.chdir", autospec=True),
            mock.patch("os.execvpe", autospec=True) as m_exec,
            mock.patch("os.getpid", return_value=4242),
            mock.patch("time.time", return_value=1790000000.5),
            redirect_stderr(err),
        ):
            do_launch("/work", argv, env if env is not None else {}, scope)
        return m_exec, err.getvalue()

    def test_the_client_runs_inside_a_capped_scope(self) -> None:
        argv = ["/opt/claude/bin/claude", "--model", "m-1"]
        env = {"PATH": "/usr/bin"}
        m_exec, _ = self._launch(argv, SessionScope("4G", "1G"), env)
        m_exec.assert_called_once_with(
            _SYSTEMD_RUN,
            [
                _SYSTEMD_RUN,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "--expand-environment=no",
                "--unit=claudewheel-session-4242-1790000000.scope",
                "-p",
                "MemoryMax=4G",
                "-p",
                "MemorySwapMax=1G",
                "-p",
                "OOMPolicy=continue",
                "--",
                *argv,
            ],
            env,
        )

    def test_the_ceiling_is_printed_at_launch(self) -> None:
        _, stderr = self._launch(["/bin/claude"], SessionScope("6G", "0"))
        self.assertEqual(
            stderr,
            "claudewheel: this session runs in "
            "claudewheel-session-4242-1790000000.scope, capped at 6G of memory "
            "and 0 of swap (session_memory_max and session_memory_swap_max in "
            "config.json)\n",
        )

    def test_systemd_run_is_looked_up_on_the_launch_path(self) -> None:
        with (
            mock.patch("shutil.which", autospec=True, return_value=None) as m_which,
            mock.patch("os.execvpe", autospec=True) as m_exec,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(OSError) as ctx:
                do_launch(
                    "/work",
                    ["/bin/claude"],
                    {"PATH": "/x:/y"},
                    SessionScope("4G", "1G"),
                )
        m_which.assert_called_once_with("systemd-run", path="/x:/y")
        m_exec.assert_not_called()
        self.assertIn("systemd-run is not on PATH", str(ctx.exception))


class LaunchSequenceScopeTests(unittest.TestCase):
    """_do_launch_sequence reads the ceiling from config.json before launching."""

    def _run(self, config: dict[str, object]) -> tuple[mock.MagicMock, str, int]:
        cfg = FakeAppConfigStore()
        cfg.config.update(config)
        do_launch_mock = mock.MagicMock()
        err = io.StringIO()
        code = 0
        with (
            mock.patch("claudewheel.preflight.PREFLIGHT_STEPS", []),
            mock.patch("claudewheel.hooks.run_hooks", autospec=True, return_value=True),
            mock.patch(
                "claudewheel.launch.resolve_launch_config",
                autospec=True,
                return_value=("/cwd", ["/bin/claude"], {}),
            ),
            mock.patch("claudewheel.launch.do_launch", do_launch_mock),
            redirect_stderr(err),
        ):
            try:
                cli._do_launch_sequence(
                    inert_workspace(Path("/nonexistent-cw-root")),
                    mock.MagicMock(),
                    cfg,
                    {"profile": "p"},
                    interactive=False,
                )
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return do_launch_mock, err.getvalue(), code

    def test_the_configured_ceiling_reaches_do_launch(self) -> None:
        do_launch_mock, _, code = self._run({"session_memory_max": "6G"})
        self.assertEqual(code, 0)
        do_launch_mock.assert_called_once_with(
            "/cwd", ["/bin/claude"], {}, SessionScope("6G", "1G")
        )

    def test_a_malformed_ceiling_fails_the_launch(self) -> None:
        do_launch_mock, stderr, code = self._run({"session_memory_max": "lots"})
        self.assertEqual(code, 1)
        do_launch_mock.assert_not_called()
        self.assertIn("Launch failed: config.json session_memory_max", stderr)

    def test_fixing_the_ceiling_clears_the_failure(self) -> None:
        # The fix the error names: a whole number with a suffix.
        do_launch_mock, stderr, code = self._run({"session_memory_max": "8G"})
        self.assertEqual(code, 0, stderr)
        do_launch_mock.assert_called_once()


class NestedScopeTests(unittest.TestCase):
    """Where a scope started from inside another scope lands.

    heavy runs its command with 'systemd-run --user --scope' from inside the
    session's scope. The user manager places every transient scope in its own
    slice, not under the scope of the process that asked for it, so the heavy
    scope is a sibling of the session scope: a heavy job's memory counts
    against its own cap only, never against the session's ceiling too.

    Skipped where no systemd user manager answers (a CI container).
    """

    def setUp(self) -> None:
        if (
            shutil.which("systemd-run") is None
            or subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", "true"],
                capture_output=True,
                timeout=20,
            ).returncode
            != 0
        ):
            self.skipTest("no systemd user manager")

    def test_a_scope_started_inside_a_scope_is_its_sibling(self) -> None:
        tag = f"{os.getpid()}-{os.urandom(4).hex()}"
        proc = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit=claudewheel-test-outer-{tag}.scope",
                "-p",
                "MemoryMax=256M",
                "--",
                "bash",
                "-c",
                'cat /proc/self/cgroup; systemd-run --user --scope --quiet --collect "--unit=$1" '
                "-p MemoryMax=128M -- cat /proc/self/cgroup",
                "bash",
                f"claudewheel-test-inner-{tag}.scope",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outer, inner = (line.split("::", 1)[1] for line in proc.stdout.splitlines())
        self.assertTrue(outer.endswith(f"/claudewheel-test-outer-{tag}.scope"), outer)
        self.assertTrue(inner.endswith(f"/claudewheel-test-inner-{tag}.scope"), inner)
        self.assertFalse(inner.startswith(outer + "/"), (outer, inner))
        self.assertEqual(os.path.dirname(inner), os.path.dirname(outer))


if __name__ == "__main__":
    unittest.main()
