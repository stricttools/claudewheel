"""End-to-end execution tests for the hook-advise-commands script.

Same exec-harness style as test_hook_unsafe_exec.py: the generated PostToolUse
script from HOOK_SCRIPTS is written to disk and run under bash with a real JSON
payload. The command has already run at PostToolUse time, so this hook only
nudges -- it emits additionalContext advice for matching commands and stays
silent (empty stdout, exit 0) otherwise.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from claudewheel import guardrail
from claudewheel.guardrail import Tier
from claudewheel.hook_scripts import HOOK_SCRIPTS


def _run_hook(command: str | None, tool_name: str = "Bash") -> tuple[int, str]:
    """Run the advise hook with a payload built from *command*/*tool_name*."""
    script = HOOK_SCRIPTS["hook-advise-commands"]
    tool_input: dict = {}
    if command is not None:
        tool_input = {"command": command}
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        proc = subprocess.run(
            ["bash", path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode, proc.stdout
    finally:
        Path(path).unlink()


def _assert_advises(testcase: unittest.TestCase, command: str) -> str:
    """Assert the hook advises *command* with valid PostToolUse JSON.

    Returns the additionalContext string.
    """
    rc, out = _run_hook(command)
    testcase.assertEqual(rc, 0, f"hook should exit 0 for {command!r}")
    testcase.assertTrue(
        out.strip(), f"expected advice output for {command!r}, got empty stdout"
    )
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as exc:  # noqa: PERF203
        testcase.fail(
            f"advice output for {command!r} is not valid JSON: {exc}\noutput: {out!r}"
        )
    hso = obj["hookSpecificOutput"]
    testcase.assertEqual(hso["hookEventName"], "PostToolUse")
    ctx = hso["additionalContext"]
    testcase.assertTrue(ctx, f"additionalContext for {command!r} must be non-empty")
    return ctx


def _assert_silent(testcase: unittest.TestCase, command: str) -> None:
    """Assert the hook stays silent (exit 0, empty stdout) for *command*."""
    rc, out = _run_hook(command)
    testcase.assertEqual(rc, 0, f"hook should exit 0 for {command!r}")
    testcase.assertEqual(
        out.strip(),
        "",
        f"expected empty stdout for {command!r}, got {out!r}",
    )


def _kill_advice() -> str:
    """The main_advice text of the ADVISE 'kill' rule from the canonical model."""
    for rule in guardrail.rules_by_tier(Tier.ADVISE):
        if rule.key == "kill":
            assert rule.main_advice is not None
            return rule.main_advice
    raise AssertionError("no ADVISE 'kill' rule in guardrail.RULES")


class AdviseKillTests(unittest.TestCase):
    """kill/pkill commands produce graceful-stop advice."""

    def test_kill_pid(self) -> None:
        ctx = _assert_advises(self, "kill 123")
        self.assertEqual(ctx, _kill_advice())
        self.assertIn("graceful stop", ctx)

    def test_pkill_by_name(self) -> None:
        ctx = _assert_advises(self, "pkill -f name")
        self.assertEqual(ctx, _kill_advice())

    def test_compound_kill(self) -> None:
        # kill embedded mid-command (after &&) still nudges.
        ctx = _assert_advises(self, "build.sh && kill $(cat app.pid)")
        self.assertEqual(ctx, _kill_advice())


class AdviseNegativeTests(unittest.TestCase):
    """Commands the ADVISE pattern must NOT match stay silent."""

    def test_echo(self) -> None:
        _assert_silent(self, "echo hello")

    def test_killall_excluded(self) -> None:
        # The model requires kill/pkill to be followed by whitespace/end, so
        # 'killall' (no separator after 'kill') must not match.
        _assert_silent(self, "killall x")

    def test_skills(self) -> None:
        _assert_silent(self, "skills")

    def test_npm_install(self) -> None:
        _assert_silent(self, "npm install")


class AdviseNonBashTests(unittest.TestCase):
    """Non-Bash tool payloads produce no advice."""

    def test_non_bash_tool_silent(self) -> None:
        rc, out = _run_hook("kill 123", tool_name="Read")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


class KillFalsePositiveTests(unittest.TestCase):
    """The kill advisor must be command-anchored: it nudges only when kill /
    pkill is actually invoked as a command (directly or via a real wrapper),
    never when 'kill' is merely a task/target name (npm run kill, etc.)."""

    # 'kill' here is a script/task name, not the kill(1) command -> stay silent.
    SILENT = (
        "npm run kill",
        "yarn kill",
        "make kill",
        "pnpm kill",
        "killall x",
    )

    # Real kill/pkill invocations, direct or through a wrapper -> must advise.
    ADVISE = (
        "kill 123",
        "kill -9 123",
        "pkill -f x",
        "build.sh && kill $(cat app.pid)",
        "sudo kill 123",
        "xargs -0 kill",
        "find . -exec kill {} \\;",
    )

    def test_non_command_kill_stays_silent(self) -> None:
        for command in self.SILENT:
            with self.subTest(command=command):
                _assert_silent(self, command)

    def test_real_kill_advises(self) -> None:
        for command in self.ADVISE:
            with self.subTest(command=command):
                ctx = _assert_advises(self, command)
                self.assertEqual(ctx, _kill_advice())


if __name__ == "__main__":
    unittest.main()
