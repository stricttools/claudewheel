"""Tests for the probe command group: create, list, stop, subscribe, unsubscribe."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli, lifecycle, probe
from claudewheel.workspace import Workspace

S = "4d97ca01-9d56-4f49-8047-77f5160febde"
T = "0b1e7c52-4f6f-4d7e-9a53-1f2a3b4c5d6e"
LAUNCHED = 1_790_000_000


def cgroup_of(scope: str) -> str:
    return f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}\n"


SCOPE_S = cgroup_of(f"claudewheel-session-4242-{LAUNCHED}.scope")
SCOPE_T = cgroup_of(f"claudewheel-session-7777-{LAUNCHED}.scope")
OUTSIDE = cgroup_of("heavy-1-2.scope")


class _CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "cw"
        env = mock.patch.dict(
            "os.environ",
            {"CLAUDEWHEEL_CONFIG_DIR": str(self.root), "HOME": str(base / "home")},
        )
        env.start()
        self.addCleanup(env.stop)
        self.ws = Workspace.open(self.root)
        self.store = self.ws.probes
        for session, pid in ((S, 4242), (T, 7777)):
            lifecycle.append_event(
                self.ws.shared.lifecycle_dir,
                lifecycle.StartedEvent(
                    at=lifecycle.now_timestamp(LAUNCHED * 1000 + 1000),
                    session=session,
                    source="hook",
                    cwd="/w",
                    config_dir="/c",
                    profile=None,
                    claude_version=None,
                    model=None,
                    permissions=None,
                    entry="startup",
                    transcript=None,
                    pid=pid,
                ),
            )

    def run_cli(self, *argv: str, cgroup: str = SCOPE_S) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            mock.patch("sys.argv", ["c", *argv]),
            mock.patch.object(probe, "own_cgroup_text", return_value=cgroup),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def create(self, *extra: str, cgroup: str = SCOPE_S) -> tuple[str, str]:
        code, out, err = self.run_cli(
            "probe", "create", "oom-kill", "--deadline", "2h", *extra, cgroup=cgroup
        )
        self.assertEqual(code, 0, err)
        match = probe.BIND_LINE_RE.search(out)
        assert match is not None, out
        return match.group(2), match.group(1)


class CreateTests(_CliCase):
    def test_create_writes_the_probe_and_subscribes_the_calling_session(self) -> None:
        probe_id, sub = self.create(
            "--session", T, "--count", "2", "--until-watched-ends"
        )
        state = probe.read_probe(self.store, probe_id)
        self.assertEqual((state.session, state.watch_session), (S, T))
        self.assertEqual((state.until_count, state.until_watched_ends), (2, True))
        self.assertEqual(state.subscriptions[sub].session, S)
        self.assertEqual(state.subscriptions[sub].agent, probe.UNBOUND)
        deadline = lifecycle.parse_timestamp_ms(state.deadline)
        self.assertAlmostEqual(deadline / 1000, probe.now_ms() / 1000 + 7200, delta=60)

    def test_all_sessions_is_spelled_out(self) -> None:
        probe_id, _ = self.create("--all-sessions")
        self.assertIsNone(probe.read_probe(self.store, probe_id).watch_session)
        code, _, err = self.run_cli("probe", "create", "oom-kill", "--deadline", "2h")
        self.assertNotEqual(code, 0)
        self.assertIn("--session", err)
        self.assertIn("--all-sessions", err)

    def test_a_deadline_is_required(self) -> None:
        code, _, err = self.run_cli("probe", "create", "oom-kill", "--all-sessions")
        self.assertNotEqual(code, 0)
        self.assertIn("--deadline", err)

    def test_an_arbitrary_command_is_refused_naming_the_kinds(self) -> None:
        code, _, err = self.run_cli(
            "probe", "create", "free -m", "--all-sessions", "--deadline", "1h"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("oom-kill", err)
        code, _, err = self.run_cli(
            "probe",
            "create",
            "oom-kill",
            "--all-sessions",
            "--deadline",
            "1h",
            "--",
            "free",
            "-m",
        )
        self.assertEqual(code, 1)
        self.assertIn("the kinds that exist are: oom-kill", err)
        self.assertFalse(self.store.probes_dir.exists())

    def test_refusals_write_nothing(self) -> None:
        cases = [
            ("--session", "11111111-2222-4333-8444-555555555555"),
            ("--session", "not-a-uuid"),
            ("--all-sessions", "--until-watched-ends"),
            ("--all-sessions", "--count", "0"),
            ("--all-sessions", "--until-file", "relative/path"),
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                code, _, err = self.run_cli(
                    "probe", "create", "oom-kill", "--deadline", "1h", *extra
                )
                self.assertEqual(code, 1, err)
                self.assertIn("Error:", err)
        code, _, err = self.run_cli(
            "probe", "create", "oom-kill", "--all-sessions", "--deadline", "2 hours"
        )
        self.assertEqual(code, 1)
        self.assertIn("90s, 30m, 2h, 7d", err)
        self.assertFalse(self.store.probes_dir.exists())

    def test_outside_a_session_scope_it_refuses_naming_the_fix(self) -> None:
        code, _, err = self.run_cli(
            "probe",
            "create",
            "oom-kill",
            "--all-sessions",
            "--deadline",
            "1h",
            cgroup=OUTSIDE,
        )
        self.assertEqual(code, 1)
        self.assertIn("outside any claudewheel session scope", err)
        self.assertIn("heavy-1-2.scope", err)
        self.assertIn("Bash tool call", err)
        # The fix: the same command from inside a session scope.
        self.create("--all-sessions", cgroup=SCOPE_S)

    def test_a_scope_the_lifecycle_store_does_not_know_is_refused(self) -> None:
        code, _, err = self.run_cli(
            "probe",
            "create",
            "oom-kill",
            "--all-sessions",
            "--deadline",
            "1h",
            cgroup=cgroup_of(f"claudewheel-session-9999-{LAUNCHED}.scope"),
        )
        self.assertEqual(code, 1)
        self.assertIn("9999", err)
        self.assertIn("relaunch", err)

    def test_dry_run_writes_nothing_and_prints_no_binding(self) -> None:
        code, out, err = self.run_cli(
            "probe",
            "create",
            "oom-kill",
            "--all-sessions",
            "--deadline",
            "1h",
            "--dry-run",
        )
        self.assertEqual(code, 0, err)
        self.assertIsNone(probe.BIND_LINE_RE.search(out))
        self.assertFalse(self.store.probes_dir.exists())


class SubscriptionTests(_CliCase):
    def test_another_session_subscribes_and_unsubscribes(self) -> None:
        probe_id, _ = self.create("--all-sessions")
        code, out, err = self.run_cli("probe", "subscribe", probe_id, cgroup=SCOPE_T)
        self.assertEqual(code, 0, err)
        match = probe.BIND_LINE_RE.search(out)
        assert match is not None
        sub = match.group(1)
        self.assertEqual(
            probe.read_probe(self.store, probe_id).subscriptions[sub].session, T
        )
        # S cannot remove T's subscription.
        code, _, err = self.run_cli("probe", "unsubscribe", sub, cgroup=SCOPE_S)
        self.assertEqual(code, 1)
        self.assertIn(T, err)
        code, _, err = self.run_cli("probe", "unsubscribe", sub, cgroup=SCOPE_T)
        self.assertEqual(code, 0, err)
        self.assertFalse(
            probe.read_probe(self.store, probe_id).subscriptions[sub].active
        )

    def test_an_unknown_or_ended_probe_is_refused(self) -> None:
        code, _, err = self.run_cli("probe", "subscribe", "0000000000000000")
        self.assertEqual(code, 1)
        self.assertIn("no probe", err)
        probe_id, _ = self.create("--all-sessions")
        self.run_cli("probe", "stop", probe_id)
        code, _, err = self.run_cli("probe", "subscribe", probe_id, cgroup=SCOPE_T)
        self.assertEqual(code, 1)
        self.assertIn("ended", err)


class StopTests(_CliCase):
    def test_only_the_creating_session_stops_a_probe(self) -> None:
        probe_id, _ = self.create("--all-sessions")
        code, _, err = self.run_cli("probe", "stop", probe_id, cgroup=SCOPE_T)
        self.assertEqual(code, 1)
        self.assertIn(S, err)
        code, out, err = self.run_cli("probe", "stop", probe_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(probe.read_probe(self.store, probe_id).ended, "stopped")
        code, _, err = self.run_cli("probe", "stop", probe_id)
        self.assertEqual(code, 1)
        self.assertIn("already ended", err)


class ListTests(_CliCase):
    def test_list_runs_outside_a_session_and_shows_everything(self) -> None:
        probe_id, sub = self.create("--session", T)
        probe.append_kill(
            self.store,
            {
                "format_version": 1,
                "id": "cccccccccccccccc",
                "at": lifecycle.now_timestamp(),
                "killed_at_us": LAUNCHED * 1_000_000,
                "unit": "ptyxis-spawn-x.scope",
                "cursor": "c",
                "scope": "other",
                "session": None,
                "command": None,
                "unattributed": "ptyxis-spawn-x.scope is neither a claudewheel session scope nor a heavy scope",
                "reports": [],
                "probes": [],
            },
        )
        probe.write_report(
            self.store,
            probe.Report(
                id="aaaaaaaaaaaaaaaa",
                at=lifecycle.now_timestamp(),
                session=S,
                agent=None,
                task=None,
                probe=probe_id,
                subscription=sub,
                kill="cccccccccccccccc",
                text="t",
            ),
            probe.recipient_of(None, sub, bound=False),
        )
        code, out, err = self.run_cli("probe", "list", cgroup=OUTSIDE)
        self.assertEqual(code, 0, err)
        self.assertIn(f"probe {probe_id} [live]: oom-kill in session {T}", out)
        self.assertIn(
            f"subscription {sub}: session {S}, not bound to a conversation yet", out
        )
        self.assertIn("report aaaaaaaaaaaaaaaa", out)
        self.assertIn("waiting for its subscription to be bound", out)
        self.assertIn("ptyxis-spawn-x.scope", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
