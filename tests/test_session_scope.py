"""Tests for the systemd user units every launched session runs in.

claudewheel starts each session's Claude Code process in a session scope with
no memory cap, inside a session slice, and points the client's
CLAUDE_CODE_SHELL_PREFIX at claudewheel-tool-scope, which runs each Bash
command in a tool scope inside the session's tools slice. The tools slice caps
the session's Bash commands together at the cap from config.json, so a runaway
command is killed while the session goes on (tests/test_tool_scope.py covers
the prefix itself).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import cli, launch
from claudewheel.defaults import DEFAULT_CONFIG
from claudewheel.hook_scripts import HOOK_SCRIPTS, TOOL_SCOPE_SCRIPT
from claudewheel.launch import SessionUnits, ToolCap, do_launch
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    FakeAppConfigStore,
    inert_workspace,
    setup_temp_config_dir,
)

_SYSTEMD_RUN = "/usr/bin/systemd-run"


class ToolCapConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["tool_memory_max"], "6G")
        self.assertEqual(DEFAULT_CONFIG["tool_memory_swap_max"], "1G")
        self.assertEqual(ToolCap.from_config(DEFAULT_CONFIG), ToolCap("6G", "1G"))
        for key in ("session_memory_max", "session_memory_swap_max"):
            self.assertNotIn(key, DEFAULT_CONFIG)

    def test_configured_values_are_used(self) -> None:
        cap = ToolCap.from_config(
            {"tool_memory_max": "8G", "tool_memory_swap_max": "512M"}
        )
        self.assertEqual(cap, ToolCap("8G", "512M"))

    def test_swap_may_be_zero(self) -> None:
        cap = ToolCap.from_config(
            {"tool_memory_max": "6G", "tool_memory_swap_max": "0"}
        )
        self.assertEqual(cap.memory_swap_max, "0")

    def test_malformed_sizes_are_refused(self) -> None:
        for key, value in (
            ("tool_memory_max", "0"),
            ("tool_memory_max", "6GB"),
            ("tool_memory_max", "6g"),
            ("tool_memory_max", "1.5G"),
            ("tool_memory_max", "infinity"),
            ("tool_memory_max", 6),
            ("tool_memory_swap_max", "-1G"),
            ("tool_memory_swap_max", ""),
            ("tool_memory_swap_max", None),
        ):
            with self.subTest(key=key, value=value):
                config = {**DEFAULT_CONFIG, key: value}
                with self.assertRaises(ValueError) as ctx:
                    ToolCap.from_config(config)
                self.assertIn(f"config.json {key}", str(ctx.exception))
                self.assertIn("K, M, G, or T suffix", str(ctx.exception))

    def test_a_missing_key_is_refused(self) -> None:
        for key in ("tool_memory_max", "tool_memory_swap_max"):
            with self.subTest(key=key):
                config = {k: v for k, v in DEFAULT_CONFIG.items() if k != key}
                with self.assertRaises(ValueError) as ctx:
                    ToolCap.from_config(config)
                self.assertIn(f"config.json has no {key}", str(ctx.exception))
                self.assertIn("restart claudewheel", str(ctx.exception))

    def test_restarting_claudewheel_adds_a_missing_key(self) -> None:
        # The fix the missing-key error names: claudewheel's startup adds the
        # keys with their defaults, and keeps a value already set.
        with tempfile.TemporaryDirectory() as tmp:
            paths = setup_temp_config_dir(
                Path(tmp), config={"theme": "dark", "tool_memory_max": "8G"}
            )
            config = Workspace.open(paths["CONFIG_DIR"]).appconfig().config
        self.assertEqual(ToolCap.from_config(config), ToolCap("8G", "1G"))

    def test_the_old_session_ceiling_is_dropped_for_the_tool_cap(self) -> None:
        # A config.json written before: the ceiling capped the whole session,
        # Claude Code included, and does not become the tool cap.
        with tempfile.TemporaryDirectory() as tmp:
            paths = setup_temp_config_dir(
                Path(tmp),
                config={
                    "theme": "dark",
                    "session_memory_max": "4G",
                    "session_memory_swap_max": "512M",
                    "_schema_version": 7,
                },
            )
            config = Workspace.open(paths["CONFIG_DIR"]).appconfig().config
        self.assertNotIn("session_memory_max", config)
        self.assertNotIn("session_memory_swap_max", config)
        self.assertEqual(ToolCap.from_config(config), ToolCap("6G", "1G"))
        self.assertGreaterEqual(config["_schema_version"], 8)


class SessionUnitsTests(unittest.TestCase):
    UNITS = SessionUnits(4242, 1790000000)

    def test_names(self) -> None:
        self.assertEqual(self.UNITS.session_slice, "claudewheel-4242_1790000000.slice")
        self.assertEqual(
            self.UNITS.session_scope, "claudewheel-session-4242-1790000000.scope"
        )
        self.assertEqual(
            self.UNITS.tool_slice, "claudewheel-4242_1790000000-tools.slice"
        )

    def test_the_tools_slice_is_named_inside_the_session_slice(self) -> None:
        # A slice's dashes name its parents: claudewheel-A-tools.slice sits in
        # claudewheel-A.slice, which sits in claudewheel.slice.
        stem = self.UNITS.session_slice.removesuffix(".slice")
        self.assertEqual(self.UNITS.tool_slice, f"{stem}-tools.slice")
        self.assertEqual(stem.count("-"), 1)

    def test_the_session_scope_is_what_the_probes_read(self) -> None:
        from claudewheel import probe

        self.assertRegex(self.UNITS.session_scope, probe.SESSION_SCOPE_RE)
        self.assertRegex(self.UNITS.session_slice, launch.SESSION_SLICE_RE)


class DoLaunchTests(unittest.TestCase):
    """do_launch execs systemd-run, which execs the client inside the session scope."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.scripts = Path(self._tmp.name) / "scripts"
        sweep = mock.patch(
            "claudewheel.launch.sweep_ended_sessions", autospec=True, return_value=[]
        )
        self.m_sweep = sweep.start()
        self.addCleanup(sweep.stop)

    def _launch(
        self,
        argv: list[str],
        cap: ToolCap,
        env: dict[str, str] | None = None,
        scripts: Path | None = None,
    ) -> tuple[mock.MagicMock, str]:
        err = io.StringIO()
        with (
            mock.patch("shutil.which", autospec=True, return_value=_SYSTEMD_RUN),
            mock.patch("os.chdir", autospec=True),
            mock.patch("os.execvpe", autospec=True) as m_exec,
            mock.patch("os.getpid", autospec=True, return_value=4242),
            mock.patch("time.time", autospec=True, return_value=1790000000.5),
            redirect_stderr(err),
        ):
            do_launch(
                "/work",
                argv,
                env if env is not None else {},
                cap,
                scripts or self.scripts,
            )
        return m_exec, err.getvalue()

    def test_the_client_runs_uncapped_in_its_session_slice(self) -> None:
        argv = ["/opt/claude/bin/claude", "--model", "m-1"]
        m_exec, _ = self._launch(argv, ToolCap("6G", "1G"), {"PATH": "/usr/bin"})
        m_exec.assert_called_once()
        binary, e_argv, _ = m_exec.call_args[0]
        self.assertEqual(binary, _SYSTEMD_RUN)
        self.assertEqual(
            e_argv,
            [
                _SYSTEMD_RUN,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "--expand-environment=no",
                "--slice=claudewheel-4242_1790000000.slice",
                "--unit=claudewheel-session-4242-1790000000.scope",
                "-p",
                "OOMPolicy=continue",
                "--",
                *argv,
            ],
        )
        self.assertFalse(
            [a for a in e_argv if "MemoryMax" in a or "MemorySwapMax" in a]
        )

    def test_the_client_runs_its_commands_through_the_tool_scope_prefix(self) -> None:
        m_exec, _ = self._launch(
            ["/bin/claude"], ToolCap("6G", "1G"), {"PATH": "/usr/bin", "KEEP": "1"}
        )
        env = m_exec.call_args[0][2]
        self.assertEqual(
            env,
            {
                "PATH": "/usr/bin",
                "KEEP": "1",
                "CLAUDE_CODE_SHELL_PREFIX": str(self.scripts / TOOL_SCOPE_SCRIPT),
                "CLAUDEWHEEL_TOOL_SLICE": "claudewheel-4242_1790000000-tools.slice",
                "CLAUDEWHEEL_SESSION_SCOPE": "claudewheel-session-4242-1790000000.scope",
                "CLAUDEWHEEL_TOOL_MEMORY_MAX": "6G",
                "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX": "1G",
            },
        )
        self.assertEqual(set(launch.TOOL_ENV_KEYS), set(env) - {"PATH", "KEEP"})

    def test_values_inherited_from_a_parent_session_are_replaced(self) -> None:
        # A launcher started from inside a launched session hands its child
        # the child's own units, not its parent's.
        inherited = {
            "CLAUDE_CODE_SHELL_PREFIX": "/elsewhere/prefix",
            "CLAUDEWHEEL_TOOL_SLICE": "claudewheel-1_1-tools.slice",
            "CLAUDEWHEEL_SESSION_SCOPE": "claudewheel-session-1-1.scope",
            "CLAUDEWHEEL_TOOL_MEMORY_MAX": "2G",
            "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX": "0",
        }
        m_exec, _ = self._launch(["/bin/claude"], ToolCap("6G", "1G"), inherited)
        env = m_exec.call_args[0][2]
        self.assertEqual(
            env["CLAUDEWHEEL_TOOL_SLICE"], "claudewheel-4242_1790000000-tools.slice"
        )
        self.assertEqual(env["CLAUDEWHEEL_TOOL_MEMORY_MAX"], "6G")
        self.assertEqual(
            env["CLAUDE_CODE_SHELL_PREFIX"], str(self.scripts / TOOL_SCOPE_SCRIPT)
        )

    def test_the_layout_and_cap_are_printed_at_launch(self) -> None:
        _, stderr = self._launch(["/bin/claude"], ToolCap("8G", "0"))
        self.assertEqual(
            stderr,
            "claudewheel: this session runs in "
            "claudewheel-session-4242-1790000000.scope, not memory-capped; its "
            "Bash commands run in claudewheel-4242_1790000000-tools.slice, capped "
            "together at 8G of memory and 0 of swap (tool_memory_max and "
            "tool_memory_swap_max in config.json)\n",
        )

    def test_a_missing_prefix_is_deployed(self) -> None:
        self.assertFalse(self.scripts.exists())
        self._launch(["/bin/claude"], ToolCap("6G", "1G"))
        prefix = self.scripts / TOOL_SCOPE_SCRIPT
        self.assertEqual(prefix.read_text(), HOOK_SCRIPTS[TOOL_SCOPE_SCRIPT])
        self.assertTrue(os.access(prefix, os.X_OK))

    def test_a_deployed_prefix_is_left_as_it_is(self) -> None:
        self.scripts.mkdir()
        prefix = self.scripts / TOOL_SCOPE_SCRIPT
        prefix.write_text('#!/bin/sh\nexec bash -c "$1"\n')
        prefix.chmod(0o755)
        self._launch(["/bin/claude"], ToolCap("6G", "1G"))
        self.assertEqual(prefix.read_text(), '#!/bin/sh\nexec bash -c "$1"\n')

    def test_ended_sessions_are_swept_before_the_exec(self) -> None:
        self._launch(["/bin/claude"], ToolCap("6G", "1G"))
        self.m_sweep.assert_called_once_with(now=1790000000.5)

    def test_a_prefix_path_with_whitespace_is_refused(self) -> None:
        with (
            mock.patch("shutil.which", autospec=True, return_value=_SYSTEMD_RUN),
            mock.patch("os.execvpe", autospec=True) as m_exec,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(ValueError) as ctx:
                do_launch(
                    "/w", ["/bin/claude"], {}, ToolCap("6G", "1G"), Path("/a b/scripts")
                )
        m_exec.assert_not_called()
        self.assertIn("whitespace", str(ctx.exception))

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
                    ToolCap("6G", "1G"),
                    self.scripts,
                )
        m_which.assert_called_once_with("systemd-run", path="/x:/y")
        m_exec.assert_not_called()
        self.assertIn("systemd-run is not on PATH", str(ctx.exception))


class UnrunnablePrefixTests(unittest.TestCase):
    """A prefix that cannot be run fails the launch, and the fix it names clears it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "cw"
        env = mock.patch.dict(
            "os.environ",
            {"CLAUDEWHEEL_CONFIG_DIR": str(self.root), "HOME": self._tmp.name},
        )
        env.start()
        self.addCleanup(env.stop)
        sweep = mock.patch(
            "claudewheel.launch.sweep_ended_sessions", autospec=True, return_value=[]
        )
        sweep.start()
        self.addCleanup(sweep.stop)
        self.scripts = Workspace.open(self.root).scripts_dir

    def _launch(self) -> mock.MagicMock:
        with (
            mock.patch("shutil.which", autospec=True, return_value=_SYSTEMD_RUN),
            mock.patch("os.chdir", autospec=True),
            mock.patch("os.execvpe", autospec=True) as m_exec,
            redirect_stderr(io.StringIO()),
        ):
            do_launch("/w", ["/bin/claude"], {}, ToolCap("6G", "1G"), self.scripts)
        return m_exec

    def test_redeploying_the_prefix_clears_the_refusal(self) -> None:
        self.scripts.mkdir(parents=True)
        prefix = self.scripts / TOOL_SCOPE_SCRIPT
        prefix.write_text(HOOK_SCRIPTS[TOOL_SCOPE_SCRIPT])
        prefix.chmod(0o644)
        with self.assertRaises(OSError) as ctx:
            self._launch()
        message = str(ctx.exception)
        self.assertIn("is not executable", message)
        fix = message.split("redeploy it with '", 1)[1].rstrip("'")
        self.assertEqual(
            fix, f"claudewheel deploy-hooks {TOOL_SCOPE_SCRIPT} --force-overwrite"
        )
        # The fix, as the message spells it.
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("sys.argv", ["claudewheel", *fix.split()[1:]]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                self.assertIn(exc.code, (0, None), err.getvalue())
        self._launch().assert_called_once()


class SweepTests(unittest.TestCase):
    """sweep_ended_sessions stops the empty slices of ended sessions and nothing else."""

    NOW = 1_790_000_600

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cgroup = Path(self._tmp.name) / "cgroup"
        root = mock.patch("claudewheel.launch.CGROUP_ROOT", self.cgroup)
        root.start()
        self.addCleanup(root.stop)
        self.listing: list[str] = []
        self.calls: list[list[str]] = []

    def _slice(self, name: str, populated: bool | None) -> None:
        """A session slice systemd lists as active, with its cgroup when *populated* is given."""
        self.listing.append(f"{name} loaded active active Slice /claudewheel/{name}")
        if populated is not None:
            d = self.cgroup / "user.slice" / "claudewheel.slice" / name
            d.mkdir(parents=True)
            (d / "cgroup.events").write_text(
                f"populated {1 if populated else 0}\nfrozen 0\n"
            )

    def _run(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if "list-units" in argv:
            out = "".join(f"{line}\n" for line in self.listing)
        elif "show" in argv:
            out = f"/user.slice/claudewheel.slice/{argv[-1]}\n"
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _sweep(self) -> list[str]:
        with mock.patch(
            "claudewheel.effects.run", autospec=True, side_effect=self._run
        ):
            return launch.sweep_ended_sessions(now=self.NOW)

    def _stops(self) -> list[str]:
        return [c[-1] for c in self.calls if "stop" in c]

    def test_an_empty_slice_of_an_ended_session_is_stopped(self) -> None:
        self._slice("claudewheel-4242_1790000000.slice", populated=False)
        self.assertEqual(self._sweep(), ["claudewheel-4242_1790000000.slice"])
        self.assertIn(
            ["systemctl", "--user", "stop", "claudewheel-4242_1790000000.slice"],
            self.calls,
        )

    def test_a_slice_with_a_process_in_it_is_kept(self) -> None:
        self._slice("claudewheel-4242_1790000000.slice", populated=True)
        self.assertEqual(self._sweep(), [])
        self.assertEqual(self._stops(), [])

    def test_a_slice_launched_within_the_last_minute_is_kept(self) -> None:
        young = self.NOW - launch.SWEEP_AFTER_SECONDS + 1
        self._slice(f"claudewheel-4242_{young}.slice", populated=False)
        self.assertEqual(self._sweep(), [])
        self.assertEqual(self._stops(), [])

    def test_only_session_slices_are_considered(self) -> None:
        # The tools slice goes with its session slice; claudewheel.slice is
        # the parent of every session.
        self._slice("claudewheel.slice", populated=False)
        self._slice("claudewheel-4242_1790000000-tools.slice", populated=False)
        self.assertEqual(self._sweep(), [])
        self.assertEqual(self._stops(), [])

    def test_a_slice_whose_cgroup_cannot_be_read_is_kept(self) -> None:
        self._slice("claudewheel-4242_1790000000.slice", populated=None)
        self.assertEqual(self._sweep(), [])
        self.assertEqual(self._stops(), [])

    def test_the_listing_asks_only_for_active_claudewheel_slices(self) -> None:
        self._sweep()
        [listing] = [c for c in self.calls if "list-units" in c]
        self.assertIn("--type=slice", listing)
        self.assertIn("--state=active", listing)
        self.assertEqual(listing[-1], "claudewheel-*.slice")


class LaunchSequenceTests(unittest.TestCase):
    """_do_launch_sequence reads the cap from config.json before launching."""

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

    def test_the_configured_cap_reaches_do_launch(self) -> None:
        do_launch_mock, _, code = self._run({"tool_memory_max": "8G"})
        self.assertEqual(code, 0)
        do_launch_mock.assert_called_once_with(
            "/cwd",
            ["/bin/claude"],
            {},
            ToolCap("8G", "1G"),
            inert_workspace(Path("/nonexistent-cw-root")).scripts_dir,
        )

    def test_a_malformed_cap_fails_the_launch(self) -> None:
        do_launch_mock, stderr, code = self._run({"tool_memory_max": "lots"})
        self.assertEqual(code, 1)
        do_launch_mock.assert_not_called()
        self.assertIn("Launch failed: config.json tool_memory_max", stderr)

    def test_fixing_the_cap_clears_the_failure(self) -> None:
        # The fix the error names: a whole number with a suffix.
        do_launch_mock, stderr, code = self._run({"tool_memory_max": "8G"})
        self.assertEqual(code, 0, stderr)
        do_launch_mock.assert_called_once()


class NestedScopeTests(unittest.TestCase):
    """Where a scope started from inside another scope lands.

    heavy runs its command with 'systemd-run --user --scope' from inside a
    session's tool scope. The user manager places every transient scope that
    names no slice in its own default slice, not under the scope of the
    process that asked for it, so the heavy scope is outside the session's
    tools slice: a heavy job's memory counts against its own cap only, never
    against the tool cap too.

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
