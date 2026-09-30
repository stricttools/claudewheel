"""The probe delivery, end to end, in real interactive Claude Code sessions.

Every test runs an installed Claude Code (probe.VERIFIED_CLIENT_VERSIONS)
under a pty against a mock API (tests/claude_pty_harness.py), with every
canonical hook claudewheel wires deployed and wired, and a throwaway store. The
reports are queued the way the probe runner queues them; the model's side of
each delivery is read from the requests that reached the mock. It covers:

- the idle wake: a report queued while the session is idle wakes it, labeled
  with the custom rewakeMessage and rewakeSummary, and is then confirmed in
  the transcript;
- print mode: under ``claude -p`` the waiter exits at once instead of holding
  the run for its timeout, and delivers nothing;
- subagent injection: a report for a subagent reaches that subagent through
  additionalContext on its next tool call;
- the 137 labeling: a Bash call that dies with status 137 while an OOM kill is
  recorded in its session is labeled for the calling conversation;
- resume delivery: a report kept for a session that ended is delivered when
  it resumes (SessionStart, source resume).

rewakeMessage and rewakeSummary are internal to Claude Code's settings
schema, so this is also the check, per verified client version, that they
still work: a version that drops them fails the idle-wake test.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from claudewheel import guardrail, lifecycle, probe, probe_runner
from claudewheel.defaults import build_canonical_shared_settings
from claudewheel.hook_scripts import HOOK_SCRIPTS, deploy_scripts
from claudewheel.workspace import Workspace
from tests.claude_pty_harness import ClaudeRun, MockApi, client_binary, print_mode

HERE = Path(__file__).resolve().parent


def setUpModule() -> None:
    for tool in ("bash", "jq", "flock"):
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"{tool} is not on PATH")


def _report(session: str, report_id: str, text: str, **over: Any) -> probe.Report:
    values: dict[str, Any] = {
        "id": report_id,
        "at": lifecycle.now_timestamp(),
        "session": session,
        "agent": None,
        "task": None,
        "probe": None,
        "subscription": None,
        "kill": "cccccccccccccccc",
        "text": f"[claudewheel probe report {report_id}] {text}",
    }
    values.update(over)
    return probe.Report(**values)


class _DeliveryCase(unittest.TestCase):
    version = ""

    def setUp(self) -> None:
        client_binary(self.version)  # an absent version fails here, naming the install
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.root = self.base / "cw"
        self.ws = Workspace.open(self.root)
        self.store = self.ws.probes
        deploy_scripts(sorted(HOOK_SCRIPTS), self.ws.scripts_dir)
        self.settings = {
            "hooks": build_canonical_shared_settings(self.ws.scripts_dir)["hooks"]
        }
        self.env = {"CLAUDEWHEEL_CONFIG_DIR": str(self.root)}
        self.runs: list[ClaudeRun] = []
        self.apis: list[MockApi] = []
        self.addCleanup(self._stop_all)

    def _stop_all(self) -> None:
        for run in self.runs:
            run.stop()
        for api in self.apis:
            api.stop()

    def api(self, rules: list[dict[str, Any]]) -> MockApi:
        api = MockApi(self.base, rules)
        self.apis.append(api)
        return api

    def claude(self, api: MockApi, *args: str) -> ClaudeRun:
        run = ClaudeRun(
            self.base, self.version, self.settings, api, self.env, list(args)
        )
        self.runs.append(run)
        return run

    def session(self, timeout: float = 30) -> lifecycle.StartedEvent:
        """The session the hook-session-start hook recorded."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in self.ws.shared.lifecycle_dir.glob("*.jsonl"):
                for event in lifecycle.read_session(path):
                    if isinstance(event, lifecycle.StartedEvent):
                        return event
            time.sleep(0.1)
        raise AssertionError("no session was recorded as started")

    def idle(self, api: MockApi, count: int, timeout: float = 30) -> None:
        """Wait until *count* requests were answered and the session is idle again."""
        api.wait_for(
            lambda _r: len(api.requests()) >= count, timeout, f"number {count}"
        )
        time.sleep(1.5)


class IdleWakeTests(_DeliveryCase):
    def test_a_report_wakes_an_idle_session_with_the_custom_rewake_texts(self) -> None:
        api = self.api([])
        self.claude(api, "go")
        started = self.session()
        self.idle(api, 1)
        probe.write_report(
            self.store,
            _report(started.session, "aaaaaaaaaaaaaaaa", "IDLE-WAKE-REPORT"),
            "main",
        )
        queued = time.monotonic()
        probe.wake_waiter(self.store, started.session)
        woke = api.wait_for(
            lambda r: "IDLE-WAKE-REPORT" in r["last"], 20, "carrying the report"
        )
        self.assertLess(time.monotonic() - queued, 10)
        self.assertIn(
            f"{guardrail.REWAKE_MESSAGE} [claudewheel probe report aaaaaaaaaaaaaaaa]",
            woke["last"],
        )
        self.assertIn(f"<summary>{guardrail.REWAKE_SUMMARY}</summary>", woke["last"])
        # The delivery is confirmed in the session's transcript.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            probe_runner.settle_reports(self.ws, now=time.time())
            if probe.list_reports(self.store, ["delivered"]):
                break
            time.sleep(0.5)
        self.assertEqual(len(probe.list_reports(self.store, ["delivered"])), 1)

    def test_reports_queued_together_arrive_in_one_delivery(self) -> None:
        api = self.api([])
        self.claude(api, "go")
        started = self.session()
        self.idle(api, 1)
        for rid, text in (
            ("aaaaaaaaaaaaaaaa", "BATCH-ONE"),
            ("bbbbbbbbbbbbbbbb", "BATCH-TWO"),
        ):
            probe.write_report(self.store, _report(started.session, rid, text), "main")
        probe.wake_waiter(self.store, started.session)
        woke = api.wait_for(
            lambda r: "BATCH-ONE" in r["last"], 20, "carrying the reports"
        )
        self.assertIn("BATCH-TWO", woke["last"])


class PrintModeTests(_DeliveryCase):
    def test_the_waiter_exits_at_once_under_print_mode(self) -> None:
        """An asyncRewake hook holds `claude -p` for its whole timeout (a week here)."""
        api = self.api([])
        start = time.monotonic()
        done = print_mode(
            self.base, self.version, self.settings, api, self.env, "go", timeout=120
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(time.monotonic() - start, 60)
        # Nothing was handed over, and no waiter was left waiting.
        self.assertEqual(probe.list_reports(self.store, ["handed"]), [])
        waiters = self.store.waiters_dir
        self.assertFalse(waiters.exists() and any(waiters.glob("*.fifo")))


class SubagentTests(_DeliveryCase):
    def test_a_subagent_gets_its_report_through_its_own_tool_call(self) -> None:
        pending = self.store.reports_dir / "pending"
        wait_for_report = (
            f'{sys.executable} -c "import glob, time\n'
            f"end = time.time() + 30\n"
            f"while time.time() < end and not glob.glob('{pending}/*/*.agent-*.json'):\n"
            f"    time.sleep(0.1)\n"
            f"print('sub-wait-done')\""
        )
        api = self.api(
            [
                {
                    "contains": "startprobe",
                    "tool": "Agent",
                    "input": {
                        "description": "SUBTASK-DESC",
                        "prompt": "SUBTASK-XYZ",
                        "subagent_type": "general-purpose",
                    },
                },
                {
                    "contains": "SUBTASK-XYZ",
                    "tool": "Bash",
                    "input": {"command": wait_for_report, "description": "wait"},
                },
                {"contains": "sub-wait-done", "text": "subdone"},
            ]
        )
        self.claude(api, "startprobe")
        started = self.session()
        deadline = time.monotonic() + 30
        agents: dict[str, probe.AgentInfo] = {}
        while time.monotonic() < deadline and not agents:
            agents = probe.read_session_agents(self.store, started.session)
            time.sleep(0.1)
        self.assertTrue(agents, "the Agent call's launch was not recorded")
        [(agent, info)] = agents.items()
        self.assertEqual(info.task, "SUBTASK-DESC")
        probe.write_report(
            self.store,
            _report(
                started.session, "aaaaaaaaaaaaaaaa", "SUBAGENT-REPORT", agent=agent
            ),
            probe.recipient_of(agent),
        )
        got = api.wait_for(
            lambda r: "sub-wait-done" in r["last"], 45, "after the subagent's call"
        )
        self.assertIn("SUBAGENT-REPORT", got["last"])
        self.assertEqual(len(probe.list_reports(self.store, ["handed"])), 1)


class OomLabelTests(_DeliveryCase):
    def test_a_137_during_a_recorded_kill_is_labeled_for_the_calling_conversation(
        self,
    ) -> None:
        pid_file = self.base / "claude.pid"
        feeder = HERE / "feed_synthetic_oom_kill.py"
        command = f"{sys.executable} {feeder} {self.root} $(cat {pid_file}) && bash -c 'kill -9 $$'"
        api = self.api(
            [
                {
                    "contains": "killprobe",
                    "tool": "Bash",
                    "input": {"command": command, "description": "die"},
                }
            ]
        )
        run = self.claude(api, "killprobe")
        pid_file.write_text(str(run.pid))
        started = self.session()
        self.assertEqual(started.pid, run.pid)
        labeled = api.wait_for(
            lambda r: probe.OOM_KILL_LABEL in r["last"], 45, "carrying the label"
        )
        self.assertIn("Exit code 137", labeled["last"])
        self.assertIn(probe.OOM_KILL_FIX, labeled["last"])
        # The session's own report of the kill reaches its main conversation too.
        api.wait_for(
            lambda r: "A command this session started was OOM-killed" in r["last"],
            30,
            "carrying the report",
        )
        [kill] = probe.read_kills(self.store)
        self.assertEqual(kill["session"], started.session)


class ResumeTests(_DeliveryCase):
    def test_a_report_kept_for_an_ended_session_is_delivered_on_resume(self) -> None:
        api = self.api([])
        first = self.claude(api, "go")
        started = self.session()
        self.idle(api, 1)
        first.stop()
        probe.write_report(
            self.store,
            _report(started.session, "aaaaaaaaaaaaaaaa", "RESUME-REPORT"),
            "main",
        )
        self.claude(api, "--resume", started.session)
        got = api.wait_for(
            lambda r: "RESUME-REPORT" in r["last"], 45, "carrying the kept report"
        )
        self.assertIn(guardrail.REWAKE_MESSAGE, got["last"])
        entries = [
            e.entry
            for e in lifecycle.read_session(
                self.ws.shared.lifecycle_dir / f"{started.session}.jsonl"
            )
            if isinstance(e, lifecycle.StartedEvent)
        ]
        self.assertIn("resume", entries)


# One class of each kind per verified client version.
for _version in probe.VERIFIED_CLIENT_VERSIONS:
    for _base in (
        IdleWakeTests,
        PrintModeTests,
        SubagentTests,
        OomLabelTests,
        ResumeTests,
    ):
        _name = f"{_base.__name__}_{_version.replace('.', '_')}"
        globals()[_name] = type(_name, (_base,), {"version": _version})
del IdleWakeTests, PrintModeTests, SubagentTests, OomLabelTests, ResumeTests
del _base, _name, _version


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
