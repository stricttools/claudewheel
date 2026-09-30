"""Tests for claudewheel-tool-scope, the CLAUDE_CODE_SHELL_PREFIX of every launched session.

Claude Code runs every shell command it spawns as ``<prefix> '<command line>'``
(Bash tool calls, hooks, the status line, stdio MCP servers). The wrapper puts
each Bash tool call in its own systemd scope inside the session's tools slice,
which caps all of them together, and runs everything else as it is. When the
kernel's OOM killer kills a process of the command, the wrapper says so on
stderr, naming the cap, instead of leaving a bare exit status.

The tests deploy the wrapper like every other script and run it under bash
with stub ``systemd-run``, ``systemctl`` and ``busctl`` first on PATH, so no
systemd unit is started. They run it in its own user and mount namespace, with
a fixed cgroup tree mounted over /sys/fs/cgroup and a fixed file over the
process's own /proc/<pid>/cgroup, so the OOM counters it reads are the test's.
No test causes an OOM kill.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claudewheel import probe
from claudewheel.hook_scripts import (
    HOOK_SCRIPTS,
    PATH_COMMANDS,
    TOOL_SCOPE_SCRIPT,
    deploy_scripts,
)

# Records systemd-run's arguments, one per line, then runs the command after
# "--" in place, as systemd-run --scope does.
_STUB_SYSTEMD_RUN = """\
#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$STUB_LOG"
while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
shift
exec "$@"
"""

# Answers 'show' with the slice state the file STUB_SLICE holds, or with the
# state of a unit that does not exist when there is no such file.
_STUB_SYSTEMCTL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_SYSTEMCTL_LOG"
case " $* " in
    *" show "*)
        if [[ -f "$STUB_SLICE" ]]; then
            cat "$STUB_SLICE"
        else
            printf 'ActiveState=inactive\\nMemoryMax=infinity\\nMemorySwapMax=infinity\\n'
        fi
        ;;
esac
"""

# Records its arguments. Creating the slice writes the state 'systemctl show'
# answers from then on: the caps it was given, or none with STUB_BUSCTL_NOCAP.
# STUB_BUSCTL_FAIL makes the call fail the way a second creation does.
_STUB_BUSCTL = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_BUSCTL_LOG"
if [[ -n "${STUB_BUSCTL_FAIL:-}" ]]; then
    echo "Call failed: Unit $6 was already loaded or has a fragment file." >&2
    exit 1
fi
max=infinity swap=infinity
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    case "${args[i]}" in
        MemoryMax) max=${args[i + 2]} ;;
        MemorySwapMax) swap=${args[i + 2]} ;;
    esac
done
[[ -z "${STUB_BUSCTL_NOCAP:-}" ]] || max=infinity
printf 'ActiveState=active\\nMemoryMax=%s\\nMemorySwapMax=%s\\n' "$max" "$swap" > "$STUB_SLICE"
echo 'o "/org/freedesktop/systemd1/job/1"'
"""

SESSION_SCOPE = "claudewheel-session-4242-1790000000.scope"
SESSION_SLICE = "claudewheel-4242_1790000000.slice"
TOOLS_SLICE = "claudewheel-4242_1790000000-tools.slice"
SIX_G = str(6 * 1024**3)
ONE_G = str(1024**3)

# How Claude Code 2.1.281 assembles a Bash tool call before handing it to the
# prefix (recorded in experiments/shell-prefix): the command Claude ran is the
# eval, and the chain ends by writing the working directory to a file.
_SNAPSHOT_AND_SETUP = (
    "{ shopt -u extglob || setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL; } "
    ">/dev/null 2>&1 || true"
)


def bash_tool_call(command: str, cwd_file: str) -> str:
    quoted = "'" + command.replace("'", "'\\''") + "'"
    return f"{_SNAPSHOT_AND_SETUP} && eval {quoted} < /dev/null && pwd -P >| {cwd_file}"


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


class ToolScopeShippedTests(unittest.TestCase):
    def test_the_wrapper_is_a_deployable_script_not_a_path_command(self) -> None:
        self.assertIn(TOOL_SCOPE_SCRIPT, HOOK_SCRIPTS)
        self.assertNotIn(TOOL_SCOPE_SCRIPT, PATH_COMMANDS)

    def test_the_wrapper_is_valid_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / TOOL_SCOPE_SCRIPT
            path.write_text(HOOK_SCRIPTS[TOOL_SCOPE_SCRIPT])
            proc = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, timeout=20
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_kill_message_carries_the_shared_oom_text(self) -> None:
        # The fix every OOM report and heavy's kill message give, reaching
        # bash as a double-quoted string.
        self.assertIn(probe.OOM_KILL_FIX.split(",")[0], HOOK_SCRIPTS[TOOL_SCOPE_SCRIPT])


class _ToolScopeCase(unittest.TestCase):
    """Run the deployed wrapper with stub systemd tools, in a fixed cgroup tree."""

    def setUp(self) -> None:
        if not _namespaces_work():
            self.skipTest("no unprivileged user and mount namespaces")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        scripts = self.base / "scripts"
        with mock.patch.dict("os.environ", {"HOME": str(self.base)}):
            deploy_scripts([TOOL_SCOPE_SCRIPT], scripts)
        self.wrapper = scripts / TOOL_SCOPE_SCRIPT
        stubs = self.base / "stub-bin"
        stubs.mkdir()
        for name, text in (
            ("systemd-run", _STUB_SYSTEMD_RUN),
            ("systemctl", _STUB_SYSTEMCTL),
            ("busctl", _STUB_BUSCTL),
        ):
            (stubs / name).write_text(text)
            (stubs / name).chmod(0o755)
        self.stub_log = self.base / "systemd-run.log"
        self.systemctl_log = self.base / "systemctl.log"
        self.busctl_log = self.base / "busctl.log"
        self.slice_state = self.base / "slice-state"
        self.cgroup = self.base / "cgroup"
        self.cgroup.mkdir()
        # The cgroup the command's scope has: inside the tools slice, inside
        # the session slice.
        self.tools_cg = (
            "/user.slice/user-1000.slice/user@1000.service/claudewheel.slice/"
            f"{SESSION_SLICE}/{TOOLS_SLICE}"
        )
        self.scope_cg = f"{self.tools_cg}/claudewheel-tool-4242-1790000000-77.scope"
        self.own_cgroup = self.base / "own-cgroup"
        self.own_cgroup.write_text(f"0::{self.scope_cg}\n")
        self._events(self.scope_cg, oom=0, oom_kill=0)
        self._events(self.tools_cg, oom=0, oom_kill=0)
        self.cwd_file = self.base / "cwd"
        self.env = {
            "PATH": f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.base),
            "STUB_LOG": str(self.stub_log),
            "STUB_SYSTEMCTL_LOG": str(self.systemctl_log),
            "STUB_BUSCTL_LOG": str(self.busctl_log),
            "STUB_SLICE": str(self.slice_state),
            "CLAUDEWHEEL_TOOL_SLICE": TOOLS_SLICE,
            "CLAUDEWHEEL_SESSION_SCOPE": SESSION_SCOPE,
            "CLAUDEWHEEL_TOOL_MEMORY_MAX": "6G",
            "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX": "1G",
        }

    def _events(self, cg: str, *, oom: int, oom_kill: int) -> None:
        """The memory.events the cgroup *cg* of the fixed tree holds."""
        d = self.cgroup / cg.lstrip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.events").write_text(
            f"low 0\nhigh 0\nmax 0\noom {oom}\noom_kill {oom_kill}\noom_group_kill 0\n"
        )

    def _peak(self, cg: str, peak_bytes: int) -> None:
        d = self.cgroup / cg.lstrip("/")
        (d / "memory.peak").write_text(f"{peak_bytes}\n")

    def _slice_exists(self, max_: str = SIX_G, swap: str = ONE_G) -> None:
        self.slice_state.write_text(
            f"ActiveState=active\nMemoryMax={max_}\nMemorySwapMax={swap}\n"
        )

    def _run(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "bash",
            "-c",
            'mount --bind "$1" /sys/fs/cgroup && mount --bind "$2" /proc/$$/cgroup '
            '&& shift 2 && exec "$@"',
            "tool-scope-under-test",
            str(self.cgroup),
            str(self.own_cgroup),
            str(self.wrapper),
            *args,
        ]
        return subprocess.run(
            argv,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _call(
        self, command: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._run(bash_tool_call(command, str(self.cwd_file)), env=env)

    def _stub_args(self) -> list[str]:
        return self.stub_log.read_text().splitlines() if self.stub_log.exists() else []


class BashToolCallTests(_ToolScopeCase):
    def setUp(self) -> None:
        super().setUp()
        self._slice_exists()

    def test_a_bash_tool_call_runs_in_its_own_scope_in_the_tools_slice(self) -> None:
        proc = self._call("echo hello")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "hello\n")
        self.assertEqual(proc.stderr, "")
        args = self._stub_args()
        for expected in (
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--expand-environment=no",
            f"--slice={TOOLS_SLICE}",
            "OOMPolicy=continue",
        ):
            self.assertIn(expected, args)
        [unit] = [a for a in args if a.startswith("--unit=")]
        self.assertRegex(
            unit, r"^--unit=claudewheel-tool-4242-1790000000-[0-9]+\.scope$"
        )
        # The scope's name is what the probe runner reads a kill's session from.
        name = unit.removeprefix("--unit=")
        self.assertRegex(name, probe.TOOL_SCOPE_RE)
        self.assertIn(f"--description=Bash tool command of {SESSION_SCOPE}", args)
        # The scope itself carries no cap: the slice caps every command together.
        self.assertFalse([a for a in args if a.startswith("MemoryMax")])

    def test_the_command_keeps_its_exit_status_and_working_directory(self) -> None:
        proc = self._call("cd / && exit 3")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        proc = self._call("cd /tmp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.cwd_file.read_text().strip(), "/tmp")

    def test_dollar_signs_reach_the_command_unexpanded(self) -> None:
        proc = self._call('x=5; echo "cost \\$$x"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "cost $5\n")

    def test_stdin_reaches_the_command(self) -> None:
        proc = subprocess.run(
            [
                *self._ns_prefix(),
                str(self.wrapper),
                "cat; true && pwd -P >| " + str(self.cwd_file),
            ],
            env=self.env,
            input="piped\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.stdout, "piped\n", proc.stderr)

    def _ns_prefix(self) -> list[str]:
        return [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "bash",
            "-c",
            'mount --bind "$1" /sys/fs/cgroup && mount --bind "$2" /proc/$$/cgroup '
            '&& shift 2 && exec "$@"',
            "tool-scope-under-test",
            str(self.cgroup),
            str(self.own_cgroup),
        ]

    def test_an_existing_capped_slice_is_not_created_again(self) -> None:
        self._call("true")
        self.assertFalse(self.busctl_log.exists())


class OtherCommandTests(_ToolScopeCase):
    """Hooks, the status line and MCP servers run as they are, outside the cap."""

    def test_a_hook_command_runs_directly(self) -> None:
        proc = self._run("echo from-a-hook; exit 4")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertEqual(proc.stdout, "from-a-hook\n")
        self.assertEqual(self._stub_args(), [])
        self.assertFalse(self.systemctl_log.exists())

    def test_a_hook_reads_its_payload_from_stdin(self) -> None:
        proc = subprocess.run(
            [str(self.wrapper), "cat"],
            env=self.env,
            input='{"hook_event_name": "PreToolUse"}',
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.stdout, '{"hook_event_name": "PreToolUse"}')

    def test_without_a_tools_slice_a_bash_call_is_refused(self) -> None:
        proc = self._call("echo never", env={"CLAUDEWHEEL_TOOL_SLICE": ""})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn(
            "claudewheel: refused to run this command: CLAUDEWHEEL_TOOL_SLICE is empty",
            proc.stderr,
        )
        self.assertEqual(self._stub_args(), [])

    def test_without_a_tools_slice_a_hook_still_runs(self) -> None:
        proc = self._run("echo hook", env={"CLAUDEWHEEL_TOOL_SLICE": ""})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "hook\n")


class SliceCreationTests(_ToolScopeCase):
    def test_a_missing_slice_is_created_with_the_cap(self) -> None:
        proc = self._call("echo ran")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "ran\n")
        [call] = self.busctl_log.read_text().splitlines()
        self.assertIn("StartTransientUnit", call)
        self.assertIn(f" {TOOLS_SLICE} fail ", call)
        self.assertIn(f"MemoryMax t {SIX_G}", call)
        self.assertIn(f"MemorySwapMax t {ONE_G}", call)
        self.assertIn(f"tools of {SESSION_SCOPE}", call)
        self.assertIn(f"--slice={TOOLS_SLICE}", self._stub_args())

    def test_a_slice_another_command_created_first_is_used(self) -> None:
        # Two first commands at once: the second creation fails, and the slice
        # the first one made carries the cap.
        self._slice_exists()
        self.slice_state.rename(self.base / "made-by-the-other")
        env = {"STUB_BUSCTL_FAIL": "1"}
        # The stub cannot race, so the other command's slice appears through
        # show once the creation has failed.
        (self.base / "stub-bin" / "busctl").write_text(
            _STUB_BUSCTL.replace(
                'echo "Call failed',
                f'cp {self.base / "made-by-the-other"} "$STUB_SLICE"; echo "Call failed',
            )
        )
        proc = self._call("echo ran", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "ran\n")

    def test_a_slice_without_the_cap_refuses_the_command(self) -> None:
        proc = self._call("echo never", env={"STUB_BUSCTL_NOCAP": "1"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn(
            f"claudewheel: refused to run this command: {TOOLS_SLICE}", proc.stderr
        )
        self.assertIn("6G", proc.stderr)
        self.assertEqual(self._stub_args(), [])

    def test_a_failed_creation_refuses_the_command_naming_why(self) -> None:
        proc = self._call("echo never", env={"STUB_BUSCTL_FAIL": "1"})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("claudewheel: refused to run this command", proc.stderr)
        self.assertIn("already loaded", proc.stderr)


class OomKillMessageTests(_ToolScopeCase):
    def setUp(self) -> None:
        super().setUp()
        self._slice_exists()

    def _killed_at_the_cap(self, kills: int = 1) -> str:
        """A command that ends as if the kernel killed *kills* of its processes
        at the tools slice's limit: it writes the counters, then exits 137."""
        events = self.cgroup / self.scope_cg.lstrip("/") / "memory.events"
        slice_events = self.cgroup / self.tools_cg.lstrip("/") / "memory.events"
        return (
            f"printf 'oom 0\\noom_kill {kills}\\n' > {events}; "
            f"printf 'oom 1\\noom_kill {kills}\\n' > {slice_events}; "
            "echo partial output; kill -9 $$"
        )

    def test_a_kill_at_the_cap_is_named_with_the_cap(self) -> None:
        self._peak(self.scope_cg, 5 * 1024**3 + 1)
        proc = self._call(self._killed_at_the_cap())
        self.assertEqual(proc.returncode, 137)
        self.assertEqual(proc.stdout, "partial output\n")
        message = proc.stderr.strip()
        self.assertTrue(
            message.startswith(
                "claudewheel: this command was OOM-killed: the kernel's OOM killer "
                "killed 1 of its processes when this session's Bash commands "
                "reached the 6G memory cap they share"
            ),
            message,
        )
        self.assertIn(TOOLS_SLICE, message)
        self.assertIn("MemoryMax=6G, MemorySwapMax=1G", message)
        self.assertIn("claudewheel-tool-4242-1790000000-", message)
        self.assertIn("peaked at 5.1G", message)
        self.assertIn("Claude Code itself is not capped and keeps running", message)
        self.assertIn(probe.OOM_KILL_FIX, message)
        self.assertIn("do not rerun it", message)
        self.assertEqual(len(proc.stderr.strip().splitlines()), 1, proc.stderr)

    def test_the_number_of_processes_killed_is_named(self) -> None:
        proc = self._call(self._killed_at_the_cap(kills=3))
        self.assertIn("killed 3 of its processes", proc.stderr)

    def test_a_kill_when_the_machine_ran_out_is_not_blamed_on_the_cap(self) -> None:
        events = self.cgroup / self.scope_cg.lstrip("/") / "memory.events"
        proc = self._call(f"printf 'oom 0\\noom_kill 1\\n' > {events}; kill -9 $$")
        self.assertEqual(proc.returncode, 137)
        self.assertIn(
            "killed 1 of its processes when this machine ran out of memory, with "
            "this session's Bash commands under the 6G memory cap they share",
            proc.stderr,
        )
        self.assertNotIn("reached the 6G", proc.stderr)

    def test_a_command_that_survived_a_child_kill_still_says_so(self) -> None:
        # A child killed while the command itself went on and succeeded.
        events = self.cgroup / self.scope_cg.lstrip("/") / "memory.events"
        proc = self._call(f"printf 'oom 0\\noom_kill 1\\n' > {events}; true")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("claudewheel: this command was OOM-killed", proc.stderr)

    def test_no_kill_no_message(self) -> None:
        proc = self._call("exit 137")
        self.assertEqual(proc.returncode, 137)
        self.assertEqual(proc.stderr, "")

    def test_no_peak_is_named_when_the_scope_has_none(self) -> None:
        proc = self._call(self._killed_at_the_cap())
        self.assertNotIn("peaked", proc.stderr)


class SystemdLayoutTests(unittest.TestCase):
    """The wrapper against the user manager: the slice it creates and where its scope lands.

    Starts a session slice, a tools slice capped at 64M with no swap, and one
    tool scope that prints its cgroup and exits, all under names of this test
    process, and stops the session slice after. Nothing is killed.

    Skipped where no systemd user manager answers (a CI container).
    """

    def setUp(self) -> None:
        if (
            shutil.which("systemd-run") is None
            or shutil.which("busctl") is None
            or subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", "true"],
                capture_output=True,
                timeout=20,
            ).returncode
            != 0
        ):
            self.skipTest("no systemd user manager")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        deploy_scripts([TOOL_SCOPE_SCRIPT], base / "scripts")
        self.wrapper = base / "scripts" / TOOL_SCOPE_SCRIPT
        self.cwd_file = base / "cwd"
        pid, launched = os.getpid(), int(time.time())
        self.session_slice = f"claudewheel-{pid}_{launched}.slice"
        self.tools_slice = f"claudewheel-{pid}_{launched}-tools.slice"
        self.session_scope = f"claudewheel-session-{pid}-{launched}.scope"
        self.addCleanup(
            subprocess.run,
            ["systemctl", "--user", "stop", self.session_slice],
            capture_output=True,
            timeout=30,
        )

    def test_the_command_runs_in_a_tool_scope_of_the_capped_tools_slice(self) -> None:
        command = (
            "cg=$(sed -n 's/^0:://p' /proc/self/cgroup); echo \"$cg\"; "
            'cat "/sys/fs/cgroup${cg%/*}/memory.max" "/sys/fs/cgroup${cg%/*}/memory.swap.max"'
        )
        proc = subprocess.run(
            [str(self.wrapper), bash_tool_call(command, str(self.cwd_file))],
            env={
                **os.environ,
                "CLAUDEWHEEL_TOOL_SLICE": self.tools_slice,
                "CLAUDEWHEEL_SESSION_SCOPE": self.session_scope,
                "CLAUDEWHEEL_TOOL_MEMORY_MAX": "64M",
                "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX": "0",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cgroup, memory_max, swap_max = proc.stdout.splitlines()
        parts = cgroup.split("/")
        self.assertEqual(
            parts[-4:-1], ["claudewheel.slice", self.session_slice, self.tools_slice]
        )
        self.assertRegex(parts[-1], probe.TOOL_SCOPE_RE)
        self.assertEqual(probe.session_scope_of_unit(parts[-1]), self.session_scope)
        self.assertEqual(memory_max, str(64 * 1024**2))
        self.assertEqual(swap_max, "0")


if __name__ == "__main__":
    unittest.main()
