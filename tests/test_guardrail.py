"""Tests for the canonical guardrail protocol model (claudewheel.guardrail).

These pin the frozen spec: the exact deny/ask arrays and their order, the
structural invariants each tier must satisfy, absence of duplicate settings
rules, newline-free advice, Python-compilability of every hook pattern, and
the two ordering constraints (git-checkout-file before git-checkout;
git-push-delete before push).

Note on regex: hook patterns are stored as ERE text and validated only for
Python ``re`` compilability here. Real bash/grep -E behavior is exercised by
the end-to-end hook-script tests -- ERE/PCRE differences are out of scope for
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
    SettingsCoverage,
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
    "Bash(sleep:*)",
    "Bash(go test:*)",
    "Bash(go build:*)",
    "Bash(go vet:*)",
    "Bash(go install:*)",
    "Bash(pytest:*)",
    "Bash(python -m pytest:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(uv run pytest:*)",
    "Bash(uv run python -m pytest:*)",
    "Bash(uv run python3 -m pytest:*)",
    "Bash(npm test:*)",
    "Bash(npm run test:*)",
    "Bash(npm ci:*)",
    "Bash(cargo test:*)",
    "Bash(cargo build:*)",
    "Bash(cgofree generate:*)",
    "Bash(cgofree verify:*)",
    "Bash(./make.bash:*)",
    "Bash(./all.bash:*)",
    "Bash(./run.bash:*)",
    "Bash(bash make.bash:*)",
    "Bash(bash all.bash:*)",
    "Bash(bash run.bash:*)",
    "Bash(make:*)",
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
    "Bash(safegit author rewrite:*)",
    "Bash(./safegit author rewrite:*)",
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
            assert r.subagent_advice is not None
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
            assert r.subagent_advice is not None
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
        self.assertEqual(len(allrules), len(set(allrules)), "duplicate settings rules")

    def test_rm_kill_pkill_ask_rules_absent(self) -> None:
        ask = guardrail.canonical_ask_rules()
        for forbidden in ("Bash(rm:*)", "Bash(kill:*)", "Bash(pkill:*)"):
            self.assertNotIn(forbidden, ask, forbidden)

    def test_advice_strings_have_no_newlines(self) -> None:
        for r in RULES:
            for advice in (r.main_advice, r.subagent_advice):
                if advice is not None:
                    self.assertNotIn("\n", advice, r.key)


class SentencePunctuationTests(unittest.TestCase):
    """The advice/message joins must read as clean sentences.

    Regression guard for the message-join defect: HARD_DENY main advice did
    not end with sentence punctuation, so joining the subagent suffix produced
    ``...instead of 'rm' You are a subagent...`` with no separating period.
    """

    _TERMINALS = (".", "!", "?")

    def test_hard_deny_main_advice_ends_with_terminal_punctuation(self) -> None:
        rules = guardrail.rules_by_tier(Tier.HARD_DENY)
        self.assertTrue(rules)
        for r in rules:
            self.assertTrue(r.main_advice, r.key)
            assert r.main_advice is not None
            self.assertIn(r.main_advice[-1], self._TERMINALS, r.key)

    def test_hard_deny_subagent_advice_is_sentence_plus_suffix(self) -> None:
        rules = guardrail.rules_by_tier(Tier.HARD_DENY)
        self.assertTrue(rules)
        for r in rules:
            # The subagent message is exactly the terminal-punctuated main
            # advice, a single space, then the fixed suffix.
            assert r.main_advice is not None
            assert r.subagent_advice is not None
            expected = r.main_advice + " " + guardrail.SUBAGENT_HARD_DENY_SUFFIX
            self.assertEqual(r.subagent_advice, expected, r.key)
            # The suffix always follows a terminal-punctuated sentence: the
            # char immediately before " " + suffix is one of . ! ?
            marker = " " + guardrail.SUBAGENT_HARD_DENY_SUFFIX
            idx = r.subagent_advice.rfind(marker)
            self.assertGreater(idx, 0, r.key)
            self.assertIn(r.subagent_advice[idx - 1], self._TERMINALS, r.key)
            # The exact old broken join must not appear.
            self.assertNotIn("'rm' You are a subagent", r.subagent_advice, r.key)

    def test_escalate_tail_follows_terminal_punctuation(self) -> None:
        rules = guardrail.rules_by_tier(Tier.ESCALATE)
        self.assertTrue(rules)
        for r in rules:
            assert r.subagent_advice is not None
            marker = " " + guardrail.ESCALATE_TAIL
            idx = r.subagent_advice.rfind(marker)
            self.assertGreater(idx, 0, r.key)
            self.assertIn(r.subagent_advice[idx - 1], self._TERMINALS, r.key)

    def test_all_main_advice_ends_with_terminal_punctuation(self) -> None:
        # Every rule with non-None main_advice (HARD_DENY + ADVISE).
        found = False
        for r in RULES:
            if r.main_advice is not None:
                found = True
                self.assertIn(r.main_advice[-1], self._TERMINALS, r.key)
        self.assertTrue(found)


class AdviceTextTests(unittest.TestCase):
    """The advice text agents actually read, pinned where it has been wrong.

    The git-stash advice used to point at "a temporary branch", which the
    fleet rules forbid outright: work in progress is committed on the branch
    the agent is already on, never parked on a side branch and never stashed.
    """

    GIT_STASH_ADVICE = (
        "Never 'git stash'. Commit the work in progress on the current branch "
        "with 'safegit commit' instead."
    )

    _SIDE_BRANCH_PHRASES = (
        "temporary branch",
        "temp branch",
        "new branch",
        "separate branch",
        "side branch",
        "another branch",
        "different branch",
    )

    def test_git_stash_main_advice_is_exact(self) -> None:
        self.assertEqual(_rule("git-stash").main_advice, self.GIT_STASH_ADVICE)

    def test_git_stash_subagent_advice_is_advice_plus_suffix(self) -> None:
        self.assertEqual(
            _rule("git-stash").subagent_advice,
            self.GIT_STASH_ADVICE + " " + guardrail.SUBAGENT_HARD_DENY_SUFFIX,
        )

    SLEEP_ADVICE = (
        "Never 'sleep' to wait: the harness notifies you when background work "
        "finishes, so read the state you are waiting on or do other work "
        "instead of padding the turn with a wait."
    )

    def test_sleep_main_advice_is_exact(self) -> None:
        self.assertEqual(_rule("sleep").main_advice, self.SLEEP_ADVICE)

    def test_sleep_subagent_advice_is_advice_plus_suffix(self) -> None:
        self.assertEqual(
            _rule("sleep").subagent_advice,
            self.SLEEP_ADVICE + " " + guardrail.SUBAGENT_HARD_DENY_SUFFIX,
        )

    def test_no_advice_sends_an_agent_to_a_side_branch(self) -> None:
        for r in RULES:
            for advice in (r.main_advice, r.subagent_advice):
                if advice is None:
                    continue
                lowered = advice.lower()
                for phrase in self._SIDE_BRANCH_PHRASES:
                    self.assertNotIn(phrase, lowered, f"{r.key}: {advice}")


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

    def test_sleep_matches_command_word_positions_only(self) -> None:
        pat = _rule("sleep").hook_patterns[0]
        for command in (
            "sleep 5",
            "sleep 580; tail x",
            "cmd && sleep 3",
            "(sleep 1)",
            "foo | sleep 2",
        ):
            self.assertIsNotNone(re.search(pat, command), command)
        for command in ("nosleep", "./sleepwalker", "echo sleep", "grep sleep file"):
            self.assertIsNone(re.search(pat, command), command)


class HeavyUnwrappedRuleTests(unittest.TestCase):
    """The heavy-unwrapped rule's model-level contract.

    Pattern behavior under real grep is pinned end to end in
    test_hook_unsafe_exec; this pins what the model itself promises.
    """

    def test_is_hard_deny(self) -> None:
        self.assertIs(_rule("heavy-unwrapped").tier, Tier.HARD_DENY)

    def test_no_deny_glob_can_match_the_wrapped_form(self) -> None:
        # Claude Code matches a Bash(<prefix>:*) glob against the command's
        # leading words, so a glob starting with 'heavy' would refuse the very
        # command the advice tells the agent to run.
        for glob in _rule("heavy-unwrapped").deny_rules:
            self.assertTrue(glob.startswith("Bash(") and glob.endswith(":*)"), glob)
            self.assertFalse(glob.startswith("Bash(heavy"), glob)

    def test_deny_globs_cover_go_toolchain_builds_and_make(self) -> None:
        globs = set(_rule("heavy-unwrapped").deny_rules)
        for glob in (
            "Bash(make:*)",
            "Bash(./make.bash:*)",
            "Bash(./all.bash:*)",
            "Bash(./run.bash:*)",
            "Bash(bash make.bash:*)",
            "Bash(bash all.bash:*)",
            "Bash(bash run.bash:*)",
        ):
            self.assertIn(glob, globs)

    def test_coverage_reason_names_what_the_globs_miss(self) -> None:
        reason = _rule("heavy-unwrapped").coverage_reason
        assert reason is not None
        self.assertIn("make.bash", reason)
        self.assertIn("'make'", reason)

    def test_advice_names_the_command_to_run_instead(self) -> None:
        advice = _rule("heavy-unwrapped").main_advice
        assert advice is not None
        self.assertIn("heavy -- <the command>", advice)
        self.assertIn("--mem", advice)

    def test_advice_sets_the_cap_from_a_measurement_never_a_guess(self) -> None:
        advice = _rule("heavy-unwrapped").main_advice
        assert advice is not None
        # No ready-made cap: an agent copies whatever number it is shown.
        self.assertIsNone(re.search(r"--mem [0-9]", advice), advice)
        self.assertIn("--mem comes from a measurement, never a guess", advice)
        self.assertIn(
            "heavy prints the peak memory use of the command's scope when the "
            "command returns",
            advice,
        )
        self.assertIn(
            "measure it once with the largest cap that fits now, which heavy "
            "names when a cap does not fit",
            advice,
        )
        self.assertIn(
            "A command killed at its cap is a defect to fix at the source, not "
            "a reason for a bigger cap",
            advice,
        )


class SettingsCoverageTests(unittest.TestCase):
    """The settings_coverage annotation is honest and internally consistent.

    settings_coverage records how completely a rule's deny/ask glob(s) cover
    its hook danger surface. It applies only to the hook-backed-with-settings
    tiers (HARD_DENY, ESCALATE); ADVISE (hook, no settings) and ASK (settings,
    no hook) carry None.
    """

    # Independent pins (hand-stated, not derived from the module).
    _EXPECTED = {
        "rm": SettingsCoverage.PARTIAL,
        "git-push-delete": SettingsCoverage.PARTIAL,
        "git-reset": SettingsCoverage.PARTIAL,
        "git-checkout-file": SettingsCoverage.NONE,
        "sleep": SettingsCoverage.PARTIAL,
        "heavy-unwrapped": SettingsCoverage.PARTIAL,
    }

    def test_hook_backed_tiers_annotated(self) -> None:
        # Every HARD_DENY/ESCALATE rule must carry a coverage value.
        for r in RULES:
            if r.tier in (Tier.HARD_DENY, Tier.ESCALATE):
                self.assertIsInstance(r.settings_coverage, SettingsCoverage, r.key)

    def test_advise_and_ask_carry_no_coverage(self) -> None:
        for r in RULES:
            if r.tier in (Tier.ADVISE, Tier.ASK):
                self.assertIsNone(r.settings_coverage, r.key)
                self.assertEqual(r.coverage_reason, "", r.key)

    def test_none_iff_no_settings(self) -> None:
        # For HARD_DENY/ESCALATE: NONE <=> the rule owns no deny AND no ask
        # glob; FULL/PARTIAL => the rule owns at least one settings glob.
        for r in RULES:
            if r.tier not in (Tier.HARD_DENY, Tier.ESCALATE):
                continue
            has_settings = bool(r.deny_rules) or bool(r.ask_rules)
            if r.settings_coverage is SettingsCoverage.NONE:
                self.assertFalse(
                    has_settings,
                    f"{r.key}: NONE coverage but owns settings glob(s)",
                )
            else:
                self.assertTrue(
                    has_settings,
                    f"{r.key}: {r.settings_coverage} coverage but no settings",
                )

    def test_specific_pins(self) -> None:
        for key, expected in self._EXPECTED.items():
            self.assertEqual(_rule(key).settings_coverage, expected, key)

    def test_partial_and_none_have_reasons(self) -> None:
        # PARTIAL/NONE are the non-derivable cases: they must explain what the
        # glob misses (or what covers the rule instead).
        for r in RULES:
            if r.settings_coverage in (
                SettingsCoverage.PARTIAL,
                SettingsCoverage.NONE,
            ):
                self.assertTrue(
                    r.coverage_reason.strip(),
                    f"{r.key}: {r.settings_coverage} needs a coverage_reason",
                )


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
            # Literal backslashes before the parens, exactly as stored in the
            # live profiles' allow arrays.
            r'Bash(sudo -n ufw status || echo "\(need sudo for ufw\)")',
            "Skill(gsd:discuss-phase)",
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
                ("SessionStart", "", "hook-session-start"),
                ("SessionEnd", "", "hook-session-end"),
            ),
        )

    def test_every_wired_script_exists_in_the_registry(self) -> None:
        """No wiring may name a script deploy-hooks cannot deploy."""
        from claudewheel.hook_scripts import HOOK_SCRIPTS

        for _event, _matcher, script in EXPECTED_HOOK_WIRINGS:
            self.assertIn(script, HOOK_SCRIPTS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
