"""Tests for the canonical guardrail protocol model (claudewheel.guardrail).

These pin the frozen spec: the exact deny/ask arrays and their order, the
structural invariants each tier must satisfy, absence of duplicate settings
rules, newline-free advice, Python-compilability of every hook pattern, and
the two ordering constraints (git-checkout-file before git-checkout;
git-push-delete before push).

Note on regex: hook patterns are stored as ERE text and validated only for
Python ``re`` compilability here. Real bash/grep -E behavior is exercised by
Phase 2's end-to-end hook tests -- ERE/PCRE differences are out of scope for
this model-level suite.
"""

from __future__ import annotations

import re
import unittest

from claudewheel import guardrail
from claudewheel.guardrail import (
    ALLOW_CONFLICTS,
    EXPECTED_HOOK_WIRINGS,
    RULES,
    Tier,
)

# The frozen canonical arrays -- duplicated here verbatim so the test is an
# independent contract, not a mirror of the module's own construction.
EXPECTED_DENY = [
    "Bash(rm:*)",
    "Bash(git add .)",
    "Bash(git add -A*)",
    "Bash(git add --all*)",
    "Bash(git add -u*)",
    "Bash(git stash:*)",
    "Bash(git restore:*)",
    "Bash(git checkout:*)",
    "Bash(git push origin --delete*)",
]

EXPECTED_ASK = [
    "Bash(git push:*)",
    "Bash(safegit push:*)",
    "Bash(./safegit push:*)",
    "Bash(git reset *)",
    "Bash(git switch -f*)",
    "Bash(git switch --force*)",
    "Bash(gh workflow run*)",
    "Bash(saferm purge:*)",
    "Bash(git rebase *)",
    "Bash(safegit rewrite-author:*)",
    "Bash(sudo:*)",
]


def _rule(key: str) -> guardrail.GuardrailRule:
    for r in RULES:
        if r.key == key:
            return r
    raise KeyError(key)


def _index(key: str) -> int:
    for i, r in enumerate(RULES):
        if r.key == key:
            return i
    raise KeyError(key)


class StructuralTests(unittest.TestCase):
    def test_every_rule_has_key_and_tier(self) -> None:
        for r in RULES:
            self.assertTrue(r.key, "rule missing key")
            self.assertIsInstance(r.tier, Tier)

    def test_keys_are_unique(self) -> None:
        keys = [r.key for r in RULES]
        self.assertEqual(len(keys), len(set(keys)), "duplicate rule keys")

    def test_hard_deny_invariants(self) -> None:
        rules = guardrail.rules_by_tier(Tier.HARD_DENY)
        self.assertTrue(rules)
        for r in rules:
            self.assertGreaterEqual(len(r.hook_patterns), 1, r.key)
            self.assertGreaterEqual(len(r.deny_rules), 0, r.key)
            self.assertEqual(r.ask_rules, (), r.key)
            self.assertTrue(r.main_advice, r.key)
            self.assertTrue(r.subagent_advice, r.key)
            # Subagent advice is main advice plus the fixed suffix.
            self.assertTrue(
                r.subagent_advice.endswith(guardrail.SUBAGENT_HARD_DENY_SUFFIX),
                r.key,
            )

    def test_escalate_invariants(self) -> None:
        rules = guardrail.rules_by_tier(Tier.ESCALATE)
        self.assertTrue(rules)
        for r in rules:
            self.assertGreaterEqual(len(r.hook_patterns), 1, r.key)
            self.assertGreaterEqual(len(r.ask_rules), 1, r.key)
            self.assertEqual(r.deny_rules, (), r.key)
            # Main agent falls through silently -- no advice.
            self.assertIsNone(r.main_advice, r.key)
            self.assertTrue(r.subagent_advice, r.key)
            self.assertIn("parent agent", r.subagent_advice, r.key)
            self.assertIn("Explain in detail", r.subagent_advice, r.key)

    def test_advise_invariants(self) -> None:
        rules = guardrail.rules_by_tier(Tier.ADVISE)
        self.assertTrue(rules)
        for r in rules:
            self.assertGreaterEqual(len(r.hook_patterns), 1, r.key)
            self.assertTrue(r.main_advice, r.key)
            self.assertEqual(r.deny_rules, (), r.key)
            self.assertEqual(r.ask_rules, (), r.key)

    def test_ask_invariants(self) -> None:
        rules = guardrail.rules_by_tier(Tier.ASK)
        self.assertTrue(rules)
        for r in rules:
            self.assertGreaterEqual(len(r.ask_rules), 1, r.key)
            self.assertEqual(r.hook_patterns, (), r.key)
            self.assertEqual(r.deny_rules, (), r.key)
            self.assertIsNone(r.main_advice, r.key)
            self.assertIsNone(r.subagent_advice, r.key)


class ExactArrayTests(unittest.TestCase):
    def test_canonical_deny_rules_pinned(self) -> None:
        self.assertEqual(guardrail.canonical_deny_rules(), EXPECTED_DENY)

    def test_canonical_ask_rules_pinned(self) -> None:
        self.assertEqual(guardrail.canonical_ask_rules(), EXPECTED_ASK)


class SettingsRuleHygieneTests(unittest.TestCase):
    def test_no_duplicate_settings_rules(self) -> None:
        allrules = guardrail.all_settings_rules()
        self.assertEqual(
            len(allrules), len(set(allrules)), "duplicate settings rules"
        )

    def test_rm_kill_pkill_ask_rules_absent(self) -> None:
        ask = guardrail.canonical_ask_rules()
        for forbidden in ("Bash(rm:*)", "Bash(kill:*)", "Bash(pkill:*)"):
            self.assertNotIn(forbidden, ask, forbidden)

    def test_advice_strings_have_no_newlines(self) -> None:
        for r in RULES:
            for advice in (r.main_advice, r.subagent_advice):
                if advice is not None:
                    self.assertNotIn("\n", advice, r.key)


class HookPatternTests(unittest.TestCase):
    def test_every_hook_pattern_compiles(self) -> None:
        for r in RULES:
            for pat in r.hook_patterns:
                try:
                    re.compile(pat)
                except re.error as exc:  # pragma: no cover - failure path
                    self.fail(f"{r.key} pattern does not compile: {exc}")

    def test_git_add_bulk_does_not_match_plain_add(self) -> None:
        # Sanity that the bulk-add matcher stays narrow (Python re approximation).
        pat = _rule("git-add-bulk").hook_patterns[0]
        self.assertIsNone(re.search(pat, "git add file.py"))
        self.assertIsNotNone(re.search(pat, "git add -A"))
        self.assertIsNotNone(re.search(pat, "git add ."))

    def test_git_switch_force_does_not_match_plain_switch(self) -> None:
        pat = _rule("git-switch-force").hook_patterns[0]
        self.assertIsNone(re.search(pat, "git switch main"))
        self.assertIsNotNone(re.search(pat, "git switch -f main"))
        self.assertIsNotNone(re.search(pat, "git switch --force"))


class OrderingTests(unittest.TestCase):
    def test_checkout_file_before_checkout(self) -> None:
        self.assertLess(_index("git-checkout-file"), _index("git-checkout"))

    def test_push_delete_before_push(self) -> None:
        self.assertLess(_index("git-push-delete"), _index("push"))


class DerivedDataTests(unittest.TestCase):
    def test_allow_conflicts_excludes_kept_entries(self) -> None:
        # These must stay allowed and never appear in the cleanup list.
        self.assertNotIn("Bash(git rm:*)", ALLOW_CONFLICTS)
        self.assertNotIn("Bash(npm run kill:*)", ALLOW_CONFLICTS)

    def test_allow_conflicts_contents(self) -> None:
        expected = (
            "Bash(git add:*)",
            "Bash(git checkout:*)",
            "Bash(git stash:*)",
            "Bash(git stash push:*)",
            "Bash(sudo npm install:*)",
            "Bash(sudo -S npm install:*)",
            "Bash(sudo -S dnf install:*)",
            "Bash(sudo -S dnf install -y chromium)",
            "Bash(sudo adb start-server:*)",
            "Bash(sudo -n iptables -L INPUT -n)",
            'Bash(sudo -n ufw status || echo "(need sudo for ufw)")',
        )
        self.assertEqual(ALLOW_CONFLICTS, expected)

    def test_expected_hook_wirings(self) -> None:
        self.assertEqual(
            EXPECTED_HOOK_WIRINGS,
            (
                ("UserPromptSubmit", "", "hook-timestamp"),
                ("PreToolUse", "Agent", "hook-block-worktree"),
                ("PreToolUse", "Bash", "hook-block-unsafe-commands"),
                ("PostToolUse", "Bash", "hook-advise-commands"),
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
