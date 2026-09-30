"""Tests for installing, checking, and stopping claudewheel-probe-runner.service.

deploy-hooks writes the unit and drives systemctl; health checks the result.
A stub systemctl first on PATH records every call and keeps the state a real
user manager would report, so no test touches the real one.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.health import check_probe_runner
from claudewheel.hook_scripts import service_unit
from claudewheel.probe import SERVICE_NAME
from claudewheel.workspace import Workspace
from tests.wheelhelpers import install_stub_systemctl


class _ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.root = base / "cw"
        env = mock.patch.dict(
            "os.environ",
            {"CLAUDEWHEEL_CONFIG_DIR": str(self.root), "HOME": str(self.home)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.log, self.state = install_stub_systemctl(self, base)
        self.ws = Workspace.open(self.root)
        self.unit = self.ws.systemd_user_dir / SERVICE_NAME

    def deploy(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            mock.patch("sys.argv", ["c", "deploy-hooks", *extra]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                code = cli.main() or 0  # type: ignore[func-returns-value]
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


class DeployServiceTests(_ServiceCase):
    def test_the_unit_is_written_enabled_and_started(self) -> None:
        code, out, err = self.deploy(SERVICE_NAME)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.unit.read_text(), service_unit(sys.executable, self.root))
        self.assertEqual(
            self.calls(),
            [
                "--user daemon-reload",
                f"--user enable {SERVICE_NAME}",
                f"--user restart {SERVICE_NAME}",
            ],
        )
        self.assertIn(f"created: {self.unit}", out)
        self.assertEqual(list(self.ws.scripts_dir.glob("*")), [])

    def test_the_unit_runs_the_runner_restarts_on_failure_and_starts_at_login(
        self,
    ) -> None:
        unit = service_unit("/py/bin/python3", Path("/cw root"))
        self.assertIn('ExecStart="/py/bin/python3" -m claudewheel.probe_runner\n', unit)
        self.assertIn('Environment="CLAUDEWHEEL_CONFIG_DIR=/cw root"\n', unit)
        self.assertIn("Restart=on-failure\n", unit)
        self.assertIn("WantedBy=default.target\n", unit)

    def test_an_existing_unit_is_left_alone_but_started(self) -> None:
        self.deploy(SERVICE_NAME)
        self.unit.write_text("# hand-edited\n")
        code, out, err = self.deploy(SERVICE_NAME)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.unit.read_text(), "# hand-edited\n")
        self.assertEqual(self.calls()[-1], f"--user enable --now {SERVICE_NAME}")
        self.assertIn("already exists", out)

    def test_force_overwrite_rewrites_and_restarts(self) -> None:
        self.deploy(SERVICE_NAME)
        self.unit.write_text("# hand-edited\n")
        code, out, err = self.deploy(SERVICE_NAME, "--force-overwrite")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.unit.read_text(), service_unit(sys.executable, self.root))
        self.assertEqual(self.calls()[-1], f"--user restart {SERVICE_NAME}")
        self.assertIn("overwritten", out)

    def test_all_installs_the_service_too(self) -> None:
        code, _, err = self.deploy("--all")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.unit.exists())
        self.assertTrue((self.ws.scripts_dir / "hook-wait-for-probe-reports").exists())

    def test_a_failing_systemctl_is_an_error(self) -> None:
        with mock.patch.dict("os.environ", {"STUB_FAIL": "restart"}):
            code, _, err = self.deploy(SERVICE_NAME)
        self.assertEqual(code, 1)
        self.assertIn("systemctl --user restart", err)
        self.assertIn("is not running", err)

    def test_dry_run_writes_and_runs_nothing(self) -> None:
        code, out, err = self.deploy(SERVICE_NAME, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.unit.exists())
        self.assertEqual(self.calls(), [])
        self.assertIn("would create", out)


class ProbeRunnerHealthTests(_ServiceCase):
    """Each failure names a fix, and performing the fix clears it."""

    def assert_fixed_by_deploying(self, needle: str) -> None:
        result = check_probe_runner(self.ws)
        self.assertFalse(result.ok)
        self.assertIn(needle, result.detail)
        self.assertIn(
            f"claudewheel deploy-hooks {SERVICE_NAME} --force-overwrite", result.detail
        )
        code, _, err = self.deploy(SERVICE_NAME, "--force-overwrite")
        self.assertEqual(code, 0, err)
        result = check_probe_runner(self.ws)
        self.assertTrue(result.ok, result.detail)

    def test_not_installed(self) -> None:
        self.assert_fixed_by_deploying("is not installed")

    def test_a_unit_that_differs(self) -> None:
        self.deploy(SERVICE_NAME)
        self.unit.write_text("# stale\n")
        self.assert_fixed_by_deploying("differs")

    def test_disabled(self) -> None:
        self.deploy(SERVICE_NAME)
        self.state.with_suffix(".enabled").write_text("disabled\n")
        self.assert_fixed_by_deploying("is disabled, not enabled")

    def test_stopped(self) -> None:
        self.deploy(SERVICE_NAME)
        self.state.with_suffix(".active").write_text("inactive\n")
        self.assert_fixed_by_deploying("is inactive, not running")

    def test_ok(self) -> None:
        self.deploy(SERVICE_NAME)
        result = check_probe_runner(self.ws)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.label, "probe-runner")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
