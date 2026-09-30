"""Tests for the probe store: probe logs, kills, reports, scopes, and texts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from claudewheel import lifecycle, probe
from claudewheel.probe import ProbeError, ProbeStore, Report

SESSION = "4d97ca01-9d56-4f49-8047-77f5160febde"
OTHER = "0b1e7c52-4f6f-4d7e-9a53-1f2a3b4c5d6e"
PROBE = "0123456789abcdef"
SUB = "fedcba9876543210"


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.store = ProbeStore(self.base / "probes")
        self.lifecycle_dir = self.base / "lifecycle"

    def create(self, **over: object) -> None:
        values: dict[str, object] = {
            "session": SESSION,
            "probe_kind": "oom-kill",
            "watch_session": OTHER,
            "deadline": "2030-01-01T00:00:00.000Z",
            "until_count": None,
            "until_watched_ends": False,
            "until_file": None,
        }
        values.update(over)
        probe.append_probe_event(self.store, PROBE, "created", **values)

    def started(self, session: str, pid: int | None, at_ms: int) -> None:
        lifecycle.append_event(
            self.lifecycle_dir,
            lifecycle.StartedEvent(
                at=lifecycle.now_timestamp(at_ms),
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


class ProbeLogTests(_StoreCase):
    def test_a_probes_life_reads_back_as_its_state(self) -> None:
        self.create(until_count=3)
        probe.append_probe_event(
            self.store, PROBE, "subscribed", subscription=SUB, session=SESSION
        )
        state = probe.read_probe(self.store, PROBE)
        self.assertEqual(state.kind, "oom-kill")
        self.assertEqual(state.watch_session, OTHER)
        self.assertEqual(state.until_count, 3)
        self.assertTrue(state.active)
        self.assertEqual(state.subscriptions[SUB].agent, probe.UNBOUND)

        probe.append_probe_event(
            self.store, PROBE, "bound", subscription=SUB, agent="a23a40730cf157323"
        )
        self.assertEqual(
            probe.read_probe(self.store, PROBE).subscriptions[SUB].agent,
            "a23a40730cf157323",
        )
        probe.append_probe_event(self.store, PROBE, "unsubscribed", subscription=SUB)
        probe.append_probe_event(self.store, PROBE, "ended", reason="stopped")
        state = probe.read_probe(self.store, PROBE)
        self.assertFalse(state.subscriptions[SUB].active)
        self.assertEqual(state.ended, "stopped")
        self.assertFalse(state.active)

    def test_a_second_binding_does_not_move_a_bound_subscription(self) -> None:
        self.create()
        probe.append_probe_event(
            self.store, PROBE, "subscribed", subscription=SUB, session=SESSION
        )
        probe.append_probe_event(
            self.store, PROBE, "bound", subscription=SUB, agent=None
        )
        probe.append_probe_event(
            self.store, PROBE, "bound", subscription=SUB, agent="x1"
        )
        self.assertIsNone(probe.read_probe(self.store, PROBE).subscriptions[SUB].agent)

    def test_an_unknown_kind_is_refused_before_anything_is_written(self) -> None:
        with self.assertRaises(ProbeError):
            self.create(probe_kind="shell-command")
        self.assertFalse(self.store.probe_file(PROBE).exists())

    def test_an_absent_probe_is_refused(self) -> None:
        with self.assertRaisesRegex(ProbeError, "no probe"):
            probe.read_probe(self.store, PROBE)

    def test_a_line_naming_another_probe_is_refused(self) -> None:
        self.create()
        line = json.dumps(
            {
                "format_version": 1,
                "id": "x",
                "at": "2030-01-01T00:00:00.000Z",
                "probe": "1111111111111111",
                "kind": "ended",
                "reason": "stopped",
            }
        )
        with self.store.probe_file(PROBE).open("a") as fh:
            fh.write(line + "\n")
        with self.assertRaisesRegex(ProbeError, "names probe"):
            probe.read_probe(self.store, PROBE)

    def test_an_interrupted_final_line_is_dropped(self) -> None:
        self.create()
        with self.store.probe_file(PROBE).open("a") as fh:
            fh.write('{"format_version": 1, "id"')
        self.assertTrue(probe.read_probe(self.store, PROBE).active)

    def test_find_subscription(self) -> None:
        self.create()
        probe.append_probe_event(
            self.store, PROBE, "subscribed", subscription=SUB, session=SESSION
        )
        probes = probe.load_probes(self.store)
        state, sub = probe.find_subscription(probes, SUB)
        self.assertEqual((state.id, sub.session), (PROBE, SESSION))
        with self.assertRaises(ProbeError):
            probe.find_subscription(probes, "0000000000000000")

    def test_a_probe_id_is_matched_before_a_path_is_built(self) -> None:
        with self.assertRaises(ProbeError):
            self.store.probe_file("../../etc")


def _report(**over: object) -> Report:
    values: dict[str, object] = {
        "id": "aaaaaaaaaaaaaaaa",
        "at": "2030-01-01T00:00:00.000Z",
        "session": SESSION,
        "agent": None,
        "task": None,
        "probe": None,
        "subscription": None,
        "kill": "bbbbbbbbbbbbbbbb",
        "text": "[claudewheel probe report aaaaaaaaaaaaaaaa] something",
    }
    values.update(over)
    return Report(**values)  # type: ignore[arg-type]


class ReportTests(_StoreCase):
    def test_a_report_is_written_pending_and_moves_by_rename(self) -> None:
        path = probe.write_report(self.store, _report(), "main")
        self.assertEqual(path.name, "aaaaaaaaaaaaaaaa.main.json")
        [item] = probe.list_reports(self.store)
        self.assertEqual((item.state, item.recipient), ("pending", "main"))
        self.assertEqual(item.report, _report())
        moved = probe.move_report(self.store, item, "handed")
        self.assertFalse(path.exists())
        self.assertTrue(moved.exists())
        [item] = probe.list_reports(self.store, ["handed"])
        self.assertEqual(item.state, "handed")

    def test_an_invalid_report_is_refused_unwritten(self) -> None:
        with self.assertRaises(ProbeError):
            probe.write_report(self.store, _report(kill="nope"), "main")
        self.assertEqual(probe.list_reports(self.store), [])

    def test_recipient_names(self) -> None:
        self.assertEqual(probe.recipient_of(None), "main")
        self.assertEqual(probe.recipient_of("a1"), "agent-a1")
        self.assertEqual(probe.recipient_of(None, SUB, bound=False), f"unbound-{SUB}")

    def test_a_stray_file_in_the_reports_is_refused(self) -> None:
        directory = self.store.report_dir("pending", SESSION)
        directory.mkdir(parents=True)
        (directory / "notes.json").write_text("{}")
        with self.assertRaises(ProbeError):
            probe.list_reports(self.store)


class KillTests(_StoreCase):
    def test_kills_round_trip(self) -> None:
        kill = {
            "format_version": 1,
            "id": "cccccccccccccccc",
            "at": "2030-01-01T00:00:00.000Z",
            "killed_at_us": 1790769000000000,
            "unit": "heavy-1-2.scope",
            "cursor": "s=1",
            "scope": "heavy",
            "session": SESSION,
            "command": "go test ./...",
            "unattributed": None,
            "reports": [],
            "probes": [],
            "label": "l",
        }
        probe.append_kill(self.store, kill)
        self.assertEqual(probe.read_kills(self.store), [kill])


class SessionScopeTests(_StoreCase):
    def test_the_session_scope_is_read_from_the_unified_line(self) -> None:
        text = (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "claudewheel-session-1546646-1790714551.scope\n"
        )
        self.assertEqual(
            probe.session_scope_of_cgroup(text),
            "claudewheel-session-1546646-1790714551.scope",
        )
        self.assertIsNone(
            probe.session_scope_of_cgroup("0::/user.slice/heavy-1-2.scope\n")
        )
        self.assertIsNone(probe.session_scope_of_cgroup("1:name=systemd:/x\n"))

    def test_the_newest_session_the_pid_started_since_launch_is_the_one(self) -> None:
        launched = 1_790_000_000
        self.started(OTHER, 4242, launched * 1000 - 5_000)  # an earlier process
        self.started(SESSION, 4242, launched * 1000 + 2_000)
        cleared = "11111111-2222-4333-8444-555555555555"
        self.started(cleared, 4242, launched * 1000 + 60_000)  # /clear later
        scope = f"claudewheel-session-4242-{launched}.scope"
        self.assertEqual(
            probe.session_for_scope(
                self.lifecycle_dir, scope, at_ms=launched * 1000 + 30_000
            ),
            (SESSION, ""),
        )
        self.assertEqual(
            probe.session_for_scope(
                self.lifecycle_dir, scope, at_ms=launched * 1000 + 90_000
            ),
            (cleared, ""),
        )

    def test_no_recorded_session_is_a_reason_not_a_guess(self) -> None:
        self.started(SESSION, None, 1_790_000_001_000)
        session, why = probe.session_for_scope(
            self.lifecycle_dir,
            "claudewheel-session-4242-1790000000.scope",
            at_ms=1_790_000_100_000,
        )
        self.assertIsNone(session)
        self.assertIn("4242", why)


class TextTests(unittest.TestCase):
    KILL = {
        "id": "cccccccccccccccc",
        "killed_at_us": 1790769000000000,
        "unit": "heavy-1-2.scope",
        "scope": "heavy",
        "session": SESSION,
        "command": "go test ./...",
        "unattributed": None,
    }

    def test_own_kill_text_carries_its_id_and_the_shared_fix(self) -> None:
        text = probe.own_kill_text("aaaaaaaaaaaaaaaa", self.KILL)
        self.assertIn("[claudewheel probe report aaaaaaaaaaaaaaaa]", text)
        self.assertIn("`go test ./...`", text)
        self.assertIn(probe.OOM_KILL_FIX, text)

    def test_probe_text_names_the_probe_and_the_session(self) -> None:
        text = probe.probe_kill_text("aaaaaaaaaaaaaaaa", PROBE, self.KILL)
        self.assertIn(f"Probe {PROBE}", text)
        self.assertIn(SESSION, text)

    def test_the_label_leads_with_the_label_and_carries_the_shared_fix(self) -> None:
        text = probe.kill_label(self.KILL)
        self.assertTrue(text.startswith(probe.OOM_KILL_LABEL))
        self.assertIn(probe.OOM_KILL_FIX, text)
        self.assertIn("heavy-1-2.scope", text)


class DurationTests(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(probe.parse_duration("90s"), 90)
        self.assertEqual(probe.parse_duration("2h"), 7200)
        self.assertEqual(probe.parse_duration("7d"), 7 * 86400)
        for bad in ("", "0h", "2", "1.5h", "2w", "-1h"):
            with self.assertRaises(ProbeError):
                probe.parse_duration(bad)


class WakeTests(_StoreCase):
    def test_no_fifo_is_nothing_to_wake(self) -> None:
        probe.wake_waiter(self.store, SESSION)

    def test_a_waiting_fifo_gets_a_byte(self) -> None:
        fifo = self.store.fifo(SESSION)
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        probe.wake_waiter(self.store, SESSION)
        self.assertEqual(os.read(fd, 10), b"x")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
