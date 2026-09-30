"""Tests for the probe runner: journal entries in, reports and kill records out.

Every entry is a synthetic one shaped like the entries systemd's user manager
writes (the recorded fields the runner reads: MESSAGE_ID, USER_UNIT,
__REALTIME_TIMESTAMP, __CURSOR). No test causes an OOM kill.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import lifecycle, probe, probe_runner
from claudewheel.workspace import Workspace

S = "4d97ca01-9d56-4f49-8047-77f5160febde"  # the session whose command is killed
T = "0b1e7c52-4f6f-4d7e-9a53-1f2a3b4c5d6e"  # another session
LAUNCHED = 1_790_000_000  # the session scope's launch second
KILLED_US = (LAUNCHED + 600) * 1_000_000
SCOPE = f"claudewheel-session-4242-{LAUNCHED}.scope"
HEAVY = f"heavy-5151-{LAUNCHED + 300}.scope"
AGENT = "a23a40730cf157323"


def entry(
    unit: str, *, at_us: int = KILLED_US, cursor: str = "s=1;i=1"
) -> dict[str, Any]:
    return {
        "MESSAGE_ID": probe_runner.OOM_KILL_MESSAGE_ID,
        "USER_UNIT": unit,
        "__REALTIME_TIMESTAMP": str(at_us),
        "__CURSOR": cursor,
        "MESSAGE": f"{unit}: The kernel OOM killer killed some processes in this unit.",
        "_COMM": "systemd",
    }


class _RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.ws = Workspace.open(self.base / "cw", claude_dir=self.base / "claude")
        self.store = self.ws.probes
        self.descriptions: dict[str, str] = {}

    def describe(self, unit: str) -> str | None:
        return self.descriptions.get(unit)

    def started(
        self,
        session: str,
        pid: int | None,
        at_ms: int,
        transcript: str | None = None,
    ) -> None:
        lifecycle.append_event(
            self.ws.shared.lifecycle_dir,
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
                transcript=transcript,
                pid=pid,
            ),
        )

    def ended(self, session: str) -> None:
        lifecycle.append_event(
            self.ws.shared.lifecycle_dir,
            lifecycle.EndedEvent(
                session=session,
                source="hook",
                outcome="exited",
                reason=None,
                detail=None,
            ),
        )

    def make_probe(
        self,
        probe_id: str,
        *,
        watch: str | None,
        creator: str = T,
        created_ms: int = LAUNCHED * 1000,
        deadline_ms: int = (LAUNCHED + 86400) * 1000,
        **over: Any,
    ) -> None:
        store = self.store
        path = store.probe_file(probe_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        values = {
            "session": creator,
            "probe_kind": "oom-kill",
            "watch_session": watch,
            "deadline": lifecycle.now_timestamp(deadline_ms),
            "until_count": None,
            "until_watched_ends": False,
            "until_file": None,
        }
        values.update(over)
        line = {
            "format_version": 1,
            "id": lifecycle.new_event_id(),
            "at": lifecycle.now_timestamp(created_ms),
            "probe": probe_id,
            "kind": "created",
            **values,
        }
        path.write_text(json.dumps(line) + "\n")

    def subscribe(self, probe_id: str, sub: str, session: str, agent: Any) -> None:
        probe.append_probe_event(
            self.store, probe_id, "subscribed", subscription=sub, session=session
        )
        if agent != probe.UNBOUND:
            probe.append_probe_event(
                self.store, probe_id, "bound", subscription=sub, agent=agent
            )

    def process(self, e: dict[str, Any]) -> dict[str, Any] | None:
        return probe_runner.process_entry(self.ws, e, describe=self.describe)

    def reports(self, state: str = "pending") -> list[probe.ReportFile]:
        return probe.list_reports(self.store, [state])


class AttributionTests(_RunnerCase):
    def test_a_session_scope_kill_is_reported_to_that_session(self) -> None:
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        kill = self.process(entry(SCOPE))
        assert kill is not None
        self.assertEqual((kill["scope"], kill["session"]), ("session", S))
        self.assertIsNone(kill["unattributed"])
        [item] = self.reports()
        self.assertEqual((item.report.session, item.recipient), (S, "main"))
        self.assertIsNone(item.report.probe)
        self.assertIn(probe.OOM_KILL_FIX, item.report.text)
        self.assertIn(f"claudewheel probe report {item.report.id}", item.report.text)
        self.assertEqual(kill["reports"], [item.report.id])
        self.assertTrue(kill["label"].startswith(probe.OOM_KILL_LABEL))
        self.assertEqual(probe.read_kills(self.store), [kill])

    def test_a_tool_scope_kill_is_reported_to_its_session(self) -> None:
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        kill = self.process(entry(f"claudewheel-tool-4242-{LAUNCHED}-5150.scope"))
        assert kill is not None
        self.assertEqual((kill["scope"], kill["session"]), ("tool", S))
        self.assertIsNone(kill["command"])
        self.assertIsNone(kill["unattributed"])
        [item] = self.reports()
        self.assertEqual((item.report.session, item.recipient), (S, "main"))
        self.assertIn("Bash command", item.report.text)
        self.assertEqual(probe.read_kills(self.store), [kill])

    def test_a_tool_scope_the_store_cannot_map_is_unattributed(self) -> None:
        kill = self.process(entry(f"claudewheel-tool-4242-{LAUNCHED}-5150.scope"))
        assert kill is not None
        self.assertEqual((kill["scope"], kill["session"]), ("tool", None))
        self.assertIn("4242", kill["unattributed"])

    def test_a_heavy_kill_reaches_the_session_heavy_named(self) -> None:
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        self.descriptions[HEAVY] = f"heavy job of {SCOPE}: go test ./..."
        kill = self.process(entry(HEAVY))
        assert kill is not None
        self.assertEqual(
            (kill["scope"], kill["session"], kill["command"]),
            ("heavy", S, "go test ./..."),
        )
        [item] = self.reports()
        self.assertIn("`go test ./...`", item.report.text)

    def test_a_heavy_kill_with_no_description_is_recorded_unrouted(self) -> None:
        kill = self.process(entry(HEAVY))
        assert kill is not None
        self.assertIsNone(kill["session"])
        self.assertIn("no description", kill["unattributed"])
        self.assertEqual(kill["reports"], [])
        self.assertEqual(self.reports(), [])
        self.assertEqual(len(probe.read_kills(self.store)), 1)

    def test_a_heavy_started_before_heavy_recorded_its_session(self) -> None:
        """systemd's default description is the whole argv: the reason names the case, not the argv."""
        self.descriptions[HEAVY] = "[systemd-run] /usr/bin/bash -c " + "x" * 5000
        kill = self.process(entry(HEAVY))
        assert kill is not None
        self.assertIsNone(kill["session"])
        self.assertLess(len(kill["unattributed"]), 300)
        self.assertIn("names no claudewheel session", kill["unattributed"])

    def test_heavy_outside_a_session(self) -> None:
        self.descriptions[HEAVY] = "heavy job outside any claudewheel session: make"
        kill = self.process(entry(HEAVY))
        assert kill is not None
        self.assertEqual((kill["session"], kill["command"]), (None, "make"))
        self.assertIn("outside", kill["unattributed"])

    def test_a_session_scope_the_store_cannot_map_is_unattributed(self) -> None:
        kill = self.process(entry(SCOPE))
        assert kill is not None
        self.assertIsNone(kill["session"])
        self.assertIn("4242", kill["unattributed"])

    def test_another_unit_is_unattributed(self) -> None:
        kill = self.process(entry("ptyxis-spawn-eb82c185.scope"))
        assert kill is not None
        self.assertEqual(kill["scope"], "other")
        self.assertIsNone(kill["session"])

    def test_an_entry_read_twice_is_recorded_once(self) -> None:
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        self.assertIsNotNone(self.process(entry(SCOPE)))
        self.assertIsNone(self.process(entry(SCOPE)))
        self.assertEqual(len(probe.read_kills(self.store)), 1)
        self.assertEqual(len(self.reports()), 1)

    def test_a_reprocessed_entry_does_not_requeue_a_delivered_report(self) -> None:
        """A crash after the reports but before the kill line must not deliver twice."""
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        self.process(entry(SCOPE))
        [item] = self.reports()
        probe.move_report(self.store, item, "delivered")
        self.store.kills_file.unlink()
        self.process(entry(SCOPE))
        self.assertEqual(self.reports(), [])
        self.assertEqual(len(self.reports("delivered")), 1)

    def test_other_journal_entries_are_ignored(self) -> None:
        e = entry(SCOPE)
        e["MESSAGE_ID"] = probe_runner.JOB_DONE_MESSAGE_ID
        self.assertIsNone(self.process(e))
        self.assertFalse(self.store.kills_file.exists())


class ProbeRoutingTests(_RunnerCase):
    P = "1111111111111111"

    def setUp(self) -> None:
        super().setUp()
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        self.started(T, 7777, LAUNCHED * 1000 + 1600)

    def test_an_all_sessions_probe_reports_an_unattributed_kill(self) -> None:
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        kill = self.process(entry("ptyxis-spawn-x.scope"))
        assert kill is not None
        self.assertEqual(kill["probes"], [self.P])
        [item] = self.reports()
        self.assertEqual((item.report.session, item.recipient), (T, "main"))
        self.assertEqual(item.report.probe, self.P)
        self.assertIn(f"Probe {self.P}", item.report.text)

    def test_a_session_probe_reports_to_its_subscribers(self) -> None:
        self.make_probe(self.P, watch=S)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        self.subscribe(self.P, "bbbbbbbbbbbbbbbb", S, None)  # S's main: already told
        self.subscribe(self.P, "cccccccccccccccc", S, AGENT)
        self.process(entry(SCOPE))
        got = sorted((r.report.session, r.recipient) for r in self.reports())
        self.assertEqual(got, sorted([(S, "main"), (T, "main"), (S, f"agent-{AGENT}")]))

    def test_a_probe_watching_another_session_does_not_see_this_one(self) -> None:
        self.make_probe(self.P, watch=T)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        kill = self.process(entry(SCOPE))
        assert kill is not None
        self.assertEqual(kill["probes"], [])

    def test_an_unbound_subscription_waits_under_its_own_name(self) -> None:
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, probe.UNBOUND)
        self.process(entry("other.scope"))
        [item] = self.reports()
        self.assertEqual(item.recipient, "unbound-aaaaaaaaaaaaaaaa")

    def test_a_finished_subagents_report_goes_to_its_main_conversation_labeled(
        self,
    ) -> None:
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, AGENT)
        session_file = self.store.session_file(T)
        session_file.parent.mkdir(parents=True)
        session_file.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "at_ms": 1,
                    "kind": "agent-launched",
                    "agent": AGENT,
                    "task": "build the thing",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "format_version": 1,
                    "at_ms": 2,
                    "kind": "agent-finished",
                    "agent": AGENT,
                }
            )
            + "\n"
        )
        self.process(entry("other.scope"))
        [item] = self.reports()
        self.assertEqual(item.recipient, "main")
        self.assertEqual(
            (item.report.agent, item.report.task), (AGENT, "build the thing")
        )

    def test_a_kill_before_the_probe_or_after_its_deadline_is_not_seen(self) -> None:
        self.make_probe(self.P, watch=None, created_ms=KILLED_US // 1000 + 1)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        self.make_probe(
            "2222222222222222", watch=None, deadline_ms=KILLED_US // 1000 - 1
        )
        kill = self.process(entry("other.scope"))
        assert kill is not None
        self.assertEqual(kill["probes"], [])
        self.assertEqual(self.reports(), [])

    def test_an_unsubscribed_subscription_gets_nothing(self) -> None:
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        probe.append_probe_event(
            self.store, self.P, "unsubscribed", subscription="aaaaaaaaaaaaaaaa"
        )
        kill = self.process(entry("other.scope"))
        assert kill is not None
        self.assertEqual((kill["probes"], kill["reports"]), ([self.P], []))


class EndProbeTests(_RunnerCase):
    P = "1111111111111111"

    def reason(self) -> str | None:
        return probe.read_probe(self.store, self.P).ended

    def test_the_deadline_ends_a_probe(self) -> None:
        self.make_probe(self.P, watch=None, deadline_ms=5000)
        probe_runner.end_probes(self.ws, now_ms=4999)
        self.assertIsNone(self.reason())
        probe_runner.end_probes(self.ws, now_ms=5000)
        self.assertEqual(self.reason(), "deadline")

    def test_the_count_ends_a_probe(self) -> None:
        self.make_probe(self.P, watch=None, until_count=2)
        self.process(entry("a.scope", cursor="c1"))
        probe_runner.end_probes(self.ws, now_ms=KILLED_US // 1000)
        self.assertIsNone(self.reason())
        self.process(entry("b.scope", cursor="c2"))
        probe_runner.end_probes(self.ws, now_ms=KILLED_US // 1000)
        self.assertEqual(self.reason(), "count")

    def test_the_file_ends_a_probe(self) -> None:
        flag = self.base / "done"
        self.make_probe(self.P, watch=None, until_file=str(flag))
        probe_runner.end_probes(self.ws, now_ms=LAUNCHED * 1000)
        self.assertIsNone(self.reason())
        flag.write_text("")
        probe_runner.end_probes(self.ws, now_ms=LAUNCHED * 1000)
        self.assertEqual(self.reason(), "file")

    def test_the_watched_session_ending_ends_a_probe(self) -> None:
        self.started(S, 4242, LAUNCHED * 1000 + 1500)
        self.make_probe(self.P, watch=S, until_watched_ends=True)
        probe_runner.end_probes(self.ws, now_ms=LAUNCHED * 1000)
        self.assertIsNone(self.reason())
        self.ended(S)
        probe_runner.end_probes(self.ws, now_ms=LAUNCHED * 1000)
        self.assertEqual(self.reason(), "watched-ended")

    def test_an_ended_probe_is_not_ended_twice(self) -> None:
        self.make_probe(self.P, watch=None, deadline_ms=5000)
        probe_runner.end_probes(self.ws, now_ms=6000)
        self.assertEqual(probe_runner.end_probes(self.ws, now_ms=7000), [])


class SettleTests(_RunnerCase):
    def setUp(self) -> None:
        super().setUp()
        self.transcript = self.base / "t" / f"{S}.jsonl"
        self.transcript.parent.mkdir()
        self.transcript.write_text("{}\n")
        # A live client: this test process stands in for it.
        self.started(S, os.getpid(), LAUNCHED * 1000 + 1500, str(self.transcript))
        self.process(entry(f"claudewheel-session-{os.getpid()}-{LAUNCHED}.scope"))
        [self.item] = self.reports()

    def hand(self) -> probe.ReportFile:
        probe.move_report(self.store, self.item, "handed")
        [item] = self.reports("handed")
        return item

    def test_a_handed_report_found_in_the_transcript_is_delivered(self) -> None:
        self.hand()
        probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(len(self.reports("handed")), 1)
        with self.transcript.open("a") as fh:
            fh.write(json.dumps({"text": self.item.report.text}) + "\n")
        counts = probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(counts["delivered"], 1)
        self.assertEqual(len(self.reports("delivered")), 1)

    def test_a_subagents_transcript_confirms_its_delivery(self) -> None:
        self.hand()
        sub = self.transcript.with_suffix("") / "subagents" / f"agent-{AGENT}.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps({"c": self.item.report.text}) + "\n")
        probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(len(self.reports("delivered")), 1)

    def test_a_handed_report_is_requeued_when_the_session_ends_without_it(self) -> None:
        self.hand()
        self.ended(S)
        counts = probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(counts["requeued"], 1)
        self.assertEqual(len(self.reports("pending")), 1)

    def test_a_handed_report_is_requeued_when_the_transcript_moved_on_without_it(
        self,
    ) -> None:
        item = self.hand()
        past = time.time() - 3600
        os.utime(item.path, (past, past))
        with self.transcript.open("a") as fh:
            fh.write("{}\n")
        counts = probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(counts["requeued"], 1)

    def test_a_sessions_own_report_never_expires(self) -> None:
        self.ended(S)
        probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(len(self.reports("pending")), 1)


class ExpiryTests(_RunnerCase):
    P = "1111111111111111"

    def test_an_ended_probes_report_expires_once_its_session_has_ended(self) -> None:
        self.started(T, os.getpid(), LAUNCHED * 1000 + 1600)
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        self.process(entry("other.scope"))
        probe.append_probe_event(self.store, self.P, "ended", reason="stopped")
        probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(len(self.reports("pending")), 1)  # T still runs
        self.ended(T)
        counts = probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(len(self.reports("expired")), 1)

    def test_a_tick_expires_pending_reports_only_when_asked(self) -> None:
        """Handed reports settle every tick; pending ones are checked for expiry each minute."""
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        self.process(entry("other.scope"))
        probe.append_probe_event(self.store, self.P, "ended", reason="stopped")
        probe_runner.tick(self.ws, expire=False)
        self.assertEqual(len(self.reports("pending")), 1)
        probe_runner.tick(self.ws, expire=True)
        self.assertEqual(len(self.reports("expired")), 1)

    def test_a_live_probes_report_waits_for_its_ended_session(self) -> None:
        self.make_probe(self.P, watch=None)
        self.subscribe(self.P, "aaaaaaaaaaaaaaaa", T, None)
        self.process(entry("other.scope"))
        probe_runner.settle_reports(self.ws, now=time.time())
        self.assertEqual(len(self.reports("pending")), 1)


class JournalTests(unittest.TestCase):
    def test_the_follower_resumes_after_the_saved_cursor(self) -> None:
        argv = probe_runner.journal_argv("s=abc")
        self.assertIn("--after-cursor=s=abc", argv)
        self.assertIn(f"MESSAGE_ID={probe_runner.OOM_KILL_MESSAGE_ID}", argv)
        self.assertIn("--follow", argv)
        fresh = probe_runner.journal_argv(None)
        self.assertEqual(fresh[-2:], ["--lines", "0"])

    def test_the_heavy_description_comes_from_the_started_line(self) -> None:
        message = f"Started {HEAVY} - heavy job of {SCOPE}: go test ./...."
        done = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"MESSAGE": message}) + "\n", stderr=""
        )
        with mock.patch("claudewheel.effects.run", return_value=done) as run:
            self.assertEqual(
                probe_runner.heavy_description(HEAVY),
                f"heavy job of {SCOPE}: go test ./...",
            )
        argv = run.call_args.args[0]
        self.assertIn(f"USER_UNIT={HEAVY}", argv)
        self.assertIn(f"MESSAGE_ID={probe_runner.JOB_DONE_MESSAGE_ID}", argv)


_STUB_JOURNALCTL = """#!/usr/bin/env bash
# Records its arguments, prints the entries in $STUB_ENTRIES, then waits the
# way journalctl --follow does, until it is terminated.
printf '%s\\n' "$*" >> "$STUB_ARGS"
cat "$STUB_ENTRIES"
exec tail -f /dev/null
"""


class ServiceProcessTests(unittest.TestCase):
    """The runner as systemd runs it: a process that stops on SIGTERM and resumes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "cw"
        stub_dir = base / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "journalctl"
        stub.write_text(_STUB_JOURNALCTL)
        stub.chmod(0o755)
        self.entries = base / "entries"
        self.args = base / "args"
        self.env = {
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(base / "home"),
            "CLAUDEWHEEL_CONFIG_DIR": str(self.root),
            "STUB_ENTRIES": str(self.entries),
            "STUB_ARGS": str(self.args),
        }

    def run_until(self, count: int) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "claudewheel.probe_runner"],
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
        )
        kills = Workspace.open(self.root).probes.kills_file
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if kills.exists() and len(kills.read_text().splitlines()) >= count:
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=30)
        return subprocess.CompletedProcess(proc.args, proc.returncode, "", stderr)

    def test_stops_gracefully_and_resumes_after_its_cursor(self) -> None:
        self.entries.write_text(
            json.dumps(entry("a.scope", cursor="c1"))
            + "\n"
            + json.dumps(entry("b.scope", cursor="c2"))
            + "\n"
        )
        first = self.run_until(2)
        self.assertEqual(first.returncode, 0, first.stderr)
        store = Workspace.open(self.root).probes
        self.assertEqual(store.cursor_file.read_text().strip(), "c2")
        self.assertIn("--lines 0", self.args.read_text().splitlines()[0])

        self.entries.write_text(json.dumps(entry("c.scope", cursor="c3")) + "\n")
        second = self.run_until(3)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("--after-cursor=c2", self.args.read_text().splitlines()[1])
        units = [k["unit"] for k in probe.read_kills(store)]
        self.assertEqual(units, ["a.scope", "b.scope", "c.scope"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
