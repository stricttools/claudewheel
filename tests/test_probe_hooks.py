"""Execution tests for the two probe hook scripts, and the exit-2 exception.

Each script from HOOK_SCRIPTS is written to disk and run under bash with a
payload in Claude Code's own shape on stdin, a throwaway store, and the
environment Claude Code gives its hooks (CLAUDE_PID, and the two variables
that say whether the session is interactive). What the scripts write is read
back through claudewheel.probe, so every line runs through the strictspec
validators.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from claudewheel import guardrail, lifecycle, probe
from claudewheel.hook_scripts import EXIT_2_HOOKS, HOOK_SCRIPTS, PATH_COMMANDS
from claudewheel.probe import ProbeStore, Report

S = "4d97ca01-9d56-4f49-8047-77f5160febde"
AGENT = "a23a40730cf157323"
WAIT = "hook-wait-for-probe-reports"
DELIVER = "hook-deliver-probe-reports"


def setUpModule() -> None:
    for tool in ("bash", "jq", "flock"):
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"{tool} is not on PATH")


def report(report_id: str, text: str, **over: Any) -> Report:
    values: dict[str, Any] = {
        "id": report_id,
        "at": lifecycle.now_timestamp(),
        "session": S,
        "agent": None,
        "task": None,
        "probe": None,
        "subscription": None,
        "kill": "cccccccccccccccc",
        "text": text,
    }
    values.update(over)
    return Report(**values)


class _HookCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "cw"
        self.store = ProbeStore(self.root / "shared" / "probes")
        self.children: list[subprocess.Popen[Any]] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)

    def script(self, name: str) -> Path:
        path = self.base / name
        if not path.exists():
            path.write_text(HOOK_SCRIPTS[name])
        return path

    def client(self) -> subprocess.Popen[Any]:
        """A process standing in for the Claude Code client."""
        child = subprocess.Popen(
            [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"]
        )
        self.children.append(child)
        return child

    def env(self, client: int, *, interactive: bool = True) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base / "home"),
            "CLAUDEWHEEL_CONFIG_DIR": str(self.root),
            "CLAUDE_PID": str(client),
            "CLAUDE_CODE_SESSION_ATTENDED": "1" if interactive else "0",
            "CLAUDE_CODE_ENTRYPOINT": "cli" if interactive else "sdk-cli",
        }

    def run_hook(
        self, name: str, payload: dict[str, Any] | str, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["bash", str(self.script(name))],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )

    def start_hook(
        self, name: str, payload: dict[str, Any], env: dict[str, str]
    ) -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            ["bash", str(self.script(name))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        self.children.append(proc)
        return proc

    def wait_exit(self, proc: subprocess.Popen[str], seconds: float) -> int | None:
        try:
            return proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None

    def queue(self, rep: Report, recipient: str = "main") -> Path:
        return probe.write_report(self.store, rep, recipient)

    def stop_payload(self) -> dict[str, Any]:
        return {"session_id": S, "hook_event_name": "Stop", "stop_hook_active": False}


class WaiterTests(_HookCase):
    def test_a_non_interactive_session_gets_no_waiter(self) -> None:
        """Under claude -p an asyncRewake hook would hold the run for its timeout."""
        self.queue(report("aaaaaaaaaaaaaaaa", "r1"))
        client = self.client()
        start = time.monotonic()
        proc = self.run_hook(
            WAIT, self.stop_payload(), self.env(client.pid, interactive=False)
        )
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(len(probe.list_reports(self.store, ["pending"])), 1)

    def test_pending_reports_are_handed_over_by_exiting_2(self) -> None:
        self.queue(report("aaaaaaaaaaaaaaaa", "first report"))
        self.queue(report("bbbbbbbbbbbbbbbb", "second report"))
        client = self.client()
        proc = self.run_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("first report", proc.stderr)
        self.assertIn("second report", proc.stderr)
        self.assertEqual(probe.list_reports(self.store, ["pending"]), [])
        self.assertEqual(len(probe.list_reports(self.store, ["handed"])), 2)

    def test_a_finished_subagents_report_is_labeled_with_the_agent_and_task(
        self,
    ) -> None:
        self.queue(report("aaaaaaaaaaaaaaaa", "the kill", agent=AGENT, task="build it"))
        client = self.client()
        proc = self.run_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(
            f"For subagent {AGENT} (task: build it), which has finished: the kill",
            proc.stderr,
        )

    def test_a_subagents_own_report_is_not_the_waiters(self) -> None:
        self.queue(
            report("aaaaaaaaaaaaaaaa", "for the agent", agent=AGENT), f"agent-{AGENT}"
        )
        client = self.client()
        proc = self.start_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertIsNone(self.wait_exit(proc, 1.5))

    def test_an_idle_waiter_wakes_when_a_report_arrives(self) -> None:
        client = self.client()
        proc = self.start_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertIsNone(self.wait_exit(proc, 1.0))
        self.queue(report("aaaaaaaaaaaaaaaa", "late report"))
        probe.wake_waiter(self.store, S)
        start = time.monotonic()
        self.assertEqual(self.wait_exit(proc, 5), 2)
        self.assertLess(time.monotonic() - start, 2.5)
        assert proc.stderr is not None
        self.assertIn("late report", proc.stderr.read())

    def test_the_waiter_exits_when_its_client_is_gone(self) -> None:
        client = self.client()
        proc = self.start_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertIsNone(self.wait_exit(proc, 1.0))
        client.kill()
        client.wait()
        self.assertEqual(self.wait_exit(proc, 10), 0)

    def test_one_waiter_per_session(self) -> None:
        client = self.client()
        first = self.start_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertIsNone(self.wait_exit(first, 1.0))
        second = self.run_hook(WAIT, self.stop_payload(), self.env(client.pid))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIsNone(first.poll())

    def test_a_new_client_takes_over_from_the_waiter_of_a_dead_one(self) -> None:
        old = self.client()
        stale = self.start_hook(WAIT, self.stop_payload(), self.env(old.pid))
        self.assertIsNone(self.wait_exit(stale, 1.0))
        new = self.client()
        fresh = self.start_hook(WAIT, self.stop_payload(), self.env(new.pid))
        self.assertIsNone(self.wait_exit(fresh, 1.0))
        old.kill()
        old.wait()
        self.assertEqual(self.wait_exit(stale, 10), 0)
        self.queue(report("aaaaaaaaaaaaaaaa", "after the takeover"))
        probe.wake_waiter(self.store, S)
        self.assertEqual(self.wait_exit(fresh, 15), 2)

    def test_failures_exit_1(self) -> None:
        client = self.client()
        self.assertEqual(
            self.run_hook(WAIT, "not json", self.env(client.pid)).returncode, 1
        )
        env = self.env(client.pid)
        env["CLAUDE_PID"] = "x"
        self.assertEqual(self.run_hook(WAIT, self.stop_payload(), env).returncode, 1)


class DeliverTests(_HookCase):
    def setUp(self) -> None:
        super().setUp()
        self.env_ = self.env(os.getpid())

    def payload(self, event: str, tool: str = "Bash", **over: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session_id": S,
            "hook_event_name": event,
            "tool_name": tool,
            "tool_use_id": "toolu_1",
            "tool_input": {"command": "true"},
        }
        data.update(over)
        return data

    def deliver(self, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        proc = self.run_hook(DELIVER, payload, self.env_)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return proc

    def context(self, proc: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        text: str = data["hookSpecificOutput"]["additionalContext"]
        return text

    def make_probe(
        self, probe_id: str = "1111111111111111", sub: str = "2222222222222222"
    ) -> None:
        probe.append_probe_event(
            self.store,
            probe_id,
            "created",
            session=S,
            probe_kind="oom-kill",
            watch_session=None,
            deadline="2099-01-01T00:00:00.000Z",
            until_count=None,
            until_watched_ends=False,
            until_file=None,
        )
        probe.append_probe_event(
            self.store, probe_id, "subscribed", subscription=sub, session=S
        )

    def test_bash_calls_are_recorded(self) -> None:
        proc = self.deliver(
            self.payload("PreToolUse", tool_input={"command": "x", "timeout": 30000})
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.deliver(self.payload("PostToolUse", tool_response={"stdout": ""}))
        events = probe.read_session_events(self.store, S)
        self.assertEqual([e["kind"] for e in events], ["call-started", "call-ended"])
        self.assertEqual(events[0]["timeout_ms"], 30000)
        self.assertIsNone(events[0]["agent"])

    def test_a_subscription_is_bound_to_the_conversation_that_created_it(self) -> None:
        self.make_probe()
        self.queue(
            report(
                "aaaaaaaaaaaaaaaa",
                "early",
                probe="1111111111111111",
                subscription="2222222222222222",
            ),
            "unbound-2222222222222222",
        )
        line = probe.BIND_LINE.format(
            subscription="2222222222222222", probe="1111111111111111"
        )
        self.deliver(
            self.payload(
                "PostToolUse",
                agent_id=AGENT,
                tool_response={"stdout": f"probe x\n{line}\n"},
            )
        )
        state = probe.read_probe(self.store, "1111111111111111")
        self.assertEqual(state.subscriptions["2222222222222222"].agent, AGENT)
        [item] = probe.list_reports(self.store, ["pending", "handed"])
        self.assertEqual(item.recipient, f"agent-{AGENT}")
        self.assertEqual(item.report.agent, AGENT)

    def test_the_main_conversation_binds_with_no_agent(self) -> None:
        self.make_probe()
        line = probe.BIND_LINE.format(
            subscription="2222222222222222", probe="1111111111111111"
        )
        self.deliver(self.payload("PostToolUse", tool_response={"stdout": line}))
        state = probe.read_probe(self.store, "1111111111111111")
        self.assertIsNone(state.subscriptions["2222222222222222"].agent)

    def test_another_sessions_subscription_is_never_bound(self) -> None:
        self.make_probe()
        line = probe.BIND_LINE.format(
            subscription="2222222222222222", probe="1111111111111111"
        )
        other = "0b1e7c52-4f6f-4d7e-9a53-1f2a3b4c5d6e"
        self.deliver(
            self.payload(
                "PostToolUse", session_id=other, tool_response={"stdout": line}
            )
        )
        state = probe.read_probe(self.store, "1111111111111111")
        self.assertEqual(state.subscriptions["2222222222222222"].agent, probe.UNBOUND)

    def test_an_agent_launch_is_recorded_with_its_task(self) -> None:
        self.deliver(
            self.payload(
                "PostToolUse",
                tool="Agent",
                tool_input={"description": "sub task", "prompt": "p"},
                tool_response={"agentId": AGENT, "status": "async_launched"},
            )
        )
        agents = probe.read_session_agents(self.store, S)
        self.assertEqual(
            (agents[AGENT].task, agents[AGENT].finished), ("sub task", False)
        )

    def test_a_subagent_gets_its_reports_on_any_tool_event(self) -> None:
        self.queue(report("aaaaaaaaaaaaaaaa", "for you", agent=AGENT), f"agent-{AGENT}")
        proc = self.deliver(self.payload("PostToolUse", tool="Read", agent_id=AGENT))
        self.assertEqual(self.context(proc), "for you")
        self.assertEqual(len(probe.list_reports(self.store, ["handed"])), 1)
        # Taken once: the next event has nothing to hand over.
        again = self.deliver(
            self.payload("PostToolUseFailure", tool="Read", agent_id=AGENT)
        )
        self.assertEqual(again.stdout, "")

    def test_the_main_conversations_reports_are_left_to_the_waiter(self) -> None:
        self.queue(report("aaaaaaaaaaaaaaaa", "main's"))
        proc = self.deliver(self.payload("PostToolUse", tool="Read"))
        self.assertEqual(proc.stdout, "")
        self.assertEqual(len(probe.list_reports(self.store, ["pending"])), 1)

    def kill(self, at_us: int, label: str) -> None:
        probe.append_kill(
            self.store,
            {
                "format_version": 1,
                "id": probe.new_id(),
                "at": lifecycle.now_timestamp(),
                "killed_at_us": at_us,
                "unit": "heavy-1-2.scope",
                "cursor": probe.new_id(),
                "scope": "heavy",
                "session": S,
                "command": "go test",
                "unattributed": None,
                "reports": [],
                "probes": [],
                "label": label,
            },
        )

    def test_a_137_during_a_kill_is_labeled(self) -> None:
        now_us = time.time_ns() // 1000
        self.kill(now_us - 500_000, "LABEL-ONE")
        proc = self.deliver(
            self.payload(
                "PostToolUseFailure", error="Exit code 137\nKilled", duration_ms=2000
            )
        )
        self.assertEqual(self.context(proc), "LABEL-ONE")

    def test_a_kill_recorded_just_after_the_call_returned_still_labels_it(self) -> None:
        """The runner reads the kill from the journal a moment after it happens."""
        proc = self.start_hook(
            DELIVER,
            self.payload("PostToolUseFailure", error="Exit code 137", duration_ms=500),
            self.env_,
        )
        self.assertIsNone(self.wait_exit(proc, 0.6))
        self.kill(time.time_ns() // 1000, "LATE-LABEL")
        self.assertEqual(self.wait_exit(proc, 10), 0)
        assert proc.stdout is not None
        data = json.loads(proc.stdout.read())
        self.assertEqual(data["hookSpecificOutput"]["additionalContext"], "LATE-LABEL")

    def test_overlapping_calls_are_named(self) -> None:
        self.deliver(
            self.payload("PreToolUse", tool_use_id="toolu_other", agent_id=AGENT)
        )
        self.deliver(self.payload("PreToolUse"))
        self.kill(time.time_ns() // 1000, "LABEL-ONE")
        proc = self.deliver(
            self.payload("PostToolUseFailure", error="Exit code 137", duration_ms=2000)
        )
        text = self.context(proc)
        self.assertTrue(text.startswith("LABEL-ONE Other Bash calls"), text)
        self.assertIn(f"toolu_other (subagent {AGENT})", text)
        self.assertNotIn("toolu_1 (", text)

    def test_a_137_with_no_kill_is_not_labeled(self) -> None:
        self.kill(1_000_000, "LONG-AGO")
        proc = self.deliver(
            self.payload("PostToolUseFailure", error="Exit code 137", duration_ms=100)
        )
        self.assertEqual(proc.stdout, "")

    def test_another_failure_is_not_labeled(self) -> None:
        self.kill(time.time_ns() // 1000, "LABEL")
        proc = self.deliver(self.payload("PostToolUseFailure", error="Exit code 1"))
        self.assertEqual(proc.stdout, "")

    def test_a_subagents_label_and_reports_come_together(self) -> None:
        self.kill(time.time_ns() // 1000, "LABEL")
        self.queue(report("aaaaaaaaaaaaaaaa", "queued", agent=AGENT), f"agent-{AGENT}")
        proc = self.deliver(
            self.payload(
                "PostToolUseFailure",
                agent_id=AGENT,
                error="Exit code 137",
                duration_ms=500,
            )
        )
        self.assertEqual(self.context(proc), "LABEL\n\nqueued")

    def test_a_finished_subagents_reports_go_to_the_main_conversation(self) -> None:
        self.queue(report("aaaaaaaaaaaaaaaa", "x", agent=AGENT), f"agent-{AGENT}")
        fifo = self.store.fifo(S)
        fifo.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        proc = self.deliver(
            {"session_id": S, "hook_event_name": "SubagentStop", "agent_id": AGENT}
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        [item] = probe.list_reports(self.store, ["pending"])
        self.assertEqual(item.recipient, "main")
        self.assertTrue(probe.read_session_agents(self.store, S)[AGENT].finished)
        self.assertEqual(os.read(fd, 10), b"x")

    def test_failures_exit_1(self) -> None:
        self.assertEqual(self.run_hook(DELIVER, "not json", self.env_).returncode, 1)
        self.assertEqual(
            self.run_hook(
                DELIVER, {"session_id": "nope", "hook_event_name": "Stop"}, self.env_
            ).returncode,
            1,
        )


class ExitTwoExceptionTests(unittest.TestCase):
    """Hooks never block a session (exit 1, never 2), with one written exception."""

    def hooks(self) -> list[str]:
        return [name for name in HOOK_SCRIPTS if name not in PATH_COMMANDS]

    def test_the_exception_is_named_with_its_reason(self) -> None:
        self.assertEqual(set(EXIT_2_HOOKS), {WAIT})
        for reason in EXIT_2_HOOKS.values():
            self.assertTrue(reason.strip())

    def test_only_the_named_hook_can_exit_2(self) -> None:
        for name in self.hooks():
            exits_2 = re.search(r"\bexit 2\b", HOOK_SCRIPTS[name]) is not None
            with self.subTest(hook=name):
                self.assertEqual(exits_2, name in EXIT_2_HOOKS)

    def test_the_exception_is_an_async_rewake_hook_wherever_it_is_wired(self) -> None:
        for wiring in guardrail.EXPECTED_HOOK_WIRINGS + guardrail.PROBE_HOOK_WIRINGS:
            if wiring.script in EXIT_2_HOOKS:
                self.assertTrue(wiring.options.async_rewake, wiring)

    def test_no_hook_exits_2_on_a_broken_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in self.hooks():
                path = Path(tmp) / name
                path.write_text(HOOK_SCRIPTS[name])
                for payload in ("", "not json", '{"session_id": "x"}'):
                    proc = subprocess.run(
                        ["bash", str(path)],
                        input=payload,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        env={
                            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HOME": tmp,
                            "CLAUDE_PID": str(os.getpid()),
                            "CLAUDE_CODE_SESSION_ATTENDED": "1",
                            "CLAUDE_CODE_ENTRYPOINT": "cli",
                        },
                        check=False,
                    )
                    with self.subTest(hook=name, payload=payload):
                        self.assertNotEqual(proc.returncode, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
