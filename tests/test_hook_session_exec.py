"""End-to-end execution tests for the two session lifecycle hook scripts.

Same exec-harness style as test_hook_advise_exec.py: the script from
HOOK_SCRIPTS is written to disk and run under bash with a real Claude Code
SessionStart/SessionEnd payload on stdin, a temporary HOME, and the launch
environment claudewheel would have set.

What the scripts write is read back with ``lifecycle.read_session``, so every
assertion below runs through the real strictspec validator: a line the bash
built wrong is a LifecycleError here, not a silently-tolerated document.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claudewheel.hook_scripts import HOOK_SCRIPTS, deploy_scripts
from claudewheel.lifecycle import EndedEvent, NamedEvent, StartedEvent, read_session
from tests.wheelhelpers import write_session_record

# The session uuid wheelhelpers.write_session_record records, so a registry
# file written by it is a registry file FOR this session.
SESSION = "4d97ca01-9d56-4f49-8047-77f5160febde"

START = "hook-session-start"
END = "hook-session-end"


def setUpModule() -> None:
    """Both scripts are bash + jq; without either there is nothing to exercise."""
    for tool in ("bash", "jq"):
        if shutil.which(tool) is None:
            raise unittest.SkipTest(f"{tool} is not on PATH")


def start_payload(source: str = "startup", **over: Any) -> dict[str, Any]:
    """A SessionStart payload in Claude Code's own shape."""
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "transcript_path": "/transcripts/projects/session.jsonl",
        "cwd": "/home/m/Projects/claudewheel",
        "hook_event_name": "SessionStart",
        "source": source,
    }
    payload.update(over)
    return payload


def end_payload(reason: str = "prompt_input_exit", **over: Any) -> dict[str, Any]:
    """A SessionEnd payload in Claude Code's own shape."""
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "transcript_path": "/transcripts/projects/session.jsonl",
        "cwd": "/home/m/Projects/claudewheel",
        "hook_event_name": "SessionEnd",
        "reason": reason,
    }
    payload.update(over)
    return payload


class _SessionHookCase(unittest.TestCase):
    """Temp HOME, temp lifecycle dir, temp Claude Code config dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config_dir = self.root / "claude-config"
        self.lifecycle_dir = self.root / "lifecycle"

    # --- fixtures --------------------------------------------------------

    def write_registry(
        self,
        *,
        pid: int = 424242,
        name: str | None = "projects-9a",
        name_source: str = "derived",
        version: str = "2.1.263",
    ) -> None:
        """Write one Claude Code registry record for SESSION under config_dir."""
        write_session_record(
            self.config_dir / "sessions",
            pid,
            proc_start="654274470",
            name=name,
            extra={"version": version, "nameSource": name_source},
        )

    def launch_env(self) -> dict[str, str]:
        """The five variables claudewheel puts in a launched session's env."""
        return {
            "CLAUDEWHEEL_LAUNCH_PROFILE": "emergency",
            "CLAUDEWHEEL_LAUNCH_VERSION": "2.1.100",
            "CLAUDEWHEEL_LAUNCH_MODEL": "claude-opus-5",
            "CLAUDEWHEEL_LAUNCH_PERMISSIONS": "skip",
            "CLAUDEWHEEL_LIFECYCLE_DIR": str(self.lifecycle_dir),
        }

    # --- running ---------------------------------------------------------

    def run_hook(
        self,
        name: str,
        payload: dict[str, Any] | str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run hook script *name* with *payload* on stdin under a clean env."""
        script = self.root / name
        script.write_text(HOOK_SCRIPTS[name])
        full_env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        full_env.update(env or {})
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["bash", str(script)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            env=full_env,
            check=False,
        )

    def assert_clean(self, proc: subprocess.CompletedProcess[str]) -> None:
        """The normal path: nothing on stdout, nothing on stderr, exit 0."""
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def events(self, lifecycle_dir: Path | None = None) -> list[Any]:
        """Read SESSION's lifecycle file back through the real validator."""
        directory = self.lifecycle_dir if lifecycle_dir is None else lifecycle_dir
        return read_session(directory / f"{SESSION}.jsonl")


class SessionStartHookTests(_SessionHookCase):
    """hook-session-start: the `started` line, and the `named` line with it."""

    def test_records_launch_env_and_registry_facts(self) -> None:
        self.write_registry()
        proc = self.run_hook(
            START,
            start_payload(),
            {**self.launch_env(), "CLAUDE_CONFIG_DIR": str(self.config_dir)},
        )
        self.assert_clean(proc)

        events = self.events()
        self.assertEqual(len(events), 2, events)
        started, named = events
        assert isinstance(started, StartedEvent)
        self.assertEqual(started.session, SESSION)
        self.assertEqual(started.source, "hook")
        self.assertEqual(started.cwd, "/home/m/Projects/claudewheel")
        self.assertEqual(started.config_dir, str(self.config_dir))
        self.assertEqual(started.profile, "emergency")
        self.assertEqual(started.claude_version, "2.1.100")
        self.assertEqual(started.model, "claude-opus-5")
        self.assertEqual(started.permissions, "skip")
        self.assertEqual(started.entry, "startup")
        self.assertEqual(started.transcript, "/transcripts/projects/session.jsonl")
        self.assertEqual(started.pid, 424242)

        assert isinstance(named, NamedEvent)
        self.assertEqual(named.session, SESSION)
        self.assertEqual(named.source, "hook")
        self.assertEqual(named.name, "projects-9a")
        self.assertEqual(named.name_source, "derived")

    def test_without_launch_env_falls_back_to_claudewheel_config_dir(self) -> None:
        """No CLAUDEWHEEL_LAUNCH_* at all: the store is found under the root."""
        self.write_registry(version="2.1.263")
        cw_root = self.root / "cw-root"
        proc = self.run_hook(
            START,
            start_payload(model="claude-sonnet-4-6"),
            {
                "CLAUDEWHEEL_CONFIG_DIR": str(cw_root),
                "CLAUDE_CONFIG_DIR": str(self.config_dir),
            },
        )
        self.assert_clean(proc)

        events = self.events(cw_root / "shared" / "lifecycle")
        started = events[0]
        assert isinstance(started, StartedEvent)
        self.assertIsNone(started.profile)
        self.assertIsNone(started.permissions)
        self.assertEqual(started.claude_version, "2.1.263")
        self.assertEqual(started.model, "claude-sonnet-4-6")

    def test_without_any_root_falls_back_to_home(self) -> None:
        proc = self.run_hook(START, start_payload())
        self.assert_clean(proc)

        events = self.events(self.home / ".claudewheel" / "shared" / "lifecycle")
        self.assertEqual(len(events), 1, events)
        assert isinstance(events[0], StartedEvent)

    def test_entry_carries_claude_codes_own_source(self) -> None:
        for source in ("startup", "resume", "clear", "fork"):
            with self.subTest(source=source):
                self.setUp()  # a fresh store per source
                proc = self.run_hook(START, start_payload(source), self.launch_env())
                self.assert_clean(proc)
                started = self.events()[0]
                assert isinstance(started, StartedEvent)
                self.assertEqual(started.entry, source)

    def test_compact_writes_nothing(self) -> None:
        """A compaction is not a process start, so nothing is recorded."""
        proc = self.run_hook(START, start_payload("compact"), self.launch_env())
        self.assert_clean(proc)
        self.assertEqual(self.events(), [])
        self.assertFalse((self.lifecycle_dir / f"{SESSION}.jsonl").exists())

    def test_unrecognized_source_fails_without_writing(self) -> None:
        proc = self.run_hook(START, start_payload("teleport"), self.launch_env())
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(proc.stdout, "")
        self.assertFalse((self.lifecycle_dir / f"{SESSION}.jsonl").exists())

    def test_malformed_session_id_fails_without_writing(self) -> None:
        proc = self.run_hook(
            START, start_payload(session_id="../../etc/passwd"), self.launch_env()
        )
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(list(self.lifecycle_dir.glob("*")), [])

    def test_malformed_payload_fails_without_writing(self) -> None:
        proc = self.run_hook(START, "not json at all", self.launch_env())
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(list(self.lifecycle_dir.glob("*")), [])

    def test_missing_cwd_fails_without_writing(self) -> None:
        payload = start_payload()
        del payload["cwd"]
        proc = self.run_hook(START, payload, self.launch_env())
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(list(self.lifecycle_dir.glob("*")), [])

    def test_no_registry_directory_leaves_pid_null_and_writes_no_name(self) -> None:
        proc = self.run_hook(
            START,
            start_payload(),
            {**self.launch_env(), "CLAUDE_CONFIG_DIR": str(self.config_dir)},
        )
        self.assert_clean(proc)

        events = self.events()
        self.assertEqual(len(events), 1, events)
        started = events[0]
        assert isinstance(started, StartedEvent)
        self.assertIsNone(started.pid)


class SessionEndHookTests(_SessionHookCase):
    """hook-session-end: the `ended` line, preceded by a `named` line."""

    def test_registry_name_is_recorded_before_the_end(self) -> None:
        self.write_registry(name="projects-9a", name_source="user")
        proc = self.run_hook(
            END,
            end_payload(),
            {**self.launch_env(), "CLAUDE_CONFIG_DIR": str(self.config_dir)},
        )
        self.assert_clean(proc)

        events = self.events()
        self.assertEqual(len(events), 2, events)
        named, ended = events
        assert isinstance(named, NamedEvent)
        self.assertEqual(named.name, "projects-9a")
        self.assertEqual(named.name_source, "user")
        assert isinstance(ended, EndedEvent)
        self.assertEqual(ended.outcome, "exited")
        self.assertEqual(ended.reason, "prompt_input_exit")
        self.assertIsNone(ended.detail)

    def test_unknown_reason_without_registry_records_the_end_alone(self) -> None:
        proc = self.run_hook(END, end_payload("went-away"), self.launch_env())
        self.assert_clean(proc)

        events = self.events()
        self.assertEqual(len(events), 1, events)
        ended = events[0]
        assert isinstance(ended, EndedEvent)
        self.assertEqual(ended.outcome, "exited")
        self.assertIsNone(ended.reason)
        self.assertIsNone(ended.detail)

    def test_malformed_session_id_fails_without_writing(self) -> None:
        proc = self.run_hook(END, end_payload(session_id="nope"), self.launch_env())
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(list(self.lifecycle_dir.glob("*")), [])


class SessionHookAppendTests(_SessionHookCase):
    """The two scripts append to one file, in the order they ran."""

    def test_start_then_end_accumulate_in_one_file(self) -> None:
        self.write_registry()
        env = {**self.launch_env(), "CLAUDE_CONFIG_DIR": str(self.config_dir)}
        self.assert_clean(self.run_hook(START, start_payload(), env))
        self.assert_clean(self.run_hook(END, end_payload("clear"), env))

        kinds = [type(e).__name__ for e in self.events()]
        self.assertEqual(
            kinds, ["StartedEvent", "NamedEvent", "NamedEvent", "EndedEvent"]
        )
        text = (self.lifecycle_dir / f"{SESSION}.jsonl").read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(len(text.strip().split("\n")), 4)


class SessionHookDeploymentTests(_SessionHookCase):
    """Both scripts are deployable like every other hook script."""

    def test_deploy_writes_both_executable(self) -> None:
        scripts_dir = self.root / "scripts"
        results = deploy_scripts([START, END], scripts_dir)
        self.assertEqual(results, [(START, "created"), (END, "created")])
        for name in (START, END):
            dest = scripts_dir / name
            self.assertEqual(dest.read_text(), HOOK_SCRIPTS[name])
            self.assertTrue(os.access(dest, os.X_OK), f"{name} must be executable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
