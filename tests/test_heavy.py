"""Tests for the heavy wrapper claudewheel ships and deploy-hooks deploys.

deploy-hooks writes the wrapper into the scripts directory like every other
script, then keeps ``~/.local/bin/heavy`` a symlink to that copy, so the
``heavy`` on PATH is always the one claudewheel ships. The behavior tests run
the deployed script under bash with a stub ``systemd-run`` first on PATH, so
they exercise the real lock, holder note, GOFLAGS, and exit-code handling
without starting a systemd scope.
"""

from __future__ import annotations

import fcntl
import io
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
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

# Records every systemctl call, one per line, and answers 'show' with the unit
# result the test chose (STUB_RESULT, default success) as a failed or inactive
# unit, the way systemd reports a finished scope.
_STUB_SYSTEMCTL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_SYSTEMCTL_LOG"
case " $* " in
    *" show "*)
        result="${STUB_RESULT:-success}"
        if [[ "$result" == success ]]; then state=inactive; else state=failed; fi
        printf 'ActiveState=%s\\nResult=%s\\n' "$state" "$result"
        ;;
esac
"""


def _start_time(pid: int) -> str:
    """The kernel start time of *pid* (field 22 of /proc/<pid>/stat)."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat.rsplit(")", 1)[1].split()[19]


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

    def test_redeploy_replaces_the_file_instead_of_rewriting_it(self) -> None:
        # bash reads a script as it runs it, so a heavy that is running (waiting
        # on its command) when the wrapper is redeployed must keep reading the
        # file it started from: the redeploy swaps in a new file rather than
        # rewriting the old one in place.
        self._run_deploy(["c", "deploy-hooks", "heavy"])
        dest = self.scripts_dir / "heavy"
        dest.write_text("#!/usr/bin/env bash\n# the copy a running heavy reads\n")
        with dest.open() as running:
            before = os.fstat(running.fileno()).st_ino
            _, stderr, code = self._run_deploy(
                ["c", "deploy-hooks", "heavy", "--force-overwrite"]
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                running.read(),
                "#!/usr/bin/env bash\n# the copy a running heavy reads\n",
            )
        self.assertNotEqual(dest.stat().st_ino, before)
        self.assertEqual(dest.read_text(), HOOK_SCRIPTS["heavy"])
        self.assertTrue(dest.stat().st_mode & stat.S_IXUSR)

    def test_dry_run_writes_no_link(self) -> None:
        stdout, stderr, code = self._run_deploy(
            ["c", "deploy-hooks", "heavy", "--dry-run"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertFalse(self.link.exists() or self.link.is_symlink())
        self.assertIn(f"would link: {self.link}", stdout)


class _HeavyRunCase(_DeployCase):
    """Run the deployed heavy with stub systemd-run and systemctl first on PATH."""

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
        systemctl = stub_dir / "systemctl"
        systemctl.write_text(_STUB_SYSTEMCTL)
        systemctl.chmod(0o755)
        self.runtime = base / "runtime"
        self.runtime.mkdir()
        self.stub_log = base / "stub.log"
        self.systemctl_log = base / "systemctl.log"
        self.lock = self.runtime / "heavy.lock"
        self.holder = self.runtime / "heavy.holder"
        self.env = {
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "STUB_LOG": str(self.stub_log),
            "STUB_SYSTEMCTL_LOG": str(self.systemctl_log),
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


class HeavyBehaviorTests(_HeavyRunCase):
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
        self.assertIn(
            "heavy [--mem SIZE] [--max-wait TIME] [--] command [args...]", proc.stdout
        )
        self.assertNotIn("set -euo pipefail", proc.stdout)
        self.assertNotIn("#", proc.stdout)

    def test_no_collect_and_a_unique_unit_is_cleaned_up(self) -> None:
        proc = self._heavy("--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = self._stub_lines()
        self.assertNotIn("--collect", lines)
        units = [line.split("=", 1)[1] for line in lines if line.startswith("--unit=")]
        self.assertEqual(len(units), 1, lines)
        unit = units[0]
        self.assertRegex(unit, r"^heavy-[0-9]+-[0-9]+\.scope$")
        calls = self.systemctl_log.read_text().splitlines()
        self.assertTrue(any("show" in c and unit in c for c in calls), calls)
        self.assertTrue(any("reset-failed" in c and unit in c for c in calls), calls)
        self.assertNotIn("killed at", proc.stderr)

    def test_a_memory_kill_is_explained(self) -> None:
        proc = self._heavy(
            "--", "bash", "-c", "exit 137", env={"STUB_RESULT": "oom-kill"}
        )
        self.assertEqual(proc.returncode, 137)
        self.assertIn(
            "heavy: killed at the 5G memory cap; rerun with "
            "'heavy --mem 10G -- bash -c exit\\ 137'",
            proc.stderr,
        )
        proc = self._heavy(
            "--mem",
            "700M",
            "--",
            "go",
            "test",
            "./...",
            env={"STUB_RESULT": "oom-kill"},
        )
        self.assertIn(
            "heavy: killed at the 700M memory cap; rerun with "
            "'heavy --mem 1400M -- go test ./...'",
            proc.stderr,
        )

    def test_other_failures_are_not_called_memory_kills(self) -> None:
        proc = self._heavy(
            "--", "bash", "-c", "exit 3", env={"STUB_RESULT": "exit-code"}
        )
        self.assertEqual(proc.returncode, 3)
        self.assertNotIn("killed at", proc.stderr)

    def test_prints_the_cap_on_every_run(self) -> None:
        proc = self._heavy("--", "true")
        self.assertIn("heavy: capped at 5G\n", proc.stderr)
        proc = self._heavy("--mem", "8G", "--", "true")
        self.assertIn("heavy: capped at 8G\n", proc.stderr)

    def test_prints_the_goflags_change_only_when_it_makes_it(self) -> None:
        proc = self._heavy("true", env={"GOFLAGS": ""})
        self.assertEqual(
            proc.stderr.count("heavy: added -p=2 to GOFLAGS"), 1, proc.stderr
        )
        proc = self._heavy("true", env={"GOFLAGS": "-p=4"})
        self.assertNotIn("GOFLAGS", proc.stderr)

    def test_scope_gets_a_lower_cpu_weight(self) -> None:
        self._heavy("--", "true")
        self.assertIn("CPUWeight=20", self._stub_lines())

    def test_mem_without_a_value_is_a_usage_error(self) -> None:
        for args in (["--mem"], ["--mem", "--", "true"], ["--mem="]):
            with self.subTest(args=args):
                proc = self._heavy(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("usage: heavy", proc.stderr)
                self.assertNotIn("unbound variable", proc.stderr)

    def test_mem_size_format_is_validated(self) -> None:
        for size in ("5GB", "abc", "0G", "5", "5g", "1.5G", "-1G"):
            with self.subTest(size=size):
                proc = self._heavy("--mem", size, "--", "true")
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("usage: heavy", proc.stderr)
        for size in ("512M", "5G", "1T", "900K"):
            with self.subTest(size=size):
                proc = self._heavy("--mem", size, "--", "true")
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_max_wait_format_is_validated(self) -> None:
        for args in (
            ["--max-wait"],
            ["--max-wait", "soon", "true"],
            ["--max-wait=5x", "true"],
        ):
            with self.subTest(args=args):
                proc = self._heavy(*args)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("usage: heavy", proc.stderr)


class HeavyLockTests(_HeavyRunCase):
    """The machine-wide lock: who holds it, how long a waiter waits, and that
    nothing but the wrapper itself ever holds it."""

    def _lock_is_free(self) -> bool:
        with self.lock.open("a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fh, fcntl.LOCK_UN)
            return True

    def _hold_lock(self) -> None:
        fh = self.lock.open("a")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX)

    def test_a_background_child_does_not_keep_the_lock(self) -> None:
        pidfile = Path(self._tmp.name) / "child.pid"
        proc = self._heavy(
            "--",
            "bash",
            "-c",
            f'sleep 60 >/dev/null 2>&1 </dev/null & echo $! > "{pidfile}"',
        )
        child = int(pidfile.read_text())
        self.addCleanup(self._kill, child)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(Path(f"/proc/{child}").exists(), "the child should still run")
        self.assertTrue(self._lock_is_free(), "the command's child holds the lock")

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_waiting_names_a_live_holder_and_reports_elapsed_time(self) -> None:
        holder = subprocess.Popen(
            [str(self.link), "--", "python3", "-c", "import time; time.sleep(30)"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        deadline = time.monotonic() + 10
        while not (self.holder.exists() and self.holder.read_text()):
            self.assertLess(
                time.monotonic(), deadline, "the holder never took the lock"
            )
            time.sleep(0.05)

        started = time.monotonic()
        proc = self._heavy(
            "--max-wait", "3s", "--", "true", env={"HEAVY_REPORT_EVERY": "1"}
        )
        waited = time.monotonic() - started

        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertGreaterEqual(waited, 2.5)
        self.assertLess(waited, 10)
        waits = [
            line
            for line in proc.stderr.splitlines()
            if line.startswith("heavy: waiting")
        ]
        self.assertGreaterEqual(len(waits), 3, proc.stderr)
        self.assertIn("0s so far", waits[0])
        self.assertIn("1s so far", waits[1])
        self.assertIn(f"pid {holder.pid}", waits[0])
        self.assertIn("time.sleep(30)", waits[0])
        self.assertIn("gives up after 3s", waits[0])
        last = proc.stderr.splitlines()[-1]
        self.assertIn("gave up after waiting 3s", last)
        self.assertIn(f"pid {holder.pid}", last)
        self.assertIn(f"kill {holder.pid}", last)
        self.assertIn("--max-wait", last)
        self.assertNotIn("capped at", proc.stderr)

    def test_a_dead_holder_is_not_named(self) -> None:
        # A heavy killed with SIGKILL never clears its holder note; a later
        # waiter must not name that dead process as the holder.
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.holder.write_text(
            f"{gone.pid} 12345\npid {gone.pid} since 09:00:00 in /x: stale-cmd\n"
        )
        self._hold_lock()
        proc = self._heavy("--max-wait", "1s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertNotIn("stale-cmd", proc.stderr)
        self.assertIn("held by: unknown", proc.stderr)
        self.assertIn(f"fuser -v {self.lock}", proc.stderr)

    def test_a_reused_pid_is_not_named(self) -> None:
        # The recorded pid is alive but is a different process (its start time
        # differs), as when the pid of a killed heavy has been reused.
        me = os.getpid()
        wrong = int(_start_time(me)) + 1
        self.holder.write_text(
            f"{me} {wrong}\npid {me} since 09:00:00 in /x: stale-cmd\n"
        )
        self._hold_lock()
        proc = self._heavy("--max-wait", "1s", "--", "true")
        self.assertNotIn("stale-cmd", proc.stderr)
        self.assertIn("held by: unknown", proc.stderr)

    def test_an_empty_or_garbled_note_is_unknown(self) -> None:
        self._hold_lock()
        for note in ("", "garbage\n"):
            with self.subTest(note=note):
                self.holder.write_text(note)
                proc = self._heavy("--max-wait", "0s", "--", "true")
                self.assertEqual(proc.returncode, 75, proc.stderr)
                self.assertIn("held by: unknown", proc.stderr)


class HeavyRealScopeTests(_DeployCase):
    """One small real job in a real systemd user scope over a tiny cap.

    Skipped where no systemd user manager answers (a CI container)."""

    def setUp(self) -> None:
        super().setUp()
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
        _, stderr, code = self._run_deploy(["c", "deploy-hooks", "heavy"])
        self.assertEqual(code, 0, stderr)
        self.runtime = Path(self._tmp.name) / "runtime"
        self.runtime.mkdir()
        # The lock lives in this throwaway runtime directory, but systemd-run
        # and systemctl find the user manager through it too, so its sockets
        # are linked in from the real one.
        real = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        for name in ("bus", "systemd"):
            (self.runtime / name).symlink_to(real / name)

    def test_a_job_over_its_cap_is_killed_and_explained(self) -> None:
        proc = subprocess.run(
            [
                str(self.link),
                "--mem",
                "40M",
                "--",
                "python3",
                "-c",
                "a = bytearray(400 * 1024 * 1024); a[::4096] = b'x' * len(a[::4096])",
            ],
            env={
                **os.environ,
                "XDG_RUNTIME_DIR": str(self.runtime),
                "HOME": str(self.home),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "heavy: killed at the 40M memory cap; rerun with 'heavy --mem 80M -- python3 -c",
            proc.stderr,
        )
        # The failed scope was read and then cleared, not left behind.
        failed = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--state=failed",
                "--no-legend",
                "heavy-*",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        self.assertEqual(failed.strip(), "")


if __name__ == "__main__":
    unittest.main()
