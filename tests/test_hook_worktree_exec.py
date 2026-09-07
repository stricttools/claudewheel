"""End-to-end execution tests for the hand-written hook script templates.

Like tests/test_hook_unsafe_exec.py, these tests write the actual template from
HOOK_SCRIPTS to disk, chmod +x it, and run it under bash with a real payload on
stdin. Two scripts are covered:

  * ``hook-block-worktree`` -- the PreToolUse hook that denies Agent tool calls
    carrying ``isolation: "worktree"``. The central assertion is that the deny
    output PARSES as JSON: Claude Code silently discards unparseable hook
    output, so a malformed envelope means the deny never fires at all.
  * ``hook-timestamp`` -- the UserPromptSubmit hook that prints the current
    timestamp.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from claudewheel.hook_scripts import HOOK_SCRIPTS

# The timestamp script's format string: date '+%Y-%m-%d %H:%M:%S %Z'.
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _run_script(name: str, stdin: str) -> tuple[int, str]:
    """Run HOOK_SCRIPTS[*name*] under bash with *stdin* on its stdin.

    Returns (returncode, stdout). The script is written fresh to a temp file
    each call so the test always exercises the current template text.
    """
    script = HOOK_SCRIPTS[name]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        proc = subprocess.run(
            ["bash", path],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode, proc.stdout
    finally:
        Path(path).unlink()


def _run_worktree_hook(payload: object) -> tuple[int, str]:
    """Run the worktree hook with *payload* JSON-encoded on stdin."""
    return _run_script("hook-block-worktree", json.dumps(payload))


def _agent_payload(**tool_input: object) -> dict[str, object]:
    """An Agent tool-call payload with the given tool_input keys."""
    return {"tool_name": "Agent", "tool_input": tool_input}


class WorktreeHookDenyTests(unittest.TestCase):
    """The one denying branch must emit a parseable deny envelope."""

    def test_worktree_isolation_denies_with_valid_json(self) -> None:
        rc, out = _run_worktree_hook(_agent_payload(isolation="worktree"))
        self.assertEqual(rc, 0, "hook should exit 0 when denying")
        self.assertTrue(out.strip(), "expected deny output, got empty stdout")
        try:
            obj = json.loads(out)
        except json.JSONDecodeError as exc:
            self.fail(f"deny output is not valid JSON: {exc}\noutput: {out!r}")
        hso = obj["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertTrue(
            hso["permissionDecisionReason"], "deny reason must be non-empty"
        )

    def test_deny_reason_mentions_worktree_isolation(self) -> None:
        _, out = _run_worktree_hook(_agent_payload(isolation="worktree"))
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("orktree", reason)

    def test_extra_tool_input_keys_still_deny(self) -> None:
        rc, out = _run_worktree_hook(
            _agent_payload(
                isolation="worktree",
                description="do a thing",
                prompt="run the task",
            )
        )
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        self.assertEqual(
            obj["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )


class WorktreeHookTemplateTests(unittest.TestCase):
    """The deny envelope is built by jq, and that is not observable at runtime.

    Today's reason string contains no quote, backslash or newline, so a
    reverted printf-built JSON literal would emit byte-identical output and
    every execution test above would still pass -- the escaping only shows up
    once someone edits the reason.  The property is therefore asserted on the
    template text itself.
    """

    def test_deny_envelope_is_built_with_jq(self) -> None:
        script = HOOK_SCRIPTS["hook-block-worktree"]
        self.assertIn("jq -cn --arg reason", script)

    def test_deny_envelope_is_not_a_printf_json_literal(self) -> None:
        script = HOOK_SCRIPTS["hook-block-worktree"]
        for line in script.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "printf" in line and "hookSpecificOutput" in line:
                self.fail(f"deny JSON is hand-interpolated by printf: {line!r}")


class WorktreeHookAllowTests(unittest.TestCase):
    """Every early-exit branch allows: exit 0 with empty stdout."""

    def _assert_allows(self, stdin: str, label: str) -> None:
        rc, out = _run_script("hook-block-worktree", stdin)
        self.assertEqual(rc, 0, f"hook should exit 0 for {label}")
        self.assertEqual(
            out.strip(), "", f"expected allow (empty stdout) for {label}, got {out!r}"
        )

    def test_empty_stdin_allows(self) -> None:
        self._assert_allows("", "empty stdin")

    def test_non_agent_tool_allows(self) -> None:
        self._assert_allows(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
            "Bash tool call",
        )

    def test_agent_without_isolation_allows(self) -> None:
        self._assert_allows(
            json.dumps(_agent_payload(description="plain agent", prompt="do it")),
            "Agent call with no isolation key",
        )

    def test_isolation_none_allows(self) -> None:
        self._assert_allows(
            json.dumps(_agent_payload(isolation="none")),
            'isolation: "none"',
        )

    def test_read_of_path_containing_worktree_allows(self) -> None:
        # The word "worktree" inside an unrelated field must not fire the hook:
        # only .tool_input.isolation on an Agent call is inspected.
        self._assert_allows(
            json.dumps(
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/home/m/Projects/worktree/notes.md"},
                }
            ),
            "Read of a path containing 'worktree'",
        )


class WorktreeHookCaseSensitivityTests(unittest.TestCase):
    """Pin the matcher's case sensitivity as it behaves today.

    The script compares with `[[ "$isolation" != "worktree" ]]`, a
    case-SENSITIVE string comparison, so a capitalized "Worktree" falls through
    and is allowed. This test documents current behavior; it is not a statement
    that case-insensitive matching would be wrong.
    """

    def test_capitalized_worktree_currently_allows(self) -> None:
        rc, out = _run_worktree_hook(_agent_payload(isolation="Worktree"))
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "",
            "documents current behavior: the isolation comparison is "
            "case-sensitive, so 'Worktree' is not matched",
        )


class TimestampHookTests(unittest.TestCase):
    """hook-timestamp prints one parseable timestamp line."""

    def test_prints_single_parseable_timestamp_line(self) -> None:
        rc, out = _run_script("hook-timestamp", "")
        self.assertEqual(rc, 0, "hook-timestamp should exit 0")
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, f"expected a single line, got {out!r}")
        # Output format is "%Y-%m-%d %H:%M:%S %Z"; the trailing zone abbreviation
        # is locale/system dependent, so split it off and parse the rest.
        stamp, _, zone = lines[0].rpartition(" ")
        self.assertTrue(zone, f"expected a timezone field in {lines[0]!r}")
        try:
            datetime.strptime(stamp, _TIMESTAMP_FORMAT)
        except ValueError as exc:
            self.fail(f"timestamp {stamp!r} does not match the script's format: {exc}")


if __name__ == "__main__":
    unittest.main()
