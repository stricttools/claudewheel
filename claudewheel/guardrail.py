"""Canonical guardrail protocol model.

This module is the single source of truth for the fleet-wide command
guardrails: which commands are hard-denied, which escalate to the user when a
subagent tries them, which merely advise, and which prompt via settings. It
carries everything later phases need:

  - Phase 2 (bash generation) reads ``hook_patterns`` plus the tier semantics
    and advice text to emit the PreToolUse/PostToolUse hook scripts.
  - Phase 3 (settings) reads ``canonical_deny_rules()`` / ``canonical_ask_rules()``
    to populate profile ``permissions`` and ``ALLOW_CONFLICTS`` to scrub dead
    or conflicting allow-array entries.
  - Phase 4 (health / patch) reads ``EXPECTED_HOOK_WIRINGS`` to verify each
    profile wires the four hook entries correctly.

The hook regex patterns are stored as PLAIN ERE text (Python raw strings,
single-escaped). Translating them into a bash/grep template (with the extra
layer of shell escaping) is Phase 2's job -- this module never emits bash.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier(Enum):
    """The four guardrail enforcement tiers.

    - HARD_DENY: denied for everyone (main agent and subagents) via a
      PreToolUse hook. A backing settings ``deny`` rule provides defense in
      depth for the cases the hook cannot reach.
    - ESCALATE: denied only when the caller is a subagent; the main agent
      falls through silently so the settings ``ask`` rule prompts the user.
    - ADVISE: PostToolUse advice only. The command runs; the agent is nudged
      afterward via additionalContext. No settings entries.
    - ASK: settings ``ask`` rule only. No hook involvement at all.
    """

    HARD_DENY = "hard_deny"
    ESCALATE = "escalate"
    ADVISE = "advise"
    ASK = "ask"


# Separator anchor shared by every command matcher: start-of-string or a shell
# command separator (``;`` ``&`` ``|`` ``&&`` ``||``) followed by optional
# whitespace. Keeps ``git add`` from matching inside e.g. ``mygit add``.
SEP = r"(^|[;&|]|&&|\|\|)\s*"

# Fixed tail appended to a subagent's HARD_DENY advice.
SUBAGENT_HARD_DENY_SUFFIX = (
    "You are a subagent: report to your parent agent why you attempted this command."
)

# Fixed tail appended after an ESCALATE rule's lead sentence to form the
# message a subagent sees when it is denied.
ESCALATE_TAIL = (
    "Only your parent agent may run this command (the user will be asked to "
    "approve it). Explain in detail to your parent agent why you wanted to run "
    "this command."
)


@dataclass(frozen=True)
class GuardrailRule:
    """One guardrail rule.

    Fields:
      - key: stable identifier, unique across all rules.
      - tier: which enforcement tier this rule belongs to.
      - hook_patterns: PLAIN ERE strings (raw, single-escaped) the hook matches
        against the command. Empty for pure-settings (ASK) rules.
      - deny_rules: settings ``permissions.deny`` array entries this rule
        contributes (HARD_DENY only, and only when it owns a deny rule).
      - ask_rules: settings ``permissions.ask`` array entries this rule
        contributes (ESCALATE and ASK).
      - main_advice: message shown to the main agent (HARD_DENY deny reason,
        ADVISE nudge). ``None`` for ESCALATE (hook is silent for main) and ASK.
      - subagent_advice: message shown to a subagent (HARD_DENY deny reason +
        suffix, ESCALATE escalation message, ADVISE nudge). ``None`` for ASK.
    """

    key: str
    tier: Tier
    hook_patterns: tuple[str, ...] = ()
    deny_rules: tuple[str, ...] = ()
    ask_rules: tuple[str, ...] = ()
    main_advice: str | None = None
    subagent_advice: str | None = None


def _hard_deny(
    key: str,
    patterns: list[str],
    deny_rules: list[str],
    advice: str,
) -> GuardrailRule:
    return GuardrailRule(
        key=key,
        tier=Tier.HARD_DENY,
        hook_patterns=tuple(patterns),
        deny_rules=tuple(deny_rules),
        main_advice=advice,
        subagent_advice=advice + " " + SUBAGENT_HARD_DENY_SUFFIX,
    )


def _escalate(
    key: str,
    patterns: list[str],
    ask_rules: list[str],
    lead: str,
) -> GuardrailRule:
    return GuardrailRule(
        key=key,
        tier=Tier.ESCALATE,
        hook_patterns=tuple(patterns),
        ask_rules=tuple(ask_rules),
        main_advice=None,
        subagent_advice=lead + " " + ESCALATE_TAIL,
    )


def _advise(key: str, patterns: list[str], advice: str) -> GuardrailRule:
    return GuardrailRule(
        key=key,
        tier=Tier.ADVISE,
        hook_patterns=tuple(patterns),
        main_advice=advice,
        subagent_advice=advice,
    )


def _ask(key: str, ask_rules: list[str]) -> GuardrailRule:
    return GuardrailRule(
        key=key,
        tier=Tier.ASK,
        ask_rules=tuple(ask_rules),
    )


# The canonical, ordered list of every guardrail rule.
#
# Order is load-bearing:
#   - It fixes the order of canonical_deny_rules() and canonical_ask_rules().
#   - More specific rules must precede the general rules they overlap so the
#     specific advice fires first when a hook evaluates rules in order:
#       * git-checkout-file BEFORE git-checkout
#       * git-push-delete   BEFORE push
RULES: tuple[GuardrailRule, ...] = (
    # -- HARD_DENY --------------------------------------------------------
    _hard_deny(
        "rm",
        [
            # Anchored rm after a separator/start (incl. end-of-string), plus rm
            # reached indirectly via sudo/env/xargs (allowing flag tokens) or
            # find's -exec/-ok family.
            SEP
            + r"rm(\s|$)"
            + r"|(^|\s)(sudo|env|xargs)\s+(-\S+\s+)*rm(\s|$)"
            + r"|(^|\s)-(exec|execdir|ok|okdir)\s+rm(\s|$)",
        ],
        ["Bash(rm:*)"],
        "Use 'saferm delete --description \"why\" file1 file2' instead of 'rm'",
    ),
    _hard_deny(
        "git-add-bulk",
        # Matches git add -A/-u/-U/--all/. but NOT a plain ``git add file``.
        [SEP + r"git\s+add\s+(-[AuU]|--all|\.)"],
        [
            "Bash(git add .)",
            "Bash(git add -A*)",
            "Bash(git add --all*)",
            "Bash(git add -u*)",
        ],
        "Use 'safegit commit -m \"msg\" -- file1 file2' instead of 'git add'",
    ),
    _hard_deny(
        "git-stash",
        [SEP + r"git\s+stash"],
        ["Bash(git stash:*)"],
        "Use 'safegit commit' on a temporary branch instead of 'git stash'",
    ),
    _hard_deny(
        "git-restore",
        [SEP + r"git\s+restore"],
        ["Bash(git restore:*)"],
        "Use the Edit tool to revert specific lines instead of 'git restore'",
    ),
    _hard_deny(
        "git-checkout-file",
        # The dashdash form ``git checkout -- <path>``. Must be evaluated BEFORE
        # git-checkout so its file-specific advice wins. Owns no settings rule
        # of its own (covered by git-checkout's Bash(git checkout:*)).
        [SEP + r"git\s+checkout\s+--\s"],
        [],
        "Use the Edit tool to revert specific lines instead of 'git checkout -- file'",
    ),
    _hard_deny(
        "git-checkout",
        # All other forms of git checkout.
        [SEP + r"git\s+checkout(\s|$)"],
        ["Bash(git checkout:*)"],
        "'git checkout' is deprecated here; use 'git switch' for branches "
        "(plain git switch is allowed) or the Edit tool to revert files",
    ),
    _hard_deny(
        "git-push-delete",
        # git push ... --delete (and the -d short form / argument-order
        # variants). Must be evaluated BEFORE the ESCALATE push rule so the
        # deletion-specific advice fires first.
        [SEP + r"git\s+push\b.*(--delete|\s-d(\s|$))"],
        ["Bash(git push origin --delete*)"],
        "Deleting remote branches is destructive; ask the user to do this deliberately.",
    ),
    # -- ESCALATE ---------------------------------------------------------
    _escalate(
        "push",
        [SEP + r"(git|safegit|\./safegit)\s+push(\s|$)"],
        [
            "Bash(git push:*)",
            "Bash(safegit push:*)",
            "Bash(./safegit push:*)",
        ],
        "Pushes happen only via rlsbl (rlsbl release run / rlsbl push).",
    ),
    _escalate(
        "git-reset",
        [SEP + r"git\s+reset(\s|$)"],
        ["Bash(git reset *)"],
        "git reset is destructive in shared worktrees.",
    ),
    _escalate(
        "git-switch-force",
        # git switch -f / --force, but NOT a plain ``git switch <branch>``.
        [SEP + r"git\s+switch\s+(-f|--force)(\s|$)"],
        [
            "Bash(git switch -f*)",
            "Bash(git switch --force*)",
        ],
        "Forced switch destroys uncommitted work in shared worktrees.",
    ),
    _escalate(
        "gh-workflow-run",
        [SEP + r"gh\s+workflow\s+run(\s|$)"],
        ["Bash(gh workflow run*)"],
        "Triggering CI workflows is an outward-facing action.",
    ),
    _escalate(
        "saferm-purge",
        [SEP + r"saferm\s+purge(\s|$)"],
        ["Bash(saferm purge:*)"],
        "saferm purge permanently destroys archived files.",
    ),
    _escalate(
        "git-rebase",
        [SEP + r"git\s+rebase(\s|$)"],
        ["Bash(git rebase *)"],
        "Rebase rewrites history in shared worktrees.",
    ),
    _escalate(
        "safegit-rewrite-author",
        [SEP + r"safegit\s+rewrite-author(\s|$)"],
        ["Bash(safegit rewrite-author:*)"],
        "Author rewriting is history rewriting.",
    ),
    # -- ADVISE -----------------------------------------------------------
    _advise(
        "kill",
        # Word-anchored kill/pkill anywhere in a compound command. ``killall``
        # will NOT match (kill must be followed by whitespace/end), but a
        # phrase like ``npm run kill`` WOULD match -- an acceptable false
        # positive for an advice-only nudge.
        [r"(^|\s)p?kill(\s|$)"],
        "This kill/pkill ran, but prefer building graceful stop commands or "
        "PID-file-based stop scripts into your tooling instead of killing "
        "processes directly.",
    ),
    # -- ASK --------------------------------------------------------------
    _ask("sudo", ["Bash(sudo:*)"]),
)


# Allow-array entries the fleet cleanup (Phase 3) must remove because they are
# dead or conflict with the canonical deny/ask rules above. Entries NOT in this
# list stay allowed on purpose -- notably Bash(git rm:*) and Bash(npm run kill:*).
ALLOW_CONFLICTS: tuple[str, ...] = (
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


# The four (event, matcher, script-name) hook wirings every profile must have.
# Phase 4 (health / patch_profiles) verifies these against each profile's
# settings hooks section.
EXPECTED_HOOK_WIRINGS: tuple[tuple[str, str, str], ...] = (
    ("UserPromptSubmit", "", "hook-timestamp"),
    ("PreToolUse", "Agent", "hook-block-worktree"),
    ("PreToolUse", "Bash", "hook-block-unsafe-commands"),
    ("PostToolUse", "Bash", "hook-advise-commands"),
)


def rules() -> tuple[GuardrailRule, ...]:
    """Return every guardrail rule in canonical order."""
    return RULES


def rules_by_tier(tier: Tier) -> tuple[GuardrailRule, ...]:
    """Return the rules belonging to *tier*, in canonical order."""
    return tuple(r for r in RULES if r.tier is tier)


def canonical_deny_rules() -> list[str]:
    """Return the ordered settings ``permissions.deny`` array.

    Collected by walking RULES in order and concatenating each rule's
    ``deny_rules``. The result is a frozen contract (pinned by tests).
    """
    out: list[str] = []
    for rule in RULES:
        out.extend(rule.deny_rules)
    return out


def canonical_ask_rules() -> list[str]:
    """Return the ordered settings ``permissions.ask`` array.

    Collected by walking RULES in order and concatenating each rule's
    ``ask_rules``. The result is a frozen contract (pinned by tests).
    """
    out: list[str] = []
    for rule in RULES:
        out.extend(rule.ask_rules)
    return out


def all_settings_rules() -> list[str]:
    """Return every settings rule (deny then ask), in canonical order."""
    return canonical_deny_rules() + canonical_ask_rules()
