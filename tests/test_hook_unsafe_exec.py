"""End-to-end execution tests for the hook-block-unsafe-commands script.

These tests write the actual template from HOOK_SCRIPTS to disk, chmod +x it,
and run it under bash with a real JSON payload on stdin -- exercising the
grep matchers and the deny() JSON emitter for real (not via inline regex
approximations). This catches two bug classes:

  1. Invalid deny JSON (unparseable output is silently discarded by Claude
     Code, so a "deny" never fires).
  2. rm matcher gaps (sudo/env/xargs/find -exec rm slipping through).

The script is generated from claudewheel.guardrail's canonical model, so the
tier matrices below are driven directly off guardrail.RULES: every HARD_DENY
rule must deny both main and subagent callers, every ESCALATE rule must deny
subagents but let the main caller fall through.
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
from claudewheel.guardrail import (
    ESCALATE_TAIL,
    SUBAGENT_HARD_DENY_SUFFIX,
    Tier,
)
from claudewheel.hook_scripts import HOOK_SCRIPTS


def _run_hook(
    command: str | None,
    tool_name: str = "Bash",
    agent_id: str | None = None,
) -> tuple[int, str]:
    """Run the hook script with a payload built from the arguments.

    Returns (returncode, stdout). The script is written fresh to a temp file
    each call so the test always exercises the current template text. When
    *agent_id* is provided, it is added to the payload (mirroring Claude Code,
    which includes agent_id/agent_type only for subagent tool calls).
    """
    script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
    tool_input: dict[str, str] = {}
    if command is not None:
        tool_input = {"command": command}
    payload: dict[str, object] = {"tool_name": tool_name, "tool_input": tool_input}
    if agent_id is not None:
        payload["agent_id"] = agent_id
        payload["agent_type"] = "claude"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        proc = subprocess.run(
            ["bash", path],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode, proc.stdout
    finally:
        Path(path).unlink()


def _assert_denies(
    testcase: unittest.TestCase,
    command: str,
    agent_id: str | None = None,
) -> str:
    """Assert the hook denies *command* with valid deny JSON. Returns reason."""
    rc, out = _run_hook(command, agent_id=agent_id)
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
    reason: str = hso["permissionDecisionReason"]
    testcase.assertTrue(reason, f"deny reason for {command!r} must be non-empty")
    return reason


def _assert_allows(
    testcase: unittest.TestCase,
    command: str,
    agent_id: str | None = None,
) -> None:
    """Assert the hook allows (exit 0, empty stdout) *command*."""
    rc, out = _run_hook(command, agent_id=agent_id)
    testcase.assertEqual(rc, 0, f"hook should exit 0 for {command!r}")
    testcase.assertEqual(
        out.strip(),
        "",
        f"expected allow (empty stdout) for {command!r} "
        f"(agent_id={agent_id!r}), got {out!r}",
    )


# One representative command that matches each HARD_DENY rule's pattern.
_HARD_DENY_SAMPLES: dict[str, str] = {
    "rm": "rm foo",
    "git-add-bulk": "git add -A",
    "git-stash": "git stash",
    "git-restore": "git restore file.txt",
    "git-checkout-file": "git checkout -- file.txt",
    "git-checkout": "git checkout main",
    "git-push-delete": "git push origin --delete foo",
}

# One representative command that matches each ESCALATE rule's pattern.
_ESCALATE_SAMPLES: dict[str, str] = {
    "push": "git push",
    "git-reset": "git reset --hard",
    "git-switch-force": "git switch -f other",
    "gh-workflow-run": "gh workflow run ci.yml",
    "saferm-purge": "saferm purge",
    "git-rebase": "git rebase main",
    "safegit-rewrite-author": "safegit rewrite-author foo",
}


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


class HookHardDenyMatrixTests(unittest.TestCase):
    """Every HARD_DENY rule denies BOTH main and subagent callers.

    Driven off guardrail.RULES: the main caller sees main_advice, the subagent
    caller sees subagent_advice (main_advice + the report-to-parent suffix).
    """

    def test_every_hard_deny_rule_has_a_sample(self) -> None:
        keys = {r.key for r in guardrail.rules_by_tier(Tier.HARD_DENY)}
        self.assertEqual(
            keys,
            set(_HARD_DENY_SAMPLES),
            "HARD_DENY sample commands out of sync with guardrail.RULES",
        )

    def test_main_caller_denied_with_main_advice(self) -> None:
        for rule in guardrail.rules_by_tier(Tier.HARD_DENY):
            command = _HARD_DENY_SAMPLES[rule.key]
            with self.subTest(rule=rule.key, caller="main"):
                reason = _assert_denies(self, command)
                self.assertEqual(reason, rule.main_advice)

    def test_subagent_caller_denied_with_subagent_advice(self) -> None:
        for rule in guardrail.rules_by_tier(Tier.HARD_DENY):
            command = _HARD_DENY_SAMPLES[rule.key]
            with self.subTest(rule=rule.key, caller="subagent"):
                reason = _assert_denies(self, command, agent_id="sub-1")
                self.assertEqual(reason, rule.subagent_advice)
                self.assertIn(SUBAGENT_HARD_DENY_SUFFIX, reason)


class HookOrderingTests(unittest.TestCase):
    """Rule ordering is load-bearing: specific rules must win over general ones."""

    def test_checkout_file_wins_over_checkout(self) -> None:
        # 'git checkout -- file' matches BOTH git-checkout-file and git-checkout;
        # the file-specific (Edit-tool) advice must fire, not the deprecation one.
        reason = _assert_denies(self, "git checkout -- file.txt")
        self.assertIn("Edit tool", reason)
        self.assertNotIn("deprecated", reason)

    def test_push_delete_wins_over_push_for_main(self) -> None:
        # 'git push --delete' is HARD_DENY (branch deletion), so even a MAIN
        # caller is denied -- it does not fall through like a plain push.
        reason = _assert_denies(self, "git push origin --delete foo")
        self.assertIn("Deleting remote branches", reason)

    def test_plain_push_main_falls_through(self) -> None:
        # Plain push is ESCALATE: main caller falls through (settings ask rule
        # handles it), so the hook emits nothing.
        _assert_allows(self, "git push")

    def test_plain_push_subagent_escalates(self) -> None:
        reason = _assert_denies(self, "git push", agent_id="sub-1")
        self.assertIn("rlsbl", reason)
        self.assertIn("Explain in detail to your parent agent", reason)


class HookEscalateMatrixTests(unittest.TestCase):
    """Every ESCALATE rule denies subagents but lets the main caller through."""

    def test_every_escalate_rule_has_a_sample(self) -> None:
        keys = {r.key for r in guardrail.rules_by_tier(Tier.ESCALATE)}
        self.assertEqual(
            keys,
            set(_ESCALATE_SAMPLES),
            "ESCALATE sample commands out of sync with guardrail.RULES",
        )

    def test_subagent_denied_with_lead_and_tail(self) -> None:
        for rule in guardrail.rules_by_tier(Tier.ESCALATE):
            command = _ESCALATE_SAMPLES[rule.key]
            with self.subTest(rule=rule.key, caller="subagent"):
                reason = _assert_denies(self, command, agent_id="sub-1")
                self.assertEqual(reason, rule.subagent_advice)
                self.assertIn(ESCALATE_TAIL, reason)

    def test_main_caller_falls_through(self) -> None:
        for rule in guardrail.rules_by_tier(Tier.ESCALATE):
            command = _ESCALATE_SAMPLES[rule.key]
            with self.subTest(rule=rule.key, caller="main"):
                _assert_allows(self, command)


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
    """Commands that must NOT be blocked (allow: exit 0, empty stdout).

    Checked for BOTH main and subagent callers -- a negative must stay silent
    regardless of who runs it.
    """

    NEGATIVES = [
        "npm install",
        "rmdir foo",
        "grep rm file",
        "git rm file",
        "ls /home/norm/",
        'saferm delete --description "x" f',
        "git switch main",
        "git add file.txt",
        "rlsbl push",
        "echo hello",
    ]

    def test_negatives_main(self) -> None:
        for command in self.NEGATIVES:
            with self.subTest(command=command, caller="main"):
                _assert_allows(self, command)

    def test_negatives_subagent(self) -> None:
        for command in self.NEGATIVES:
            with self.subTest(command=command, caller="subagent"):
                _assert_allows(self, command, agent_id="sub-1")


class HookNonBashTests(unittest.TestCase):
    """Non-Bash tool payloads are always allowed."""

    def test_non_bash_tool_allows(self) -> None:
        rc, out = _run_hook("rm -rf /", tool_name="Read")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_non_bash_tool_allows_subagent(self) -> None:
        rc, out = _run_hook("rm -rf /", tool_name="Read", agent_id="sub-1")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


# Independent hand-enumerated oracle for the git-push-delete rule. These rows
# are NOT derived from the rule's own regex -- they assert the intended contract
# so a tautological "regex matches its own pattern" cannot pass them.
_DELETE_ADVICE_MARKER = "Deleting remote branches"

# Remotes to exercise: two named remotes plus a URL remote (whose embedded
# colon in git@host:repo.git must NOT be misread as an empty-source refspec).
_DELETE_REMOTES = ("origin", "upstream", "git@host:repo.git")

# Deletion forms, as f-string templates over {remote}. Every one of these MUST
# be treated as a remote-branch deletion (hard-deny) regardless of remote.
_DELETE_FORMS = (
    ("colon-empty-source", "git push {remote} :b"),
    ("colon-force-empty-source", "git push {remote} +:b"),
    ("colon-refs-heads", "git push {remote} :refs/heads/b"),
    ("short-d", "git push {remote} -d b"),
    ("long-delete", "git push {remote} --delete b"),
    ("flag-reordered", "git push --delete {remote} b"),
)


class HookGitPushDeleteMatrixTests(unittest.TestCase):
    """git-push-delete: every deletion form for every remote hard-denies both
    callers with the branch-deletion advice."""

    def test_deletion_forms_hard_deny_main(self) -> None:
        for remote in _DELETE_REMOTES:
            for form_name, template in _DELETE_FORMS:
                command = template.format(remote=remote)
                with self.subTest(remote=remote, form=form_name, caller="main"):
                    reason = _assert_denies(self, command)
                    self.assertIn(_DELETE_ADVICE_MARKER, reason)

    def test_deletion_forms_hard_deny_subagent(self) -> None:
        for remote in _DELETE_REMOTES:
            for form_name, template in _DELETE_FORMS:
                command = template.format(remote=remote)
                with self.subTest(remote=remote, form=form_name, caller="subagent"):
                    reason = _assert_denies(self, command, agent_id="sub-1")
                    self.assertIn(_DELETE_ADVICE_MARKER, reason)
                    self.assertIn(SUBAGENT_HARD_DENY_SUFFIX, reason)


class HookGitPushDeleteNegativeTests(unittest.TestCase):
    """Pushes that are NOT branch deletions must not fire the delete rule.

    For a MAIN caller the delete rule (HARD_DENY) is the only thing that could
    deny a push, so an allow (empty stdout) proves the delete rule stayed
    silent. For a SUBAGENT caller the plain-push ESCALATE rule may legitimately
    deny -- but its reason must be the escalation message, never the
    branch-deletion advice.
    """

    # Ordinary pushes and force-with-source pushes -- never deletions.
    NORMAL_PUSHES = (
        "git push origin b",
        "git push origin HEAD:main",
        "git push origin main:main",
        "git push -u origin b",
        "git push origin +HEAD:main",
        "git push git@host:repo.git main",
    )

    # A stray colon or -d living in a LATER shell segment must not leak into the
    # push segment's match (the matcher is bounded to a single shell segment).
    CROSS_SEPARATOR = (
        "git push origin main && echo ' :done'",
        "git push origin main; grep ' :' f",
        "git push origin main && git branch -d oldbranch",
    )

    def _assert_not_delete_denied(self, command: str) -> None:
        # Main caller: the delete rule is the only push-denier -> must allow.
        _assert_allows(self, command)
        # Subagent caller: if denied at all, it must be the escalation, not the
        # branch-deletion advice.
        _, out = _run_hook(command, agent_id="sub-1")
        if out.strip():
            self.assertNotIn(_DELETE_ADVICE_MARKER, out)

    def test_normal_pushes_not_delete_denied(self) -> None:
        for command in self.NORMAL_PUSHES:
            with self.subTest(command=command):
                self._assert_not_delete_denied(command)

    def test_cross_separator_not_delete_denied(self) -> None:
        for command in self.CROSS_SEPARATOR:
            with self.subTest(command=command):
                self._assert_not_delete_denied(command)


class HookAntiKeywordFalsePositiveTests(unittest.TestCase):
    """Structural anchoring property: a rule's command keyword appearing as a
    NON-command token (an argument to echo / another script) must not fire.

    Exercised via the block hook, so it covers every HARD_DENY and ESCALATE
    rule. The subagent caller is the strictest observer (both tiers can deny a
    subagent), so subagent-allow proves the rule did not fire for either tier.
    """

    def _samples(self) -> dict[str, str]:
        return {**_HARD_DENY_SAMPLES, **_ESCALATE_SAMPLES}

    def test_keyword_as_echo_argument_does_not_fire(self) -> None:
        for key, sample in self._samples().items():
            command = f"echo {sample}"
            with self.subTest(rule=key, wrapper="echo"):
                _assert_allows(self, command, agent_id="sub-1")

    def test_keyword_as_script_argument_does_not_fire(self) -> None:
        for key, sample in self._samples().items():
            command = f"somescript {sample}"
            with self.subTest(rule=key, wrapper="somescript"):
                _assert_allows(self, command, agent_id="sub-1")


if __name__ == "__main__":
    unittest.main()
