"""End-to-end execution tests for the hook-block-unsafe-commands script.

These tests write the actual template from HOOK_SCRIPTS to disk, chmod +x it,
and run it under bash with a real JSON payload on stdin -- exercising the
grep matchers and the deny() JSON emitter for real (not via inline regex
approximations). This catches two bug classes:

  1. Invalid deny JSON (unparseable output is silently discarded by Claude
     Code, so a "deny" never fires).
  2. rm matcher gaps (sudo/env/xargs/find -exec rm slipping through).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from claudewheel.hook_scripts import HOOK_SCRIPTS


def _run_hook(command: str | None, tool_name: str = "Bash") -> tuple[int, str]:
    """Run the hook script with a payload built from *command*/*tool_name*.

    Returns (returncode, stdout). The script is written fresh to a temp file
    each call so the test always exercises the current template text.
    """
    script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
    tool_input: dict = {}
    if command is not None:
        tool_input = {"command": command}
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False
    ) as f:
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


def _assert_denies(testcase: unittest.TestCase, command: str) -> str:
    """Assert the hook denies *command* with valid deny JSON. Returns reason."""
    rc, out = _run_hook(command)
    testcase.assertEqual(rc, 0, f"hook should exit 0 for {command!r}")
    testcase.assertTrue(
        out.strip(), f"expected deny output for {command!r}, got empty stdout"
    )
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as exc:  # noqa: PERF203
        testcase.fail(
            f"deny output for {command!r} is not valid JSON: {exc}\noutput: {out!r}"
        )
    hso = obj["hookSpecificOutput"]
    testcase.assertEqual(hso["hookEventName"], "PreToolUse")
    testcase.assertEqual(
        hso["permissionDecision"], "deny", f"expected deny for {command!r}"
    )
    reason = hso["permissionDecisionReason"]
    testcase.assertTrue(reason, f"deny reason for {command!r} must be non-empty")
    return reason


def _assert_allows(testcase: unittest.TestCase, command: str) -> None:
    """Assert the hook allows (exit 0, empty stdout) *command*."""
    rc, out = _run_hook(command)
    testcase.assertEqual(rc, 0, f"hook should exit 0 for {command!r}")
    testcase.assertEqual(
        out.strip(), "", f"expected allow (empty stdout) for {command!r}, got {out!r}"
    )


class HookDenyBranchTests(unittest.TestCase):
    """Every deny branch must emit valid JSON with a deny decision."""

    def test_git_add_all_denies(self) -> None:
        reason = _assert_denies(self, "git add -A")
        self.assertIn("safegit commit", reason)

    def test_git_add_dot_denies(self) -> None:
        _assert_denies(self, "git add .")

    def test_git_stash_denies(self) -> None:
        reason = _assert_denies(self, "git stash")
        self.assertIn("safegit", reason)

    def test_git_restore_denies(self) -> None:
        reason = _assert_denies(self, "git restore file.txt")
        self.assertIn("Edit", reason)

    def test_git_checkout_doubledash_denies(self) -> None:
        reason = _assert_denies(self, "git checkout -- file.txt")
        self.assertIn("Edit", reason)

    def test_rm_deny_message_mentions_saferm(self) -> None:
        reason = _assert_denies(self, "rm foo.txt")
        self.assertIn("saferm delete", reason)
        # The literal message contains quotes around "why" -- these must survive
        # intact through JSON encoding.
        self.assertIn('"why"', reason)


class HookRmHardeningTests(unittest.TestCase):
    """Hardened rm matcher: catch sudo/env/xargs/find -exec rm variants."""

    def test_plain_rm(self) -> None:
        _assert_denies(self, "rm foo")

    def test_rm_rf(self) -> None:
        _assert_denies(self, "rm -rf x")

    def test_rm_after_and(self) -> None:
        _assert_denies(self, "a && rm b")

    def test_rm_after_semicolon(self) -> None:
        _assert_denies(self, "a; rm b")

    def test_rm_after_pipe(self) -> None:
        _assert_denies(self, "a | rm")

    def test_sudo_rm(self) -> None:
        _assert_denies(self, "sudo rm x")

    def test_env_rm(self) -> None:
        _assert_denies(self, "env rm x")

    def test_xargs_rm(self) -> None:
        _assert_denies(self, "ls | xargs rm")

    def test_xargs_flag_rm(self) -> None:
        _assert_denies(self, "xargs -0 rm")

    def test_find_exec_rm(self) -> None:
        _assert_denies(self, "find . -name '*.tmp' -exec rm {} \\;")

    def test_find_ok_rm(self) -> None:
        _assert_denies(self, "find . -ok rm {} \\;")


class HookAllowTests(unittest.TestCase):
    """Commands that must NOT be blocked (allow: exit 0, empty stdout)."""

    def test_npm_install(self) -> None:
        _assert_allows(self, "npm install")

    def test_rmdir(self) -> None:
        _assert_allows(self, "rmdir foo")

    def test_grep_rm(self) -> None:
        _assert_allows(self, "grep rm file")

    def test_git_rm(self) -> None:
        _assert_allows(self, "git rm file")

    def test_path_containing_rm(self) -> None:
        _assert_allows(self, "ls /home/norm/")

    def test_saferm(self) -> None:
        _assert_allows(self, 'saferm delete --description "x" f')

    def test_echo(self) -> None:
        _assert_allows(self, "echo hello")


class HookNonBashTests(unittest.TestCase):
    """Non-Bash tool payloads are always allowed."""

    def test_non_bash_tool_allows(self) -> None:
        rc, out = _run_hook("rm -rf /", tool_name="Read")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()
