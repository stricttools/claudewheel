"""Tests for the heavy wrapper claudewheel ships and deploy-hooks deploys.

deploy-hooks writes the wrapper into the scripts directory like every other
script, then keeps ``~/.local/bin/heavy`` a symlink to that copy, so the
``heavy`` on PATH is always the one claudewheel ships. The behavior tests run
the deployed script under bash with a stub ``systemd-run`` first on PATH, so
they exercise the real admission (slots, notes, and the memory budget),
GOFLAGS, and exit-code handling without starting a systemd scope. They run it
in its own user and mount namespace, where a fixed /proc/meminfo and a fixed
cgroup tree are mounted over the real ones, so the budget heavy computes is
the same on every machine and heavy itself has no switch that fakes it.
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

# Records every systemctl call, one per line. Answers 'list-units' with the
# lines of the file STUB_SCOPES names (none by default); 'show -P ControlGroup
# UNIT' with /UNIT, a directory of the fixed cgroup tree that exists only when
# the test made it, so a running job counts its whole cap against the budget
# unless the test gave its scope a memory use (or with /STUB_CGROUP for every
# unit, when set); and any other 'show' with the unit result
# the test chose (STUB_RESULT, default success) as a failed or inactive unit,
# the way systemd reports a finished scope.
_STUB_SYSTEMCTL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_SYSTEMCTL_LOG"
case " $* " in
    *" list-units "*) [[ -z "${STUB_SCOPES:-}" ]] || cat "$STUB_SCOPES" ;;
    *" -P ControlGroup "*) printf '/%s\\n' "${STUB_CGROUP:-${@: -1}}" ;;
    *" -P ActiveState "*) [[ -n "${STUB_STILL_ACTIVE:-}" ]] && echo active || echo inactive ;;
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


def _kib(size: str) -> int:
    """A size with a K, M, G, or T suffix in KiB, 1024-based as heavy reads it."""
    units = {"K": 1, "M": 1024, "G": 1024**2, "T": 1024**3}
    return int(size[:-1]) * units[size[-1]]


# A command that runs until the file named by its argument exists, or a minute
# has passed.
_WAIT_FOR_RELEASE = (
    "import os, sys, time\n"
    "deadline = time.monotonic() + 60\n"
    "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
    "    time.sleep(0.05)"
)


def _in_namespace(meminfo: Path, cgroup: Path | None, *argv: str) -> list[str]:
    """*argv* in its own user and mount namespace, with *meminfo* mounted over
    /proc/meminfo and, when given, *cgroup* over /sys/fs/cgroup.

    unshare and bash exec in place, so the process started is *argv*'s own.
    """
    mounts = 'mount --bind "$1" /proc/meminfo'
    if cgroup is not None:
        mounts += ' && mount --bind "$2" /sys/fs/cgroup'
    return [
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "bash",
        "-c",
        f'{mounts} && shift 2 && exec "$@"',
        "heavy-under-test",
        str(meminfo),
        str(cgroup or ""),
        *argv,
    ]


def _namespaces_work() -> bool:
    return (
        shutil.which("unshare") is not None
        and subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "true"],
            capture_output=True,
            timeout=20,
        ).returncode
        == 0
    )


# heavy's slot count, as its help states it.
_SLOTS = 8


def _state(pid: int) -> str:
    """The state letter of *pid* (R, S, Z, ...), empty once it is gone."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError, ProcessLookupError:
        return ""


def _children(pid: int) -> list[int]:
    """The direct children of *pid*."""
    try:
        tasks = Path(f"/proc/{pid}/task").iterdir()
        return [
            int(child)
            for task in tasks
            for child in (task / "children").read_text().split()
        ]
    except FileNotFoundError, ProcessLookupError:
        return []


def _is_free(path: Path) -> bool:
    """Whether no process holds a flock on *path*."""
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
        if not _namespaces_work():
            self.skipTest("no unprivileged user and mount namespaces")
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
        self.slots = self.runtime / "heavy.slots"
        self.meminfo = base / "meminfo"
        self.cgroup = base / "cgroup"
        self.cgroup.mkdir()
        self._set_memory(total="64G", available="16G")
        self.env = {
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "XDG_RUNTIME_DIR": str(self.runtime),
            "STUB_LOG": str(self.stub_log),
            "STUB_SYSTEMCTL_LOG": str(self.systemctl_log),
            "HOME": str(self.home),
        }

    def _set_memory(self, total: str, available: str) -> None:
        """The MemTotal and MemAvailable heavy reads, such as "16G"."""
        self.meminfo.write_text(
            f"MemTotal:       {_kib(total)} kB\n"
            f"MemFree:        {_kib(available)} kB\n"
            f"MemAvailable:   {_kib(available)} kB\n"
        )

    def _set_scope_use(
        self, unit: str, current: str, cache: str, cap: str | None = None
    ) -> None:
        """What the scope *unit* holds, *cache* of it page cache, under *cap*.

        No *cap* is a scope with no MemoryMax (memory.max reads "max").
        """
        scope = self.cgroup / unit
        scope.mkdir(exist_ok=True)
        (scope / "memory.current").write_text(f"{_kib(current) * 1024}\n")
        (scope / "memory.stat").write_text(f"anon 0\nfile {_kib(cache) * 1024}\n")
        max_ = "max" if cap is None else str(_kib(cap) * 1024)
        (scope / "memory.max").write_text(f"{max_}\n")

    def _list_scopes(self, *lines: str) -> None:
        """The heavy scopes systemctl list-units reports as running."""
        listing = Path(self._tmp.name) / "scopes"
        listing.write_text("".join(f"{line}\n" for line in lines))
        self.env["STUB_SCOPES"] = str(listing)

    def _argv(self, *args: str) -> list[str]:
        """heavy with *args*, under the fixed /proc/meminfo and cgroup tree."""
        return _in_namespace(self.meminfo, self.cgroup, str(self.link), *args)

    def _heavy(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._argv(*args),
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

    def test_systemd_run_leaves_dollar_signs_alone(self) -> None:
        # systemd-run expands $VAR and $$ in the command line unless told not
        # to, which would rewrite a command such as bash -c 'echo $x'.
        self._heavy("--", "true")
        self.assertIn("--expand-environment=no", self._stub_lines())

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

    def test_claims_a_slot_whose_note_names_it_and_frees_it_after(self) -> None:
        proc = self._heavy(
            "--mem", "700M", "--", "bash", "-c", 'cat "$XDG_RUNTIME_DIR"/heavy.slots/*'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        note = proc.stdout.splitlines()
        self.assertEqual(len(note), 3, proc.stdout)
        pid, start = note[0].split()
        self.assertTrue(pid.isdigit())
        self.assertTrue(start.isdigit())
        cap, mem, unit = note[1].split()
        self.assertEqual((cap, mem), (str(700 * 1024), "700M"))
        self.assertRegex(unit, r"^heavy-[0-9]+-[0-9]+\.scope$")
        self.assertIn(f"pid {pid} since ", note[2])
        self.assertIn("bash -c", note[2])
        self.assertEqual(len(list(self.slots.iterdir())), _SLOTS)
        for slot in self.slots.iterdir():
            self.assertTrue(_is_free(slot), f"{slot} is still held")

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
        # The failed scope is read and then cleared, so none accumulate.
        calls = self.systemctl_log.read_text().splitlines()
        self.assertTrue(
            any(c.startswith("--user reset-failed heavy-") for c in calls), calls
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
        for size in ("512M", "5G", "14G", "900K"):
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


class _AdmissionCase(_HeavyRunCase):
    """Helpers for the admission tests: stand-in jobs and heavies in flight."""

    def _hold_slot(
        self,
        index: int,
        mem: str = "1M",
        pid: int | None = None,
        start: str | None = None,
        desc: str = "stand-in-job",
        note: str | None = None,
    ) -> Path:
        """Hold slot *index* from this test process, as a running job would.

        The note names *pid* (this process by default) with *start* (its real
        start time by default), a cap of *mem*, and *desc*; *note* replaces the
        whole note instead.
        """
        self.slots.mkdir(exist_ok=True)
        path = self.slots / str(index)
        fh = path.open("a")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX)
        if note is None:
            pid = os.getpid() if pid is None else pid
            start = _start_time(pid) if start is None else start
            cap = _kib(mem)
            note = (
                f"{pid} {start}\n{cap} {mem} heavy-stand-in.scope\n"
                f"pid {pid} since 09:00:00 in /x, scope heavy-stand-in.scope: {desc}\n"
            )
        path.write_text(note)
        return path

    def _start_heavy(self, *args: str, **env: str) -> subprocess.Popen[str]:
        """Start heavy in the background; stopped with SIGKILL at cleanup."""
        proc = subprocess.Popen(
            self._argv(*args),
            env={**self.env, **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(_kill, proc.pid)
        return proc

    def _wait_for_line(self, proc: subprocess.Popen[str], prefix: str) -> str:
        """Read *proc*'s stderr until a line starting with *prefix*."""
        assert proc.stderr is not None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line: str = proc.stderr.readline()
            if line.startswith(prefix):
                return line
            if not line and proc.poll() is not None:
                break
        self.fail(f"heavy never printed a line starting with {prefix!r}")

    def _start_running_job(self, mem: str) -> tuple[subprocess.Popen[str], Path]:
        """A heavy whose command runs until the returned file exists.

        The file is made at cleanup, and the command gives up after a minute
        in any case, so it never outlives the test even when its heavy was
        killed.
        """
        release = Path(self._tmp.name) / f"release-{time.monotonic_ns()}"
        proc = self._start_heavy(
            "--mem",
            mem,
            "--",
            "python3",
            "-c",
            _WAIT_FOR_RELEASE,
            str(release),
        )
        self.addCleanup(release.touch)
        self._wait_for_line(proc, "heavy: capped at")
        return proc, release

    def _fill_slots(self, count: int, mem: str = "1M") -> None:
        for index in range(count):
            self._hold_slot(index, mem=mem, desc=f"stand-in-{index}")


class HeavyAdmissionTests(_AdmissionCase):
    """Admission against the memory budget and the slots: who may start,
    who waits, what a waiter says, and that the command never holds any of it."""

    def test_small_jobs_start_beside_running_ones(self) -> None:
        # Many heavy jobs may run at once: only the budget and the slots bound
        # them, not one machine-wide lock.
        self._fill_slots(_SLOTS - 1)
        proc = self._heavy("--mem", "1M", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("waiting", proc.stderr)

    def test_a_job_that_does_not_fit_waits_and_names_the_running_jobs(self) -> None:
        # 16G available less the 2G margin is 14G, and the running job's 10G
        # cap leaves 4G of it.
        holder, _ = self._start_running_job("10G")

        started = time.monotonic()
        proc = self._heavy(
            "--mem",
            "5G",
            "--max-wait",
            "3s",
            "--",
            "true",
            env={"HEAVY_REPORT_EVERY": "1"},
        )
        waited = time.monotonic() - started

        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertGreaterEqual(waited, 2.5)
        self.assertLess(waited, 12)
        waits = [
            line
            for line in proc.stderr.splitlines()
            if line.startswith("heavy: waiting")
        ]
        self.assertGreaterEqual(len(waits), 3, proc.stderr)
        self.assertIn("0s so far", waits[0])
        self.assertIn("1s so far", waits[1])
        self.assertIn("to start a 5G job", waits[0])
        self.assertIn("gives up after 3s", waits[0])
        self.assertIn(
            "16.0G available less the 2G margin leaves 14.0G, and running heavy "
            "jobs still reserve 10.0G of it",
            waits[0],
        )
        self.assertIn("(cap 10G, using 0.0G)", waits[0])
        self.assertIn(f"pid {holder.pid}", waits[0])
        self.assertIn("os.path.exists", waits[0])
        last = proc.stderr.splitlines()[-1]
        self.assertIn("gave up after waiting 3s", last)
        self.assertIn(f"pid {holder.pid}", last)
        self.assertIn(f"kill {holder.pid}", last)
        self.assertIn("--max-wait", last)
        self.assertIn("smaller --mem", last)
        self.assertNotIn("capped at", proc.stderr)

    def test_a_waiting_job_starts_once_the_running_one_ends(self) -> None:
        holder, release = self._start_running_job("10G")
        waiter = self._start_heavy("--mem", "5G", "--max-wait", "60s", "--", "true")
        self._wait_for_line(waiter, "heavy: waiting")

        release.touch()
        self.assertEqual(holder.wait(timeout=15), 0)
        self.assertEqual(waiter.wait(timeout=15), 0)
        assert waiter.stderr is not None
        self.assertIn("heavy: capped at 5G", waiter.stderr.read())

    def test_waits_while_every_slot_is_taken(self) -> None:
        self._fill_slots(_SLOTS)
        proc = self._heavy("--mem", "1M", "--max-wait", "1s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn(f"all {_SLOTS} heavy slots are in use", proc.stderr)
        self.assertIn("stand-in-7", proc.stderr)
        self.assertIn(f"kill {os.getpid()}", proc.stderr.splitlines()[-1])

    def test_a_cap_this_machine_can_never_admit_is_refused(self) -> None:
        proc = self._heavy("--mem", "100T", "--", "true")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--mem 100T is more than this machine can ever give", proc.stderr)
        self.assertIn("usage: heavy", proc.stderr)

    def test_the_largest_cap_this_machine_can_admit_is_not_refused(self) -> None:
        # 64G of memory less the 2G margin: 62G may wait, 62G and 1M never fits.
        proc = self._heavy("--mem", "63489M", "--", "true")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("64.0G of memory less the 2G margin is 62.0G", proc.stderr)
        proc = self._heavy("--mem", "62G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertNotIn("usage: heavy", proc.stderr)

    def test_a_job_fits_up_to_the_budget_and_not_beyond(self) -> None:
        # 16G available less the 2G margin is 14G; the running job reserves 10G.
        self._hold_slot(0, mem="10G")
        proc = self._heavy("--mem", "4G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._heavy("--mem", "4097M", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)

    def test_a_running_job_reserves_only_what_it_has_not_used(self) -> None:
        # The running job's scope holds 3G, 1G of it page cache the kernel
        # would reclaim, so it has used 2G of its 10G cap and reserves 8G:
        # 6G fits beside it in the 14G budget, 6G and 1M does not.
        self._hold_slot(0, mem="10G")
        self._set_scope_use("heavy-stand-in.scope", current="3G", cache="1G")
        proc = self._heavy("--mem", "6G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._heavy("--mem", "6145M", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("running heavy jobs still reserve 8.0G", proc.stderr)
        self.assertIn("(cap 10G, using 2.0G)", proc.stderr)

    def test_the_budget_follows_available_memory(self) -> None:
        self._set_memory(total="64G", available="6G")
        proc = self._heavy("--mem", "5G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("no heavy job is running", proc.stderr)
        self._set_memory(total="64G", available="7G")
        proc = self._heavy("--mem", "5G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_cap_that_cannot_fit_with_no_heavy_job_running_fails_at_once(
        self,
    ) -> None:
        # 5.2G available less the 2G margin is 3.2G: no heavy job can end to
        # make room for 6G, so heavy does not wait out its --max-wait, and it
        # names the largest cap that fits now.
        self._set_memory(total="16G", available="5324M")
        started = time.monotonic()
        proc = self._heavy("--mem", "6G", "--max-wait", "60s", "--", "true")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertNotIn("heavy: waiting", proc.stderr)
        self.assertIn("heavy: cannot start a 6G job", proc.stderr)
        self.assertIn("leaves 3.2G", proc.stderr)
        self.assertIn("no heavy job is running", proc.stderr)
        self.assertIn("rerun with --mem 3G or less", proc.stderr)
        self.assertNotIn("capped at", proc.stderr)

    def test_the_largest_cap_that_fits_is_named_in_megabytes_below_a_gigabyte(
        self,
    ) -> None:
        self._set_memory(total="16G", available="2560M")
        proc = self._heavy("--mem", "1G", "--max-wait", "60s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("rerun with --mem 512M or less", proc.stderr)

    def test_a_cap_that_does_not_fit_beside_a_running_job_still_waits(
        self,
    ) -> None:
        self._hold_slot(0, mem="10G")
        proc = self._heavy("--mem", "5G", "--max-wait", "1s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn("heavy: waiting", proc.stderr)
        self.assertNotIn("cannot start", proc.stderr)

    def test_a_dead_holder_is_unknown_and_blocks(self) -> None:
        # A note naming a process that is gone, on a slot some process still
        # holds: heavy cannot tell what that job reserves, so it counts it as
        # everything and names the slot file to find the holder by.
        gone = subprocess.Popen(["true"])
        gone.wait()
        slot = self._hold_slot(0, pid=gone.pid, start="12345", desc="stale-cmd")
        proc = self._heavy("--mem", "1M", "--max-wait", "1s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertNotIn("stale-cmd", proc.stderr)
        self.assertIn("unknown", proc.stderr)
        self.assertIn(f"fuser -v {slot}", proc.stderr)

    def test_a_reused_pid_is_unknown(self) -> None:
        me = os.getpid()
        self._hold_slot(
            0, pid=me, start=str(int(_start_time(me)) + 1), desc="stale-cmd"
        )
        proc = self._heavy("--mem", "1M", "--max-wait", "1s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertNotIn("stale-cmd", proc.stderr)
        self.assertIn("unknown", proc.stderr)

    def test_an_empty_or_garbled_note_is_unknown(self) -> None:
        for index, note in enumerate(("", "garbage\n")):
            with self.subTest(note=note):
                slot = self._hold_slot(index, note=note)
                proc = self._heavy("--mem", "1M", "--max-wait", "0s", "--", "true")
                self.assertEqual(proc.returncode, 75, proc.stderr)
                self.assertIn(f"fuser -v {slot}", proc.stderr)

    def test_a_scope_whose_heavy_is_gone_still_reserves_its_cap(self) -> None:
        # A heavy killed while its command ran leaves the command running in
        # its scope with no slot: the scope itself is counted. It holds 3G, 1G
        # of it page cache, under a 10G cap, so it reserves 8G of the 14G
        # budget.
        orphan = "heavy-111-222.scope"
        self._list_scopes(f"{orphan} loaded active running [systemd-run] go test ./...")
        self._set_scope_use(orphan, current="3G", cache="1G", cap="10G")
        proc = self._heavy("--mem", "6G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self._heavy("--mem", "6145M", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn(
            f"scope {orphan}, running without a heavy slot, as when its heavy was "
            f"killed (cap 10.0G, using 2.0G; stop it "
            f"with 'systemctl --user stop {orphan}')",
            proc.stderr,
        )
        self.assertIn("running heavy jobs still reserve 8.0G", proc.stderr)

    def test_a_scope_that_is_no_longer_active_reserves_nothing(self) -> None:
        orphan = "heavy-111-222.scope"
        self._list_scopes(f"{orphan} loaded inactive dead [systemd-run] go test ./...")
        self._set_scope_use(orphan, current="3G", cache="1G", cap="10G")
        proc = self._heavy("--mem", "14G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_scope_with_no_cap_blocks(self) -> None:
        orphan = "heavy-111-222.scope"
        self._list_scopes(f"{orphan} loaded active running [systemd-run] go test ./...")
        self._set_scope_use(orphan, current="1G", cache="0K")
        proc = self._heavy("--mem", "1M", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn(f"scope {orphan} with no memory cap", proc.stderr)

    def test_a_running_job_is_counted_once_by_its_slot_and_its_scope(self) -> None:
        # The stand-in job's slot names its scope, which is running too: its
        # 10G cap is counted once, leaving 4G of the 14G budget.
        self._hold_slot(0, mem="10G")
        self._list_scopes(
            "heavy-stand-in.scope loaded active running [systemd-run] stand-in"
        )
        self._set_scope_use("heavy-stand-in.scope", current="0K", cache="0K", cap="10G")
        proc = self._heavy("--mem", "4G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_what_the_command_leaves_running_is_stopped(self) -> None:
        # Its scope's cgroup still holds a process once the command returned.
        (self.cgroup / "own").mkdir()
        (self.cgroup / "own" / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        proc = self._heavy("--", "true", env={"STUB_CGROUP": "own"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        unit = next(
            line.split("=", 1)[1]
            for line in self._stub_lines()
            if line.startswith("--unit=")
        )
        calls = self.systemctl_log.read_text().splitlines()
        self.assertIn(f"--user stop {unit}", calls)
        self.assertIn(
            f"heavy: stopped what the command left running in {unit}", proc.stderr
        )

    def test_a_scope_that_ended_with_its_command_is_not_stopped(self) -> None:
        # Its scope's cgroup is empty, though the unit still reads active, as
        # it does for a moment after the last process exits.
        (self.cgroup / "own").mkdir()
        (self.cgroup / "own" / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        proc = self._heavy(
            "--", "true", env={"STUB_CGROUP": "own", "STUB_STILL_ACTIVE": "1"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.systemctl_log.read_text().splitlines()
        self.assertFalse(any(" stop " in f" {c} " for c in calls), calls)
        self.assertNotIn("stopped", proc.stderr)

    def test_a_background_child_holds_neither_the_lock_nor_a_slot(self) -> None:
        pidfile = Path(self._tmp.name) / "child.pid"
        proc = self._heavy(
            "--",
            "bash",
            "-c",
            f'sleep 60 >/dev/null 2>&1 </dev/null & echo $! > "{pidfile}"',
        )
        child = int(pidfile.read_text())
        self.addCleanup(_kill, child)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(Path(f"/proc/{child}").exists(), "the child should still run")
        self.assertTrue(_is_free(self.lock), "the command's child holds the lock")
        for slot in self.slots.iterdir():
            self.assertTrue(_is_free(slot), f"the command's child holds {slot}")

    def test_the_command_is_started_with_no_lock_or_slot_descriptor(self) -> None:
        proc = self._heavy("--", "bash", "-c", "ls -l /proc/$$/fd")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("heavy.lock", proc.stdout)
        self.assertNotIn("heavy.slots", proc.stdout)

    def test_a_waiting_heavy_leaves_the_admission_lock_closed_between_attempts(
        self,
    ) -> None:
        # The waiting message points at 'fuser -v' on the admission lock, which
        # lists every process with it open: a waiting heavy, and the sleep it
        # waits in, must not be among them.
        self._fill_slots(_SLOTS)
        waiter = self._start_heavy("--mem", "1M", "--max-wait", "60s", "--", "true")
        self._wait_for_line(waiter, "heavy: waiting")
        lock = str(self.lock)
        holders: set[str] = set()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            for pid in [waiter.pid, *_children(waiter.pid)]:
                try:
                    fds = list(Path(f"/proc/{pid}/fd").iterdir())
                    comm = Path(f"/proc/{pid}/comm").read_text().strip()
                except FileNotFoundError, ProcessLookupError:
                    continue
                for fd in fds:
                    try:
                        if os.readlink(fd) == lock:
                            holders.add(comm)
                    except FileNotFoundError, ProcessLookupError:
                        pass
            time.sleep(0.02)
        self.assertNotIn("sleep", holders)


class HeavyCrashTests(_AdmissionCase):
    """A heavy killed at any point, SIGKILL included, never keeps budget."""

    def test_a_killed_running_heavy_frees_its_slot_at_once(self) -> None:
        self._fill_slots(_SLOTS - 1)
        pidfile = Path(self._tmp.name) / "command.pid"
        victim = self._start_heavy(
            "--mem",
            "1M",
            "--",
            "bash",
            "-c",
            f'echo $$ > "{pidfile}"; exec python3 -c "import time; time.sleep(60)"',
        )
        self._wait_for_line(victim, "heavy: capped at")
        blocked = self._heavy("--mem", "1M", "--max-wait", "0s", "--", "true")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)

        os.kill(victim.pid, signal.SIGKILL)
        victim.wait(timeout=10)
        # The command itself is still running; only the wrapper died.
        self.addCleanup(_kill, int(pidfile.read_text()))

        proc = self._heavy("--mem", "1M", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_killed_running_heavy_frees_its_budget_at_once(self) -> None:
        victim, _ = self._start_running_job("10G")
        blocked = self._heavy("--mem", "5G", "--max-wait", "0s", "--", "true")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)

        os.kill(victim.pid, signal.SIGKILL)
        victim.wait(timeout=10)

        proc = self._heavy("--mem", "5G", "--max-wait", "0s", "--", "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_process_killed_while_admitting_frees_the_admission_lock(self) -> None:
        # The admission lock is held only for the moment of admission; a heavy
        # killed in that moment releases it with its death.
        admitting = subprocess.Popen(
            [
                "python3",
                "-c",
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('locked', flush=True); time.sleep(60)",
                str(self.lock),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(admitting.wait)
        self.addCleanup(_kill, admitting.pid)
        assert admitting.stdout is not None
        self.assertEqual(admitting.stdout.readline(), "locked\n")

        waiter = self._start_heavy("--mem", "1M", "--max-wait", "60s", "--", "true")
        line = self._wait_for_line(waiter, "heavy: waiting")
        self.assertIn(f"fuser -v {self.lock}", line)

        os.kill(admitting.pid, signal.SIGKILL)
        self.assertEqual(waiter.wait(timeout=15), 0)

    def test_a_heavy_killed_while_waiting_holds_nothing(self) -> None:
        self._fill_slots(_SLOTS)
        waiter = self._start_heavy("--mem", "1M", "--max-wait", "60s", "--", "true")
        self._wait_for_line(waiter, "heavy: waiting")
        os.kill(waiter.pid, signal.SIGKILL)
        waiter.wait(timeout=10)
        self.assertTrue(_is_free(self.lock))


class HeavyRealScopeTests(_DeployCase):
    """Real jobs in real systemd user scopes.

    heavy runs in its own user and mount namespace with a /proc/meminfo that
    reports 2000G of memory, 1000G of it available, so the admission figures
    do not depend on this machine's memory: the heavy jobs other sessions run
    meanwhile reserve at most a few gigabytes of the 998G budget, against caps
    of hundreds of gigabytes here. Every cap is only declared; each job uses a
    few megabytes, and none goes over its cap: a real memory kill is logged by
    the kernel and systemd and shows the desktop user a notification that a
    process was killed, so the memory-kill path is tested with the stub
    systemd-run only.

    Skipped where no systemd user manager answers (a CI container) or no
    unprivileged namespaces are allowed.
    """

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
        if not _namespaces_work():
            self.skipTest("no unprivileged user and mount namespaces")
        _, stderr, code = self._run_deploy(["c", "deploy-hooks", "heavy"])
        self.assertEqual(code, 0, stderr)
        base = Path(self._tmp.name)
        self.runtime = base / "runtime"
        self.runtime.mkdir()
        # The slots live in this throwaway runtime directory, but systemd-run
        # and systemctl find the user manager through it too, so its sockets
        # are linked in from the real one.
        real = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        for name in ("bus", "systemd"):
            (self.runtime / name).symlink_to(real / name)
        self.meminfo = base / "meminfo"
        self.meminfo.write_text(
            f"MemTotal:       {_kib('2000G')} kB\n"
            f"MemFree:        {_kib('1000G')} kB\n"
            f"MemAvailable:   {_kib('1000G')} kB\n"
        )
        self.env = {
            **os.environ,
            "XDG_RUNTIME_DIR": str(self.runtime),
            "HOME": str(self.home),
        }

    def _argv(self, *args: str) -> list[str]:
        return _in_namespace(self.meminfo, None, str(self.link), *args)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._argv(*args), env=self.env, capture_output=True, text=True, timeout=60
        )

    def _start(self, *args: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            self._argv(*args),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(_kill, proc.pid)
        return proc

    def _stop_scope_at_cleanup(self, unit: str) -> None:
        self.addCleanup(
            subprocess.run,
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            timeout=30,
        )

    def _scope_of(self, pid: int) -> str:
        """The unit of the scope *pid* runs in."""
        return Path(f"/proc/{pid}/cgroup").read_text().strip().rsplit("/", 1)[-1]

    def test_two_small_jobs_run_at_the_same_time(self) -> None:
        # Each job marks that it runs, then waits for the other's mark: under
        # one machine-wide lock the first would wait for the second forever.
        base = Path(self._tmp.name)
        waits_for = (
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').close()\n"
            "deadline = time.monotonic() + 20\n"
            "while not os.path.exists(sys.argv[2]):\n"
            "    if time.monotonic() > deadline: sys.exit(1)\n"
            "    time.sleep(0.05)"
        )
        first = self._start(
            "--mem",
            "64M",
            "--",
            "python3",
            "-c",
            waits_for,
            str(base / "a"),
            str(base / "b"),
        )
        second = self._start(
            "--mem",
            "64M",
            "--",
            "python3",
            "-c",
            waits_for,
            str(base / "b"),
            str(base / "a"),
        )
        self.assertEqual(first.wait(timeout=40), 0)
        self.assertEqual(second.wait(timeout=40), 0)

    def test_a_job_that_does_not_fit_waits_for_a_running_one(self) -> None:
        # The running job's 400G cap leaves under 600G of the 998G budget, so a
        # 700G job waits; once the running job ends, the 700G job starts.
        release = Path(self._tmp.name) / "release"
        self.addCleanup(release.touch)
        holder = self._start(
            "--mem", "400G", "--", "python3", "-c", _WAIT_FOR_RELEASE, str(release)
        )
        assert holder.stderr is not None
        self.assertIn("heavy: capped at 400G", holder.stderr.readline())

        blocked = self._run("--mem", "700G", "--max-wait", "1s", "--", "true")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertIn(f"pid {holder.pid}", blocked.stderr)
        self.assertRegex(blocked.stderr, r"\(cap 400G, using [0-9.]+G\)")

        release.touch()
        self.assertEqual(holder.wait(timeout=30), 0)
        admitted = self._run("--mem", "700G", "--max-wait", "10s", "--", "true")
        self.assertEqual(admitted.returncode, 0, admitted.stderr)

    def test_a_killed_heavy_leaves_its_scope_counted_until_it_is_stopped(self) -> None:
        # Killing heavy does not stop its command, which runs on in its capped
        # scope: the scope keeps its cap reserved, and the waiting message
        # names the command that stops it. Running that command frees it.
        release = Path(self._tmp.name) / "release"
        self.addCleanup(release.touch)
        pidfile = Path(self._tmp.name) / "command.pid"
        holder = self._start(
            "--mem",
            "400G",
            "--",
            "bash",
            "-c",
            f'echo $$ > "{pidfile}"; exec python3 -c "$0" "$1"',
            _WAIT_FOR_RELEASE,
            str(release),
        )
        assert holder.stderr is not None
        self.assertIn("heavy: capped at 400G", holder.stderr.readline())
        deadline = time.monotonic() + 10
        while not pidfile.exists() or not pidfile.read_text().strip():
            self.assertLess(time.monotonic(), deadline, "the command never started")
            time.sleep(0.05)
        command = int(pidfile.read_text())
        unit = self._scope_of(command)
        self.assertRegex(unit, r"^heavy-[0-9]+-[0-9]+\.scope$")
        self._stop_scope_at_cleanup(unit)

        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        self.assertTrue(Path(f"/proc/{command}").exists(), "the command should run on")

        blocked = self._run("--mem", "700G", "--max-wait", "1s", "--", "true")
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        stop = f"systemctl --user stop {unit}"
        self.assertIn(
            f"scope {unit}, running without a heavy slot, as when its heavy was "
            "killed (cap 400.0G",
            blocked.stderr,
        )
        self.assertIn(f"stop it with '{stop}'", blocked.stderr)

        subprocess.run(stop.split(), check=True, capture_output=True, timeout=30)
        admitted = self._run("--mem", "700G", "--max-wait", "10s", "--", "true")
        self.assertEqual(admitted.returncode, 0, admitted.stderr)

    def test_arguments_reach_the_command_unchanged(self) -> None:
        text = "$HOME $$ ${X:-y} $(true) `true` \\ '\""
        proc = self._run("--mem", "64M", "--", "printf", "%s", text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, text)

    def test_nothing_is_reported_stopped_when_nothing_was_left(self) -> None:
        for _ in range(5):
            proc = self._run("--mem", "64M", "--", "true")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("stopped", proc.stderr)

    def test_what_the_command_leaves_running_is_stopped_with_its_scope(self) -> None:
        pidfile = Path(self._tmp.name) / "child.pid"
        proc = self._run(
            "--mem",
            "64M",
            "--",
            "bash",
            "-c",
            'python3 -c "import time; time.sleep(60)" >/dev/null 2>&1 </dev/null & '
            f'echo $! > "{pidfile}"',
        )
        child = int(pidfile.read_text())
        self.addCleanup(_kill, child)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "heavy: stopped what the command left running in heavy-", proc.stderr
        )
        deadline = time.monotonic() + 10
        while Path(f"/proc/{child}").exists() and "Z" not in _state(child):
            self.assertLess(time.monotonic(), deadline, "the child still runs")
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
