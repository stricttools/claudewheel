"""Tests for the heavy wrapper claudewheel ships and deploy-hooks deploys.

deploy-hooks writes the wrapper into the scripts directory like every other
script, then keeps ``~/.local/bin/heavy`` a symlink to that copy, so the
``heavy`` on PATH is always the one claudewheel ships. The behavior tests run
the deployed script under bash with a stub ``systemd-run`` first on PATH, so
they exercise the real lock, holder note, GOFLAGS, and exit-code handling
without starting a systemd scope.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.hook_scripts import HOOK_SCRIPTS, PATH_COMMANDS

# Records systemd-run's arguments and the GOFLAGS it was started with, then runs
# the command after "--" the way systemd-run --scope does: in the foreground,
# with its exit code.
_STUB_SYSTEMD_RUN = """\
#!/usr/bin/env bash
printf '%s\\n' "$@" > "$STUB_LOG"
printf 'GOFLAGS=%s\\n' "${GOFLAGS-}" >> "$STUB_LOG"
while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
shift
exec "$@"
"""


class _DeployCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.home.mkdir()
        self.scripts_dir = base / "cw" / "scripts"
        self.bin_dir = self.home / ".local" / "bin"
        self.link = self.bin_dir / "heavy"
        env = mock.patch.dict(
            "os.environ",
            {"CLAUDEWHEEL_CONFIG_DIR": str(base / "cw"), "HOME": str(self.home)},
        )
        env.start()
        self.addCleanup(env.stop)

    def _run_deploy(self, argv: list[str]) -> tuple[str, str, int]:
        out = io.StringIO()
        err = io.StringIO()
        code = 0
        with (
            mock.patch("sys.argv", argv),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return out.getvalue(), err.getvalue(), code


class HeavyShippedTests(unittest.TestCase):
    def test_heavy_is_a_deployable_path_command(self) -> None:
        self.assertIn("heavy", HOOK_SCRIPTS)
        self.assertIn("heavy", PATH_COMMANDS)

    def test_every_path_command_is_a_deployable_script(self) -> None:
        for name in PATH_COMMANDS:
            self.assertIn(name, HOOK_SCRIPTS)

    def test_heavy_is_valid_bash(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", "/dev/stdin"],
            input=HOOK_SCRIPTS["heavy"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(HOOK_SCRIPTS["heavy"].startswith("#!/usr/bin/env bash\n"))


class HeavyDeployTests(_DeployCase):
    def test_deploy_heavy_writes_the_shipped_source_and_links_it(self) -> None:
        stdout, stderr, code = self._run_deploy(["c", "deploy-hooks", "heavy"])
        self.assertEqual(code, 0, stderr)

        dest = self.scripts_dir / "heavy"
        self.assertEqual(dest.read_text(), HOOK_SCRIPTS["heavy"])
        self.assertTrue(dest.stat().st_mode & stat.S_IXUSR)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(dest))
        self.assertEqual(self.link.read_text(), HOOK_SCRIPTS["heavy"])
        self.assertIn(f"linked: {self.link} -> {dest}", stdout)

    def test_deploy_all_links_heavy(self) -> None:
        _, stderr, code = self._run_deploy(["c", "deploy-hooks", "--all"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(os.readlink(self.link), str(self.scripts_dir / "heavy"))

    def test_deploying_a_hook_script_creates_no_link(self) -> None:
        self._run_deploy(["c", "deploy-hooks", "hook-timestamp"])
        self.assertFalse(self.link.exists() or self.link.is_symlink())

    def test_redeploy_keeps_the_link(self) -> None:
        self._run_deploy(["c", "deploy-hooks", "heavy"])
        stdout, stderr, code = self._run_deploy(["c", "deploy-hooks", "heavy"])
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"already linked: {self.link}", stdout)
        self.assertEqual(os.readlink(self.link), str(self.scripts_dir / "heavy"))

    def test_foreign_heavy_is_refused_without_force_overwrite(self) -> None:
        self.bin_dir.mkdir(parents=True)
        self.link.write_text("#!/bin/sh\necho hand-written\n")

        _, stderr, code = self._run_deploy(["c", "deploy-hooks", "heavy"])

        self.assertEqual(code, 1)
        self.assertIn(str(self.link), stderr)
        self.assertIn("--force-overwrite", stderr)
        self.assertFalse(self.link.is_symlink())
        self.assertEqual(self.link.read_text(), "#!/bin/sh\necho hand-written\n")

    def test_force_overwrite_replaces_a_foreign_heavy(self) -> None:
        # The fix the refusal names: rerun with --force-overwrite.
        self.bin_dir.mkdir(parents=True)
        self.link.write_text("#!/bin/sh\necho hand-written\n")
        self._run_deploy(["c", "deploy-hooks", "heavy"])

        stdout, stderr, code = self._run_deploy(
            ["c", "deploy-hooks", "heavy", "--force-overwrite"]
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(os.readlink(self.link), str(self.scripts_dir / "heavy"))
        self.assertIn(f"relinked: {self.link}", stdout)
        self.assertEqual(sorted(p.name for p in self.bin_dir.iterdir()), ["heavy"])

    def test_force_overwrite_replaces_a_symlink_elsewhere(self) -> None:
        self.bin_dir.mkdir(parents=True)
        self.link.symlink_to(self.home / "missing-heavy")

        _, stderr, code = self._run_deploy(
            ["c", "deploy-hooks", "--all", "--force-overwrite"]
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(os.readlink(self.link), str(self.scripts_dir / "heavy"))

    def test_dry_run_writes_no_link(self) -> None:
        stdout, stderr, code = self._run_deploy(
            ["c", "deploy-hooks", "heavy", "--dry-run"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertFalse(self.link.exists() or self.link.is_symlink())
        self.assertIn(f"would link: {self.link}", stdout)


class HeavyBehaviorTests(_DeployCase):
    """Run the deployed heavy with a stub systemd-run first on PATH."""

    def setUp(self) -> None:
        super().setUp()
        _, stderr, code = self._run_deploy(["c", "deploy-hooks", "heavy"])
        self.assertEqual(code, 0, stderr)
        base = Path(self._tmp.name)
        stub_dir = base / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "systemd-run"
        stub.write_text(_STUB_SYSTEMD_RUN)
        stub.chmod(0o755)
        self.runtime = base / "runtime"
        self.runtime.mkdir()
        self.stub_log = base / "stub.log"
        self.env = {
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "STUB_LOG": str(self.stub_log),
            "HOME": str(self.home),
        }

    def _heavy(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.link), *args],
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=20,
        )

    def _stub_lines(self) -> list[str]:
        return self.stub_log.read_text().splitlines()

    def test_runs_the_command_in_a_capped_scope(self) -> None:
        proc = self._heavy("--", "echo", "hello")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "hello\n")
        lines = self._stub_lines()
        for expected in ("--user", "--scope", "MemoryMax=5G", "MemorySwapMax=0"):
            self.assertIn(expected, lines)

    def test_mem_sets_the_cap(self) -> None:
        self._heavy("--mem", "8G", "--", "true")
        self.assertIn("MemoryMax=8G", self._stub_lines())
        self._heavy("--mem=9G", "true")
        self.assertIn("MemoryMax=9G", self._stub_lines())

    def test_exit_code_passes_through(self) -> None:
        proc = self._heavy("--", "bash", "-c", "exit 7")
        self.assertEqual(proc.returncode, 7)

    def test_goflags_gets_a_parallelism_cap_unless_set(self) -> None:
        self._heavy("true", env={"GOFLAGS": ""})
        self.assertIn("GOFLAGS=-p=2", self._stub_lines())
        self._heavy("true", env={"GOFLAGS": "-mod=mod"})
        self.assertIn("GOFLAGS=-mod=mod -p=2", self._stub_lines())
        self._heavy("true", env={"GOFLAGS": "-p=4"})
        self.assertIn("GOFLAGS=-p=4", self._stub_lines())

    def test_takes_the_lock_and_clears_the_holder_note(self) -> None:
        proc = self._heavy("--", "bash", "-c", 'cat "$XDG_RUNTIME_DIR/heavy.holder"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bash -c", proc.stdout)
        self.assertTrue((self.runtime / "heavy.lock").exists())
        self.assertEqual((self.runtime / "heavy.holder").read_text(), "")

    def test_no_command_is_refused(self) -> None:
        proc = self._heavy("--mem", "8G")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no command given", proc.stderr)

    def test_help_prints_only_the_usage_comment(self) -> None:
        proc = self._heavy("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("heavy [--mem SIZE] [--] command [args...]", proc.stdout)
        self.assertNotIn("set -euo pipefail", proc.stdout)
        self.assertNotIn("#", proc.stdout)


if __name__ == "__main__":
    unittest.main()
