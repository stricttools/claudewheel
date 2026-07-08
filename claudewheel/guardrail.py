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


def _as_sentence(text: str) -> str:
    """Ensure *text* ends with terminal sentence punctuation.

    Strips trailing whitespace; if the result is non-empty and its last
    character is not one of ``.``/``!``/``?``, appends a period. Only the final
    character matters -- internal punctuation (e.g. ``;``) is left untouched.
    Idempotent for text that already ends in terminal punctuation.
    """
    text = text.rstrip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _hard_deny(
    key: str,
    patterns: list[str],
    deny_rules: list[str],
    advice: str,
) -> GuardrailRule:
    main_advice = _as_sentence(advice)
    return GuardrailRule(
        key=key,
        tier=Tier.HARD_DENY,
        hook_patterns=tuple(patterns),
        deny_rules=tuple(deny_rules),
        main_advice=main_advice,
        subagent_advice=main_advice + " " + SUBAGENT_HARD_DENY_SUFFIX,
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
        subagent_advice=_as_sentence(lead) + " " + ESCALATE_TAIL,
    )


def _advise(key: str, patterns: list[str], advice: str) -> GuardrailRule:
    advice = _as_sentence(advice)
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


# ---------------------------------------------------------------------------
# Pattern builders -- anchoring by construction.
#
# No rule hand-writes a raw or weakly-anchored ERE pattern. Every command
# matcher is built from one of these so the SEP anchor (and, where relevant,
# the wrapper alternation) is applied uniformly and cannot be forgotten.
# ---------------------------------------------------------------------------


def _cmd(literal: str) -> str:
    """SEP-anchor a command-matcher *literal*.

    Prepends the shared separator anchor so the matcher only fires when the
    literal begins a shell command (start-of-string or after a ``;``/``&``/``|``
    separator), never mid-token (e.g. ``mygit add`` must not match ``git add``).
    """
    return SEP + literal


def _wrapped_matcher(cmd: str) -> str:
    """SEP-anchored matcher for *cmd* plus indirect-invocation wrappers.

    Emits three OR-joined branches so *cmd* is caught whether it is invoked
    directly or reached through a common wrapper:

      - the SEP-anchored bare form (``cmd`` at start / after a separator);
      - via ``sudo``/``env``/``xargs`` (allowing any leading ``-flag`` tokens);
      - via ``find``'s ``-exec``/``-execdir``/``-ok``/``-okdir`` actions.

    *cmd* is an ERE fragment (e.g. ``rm`` or ``p?kill``); each branch requires
    it to be followed by whitespace or end-of-string so ``rmdir``/``killall``
    do not match.
    """
    tail = r"(\s|$)"
    return (
        SEP + cmd + tail
        + r"|(^|\s)(sudo|env|xargs)\s+(-\S+\s+)*" + cmd + tail
        + r"|(^|\s)-(exec|execdir|ok|okdir)\s+" + cmd + tail
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
        # Anchored rm after a separator/start (incl. end-of-string), plus rm
        # reached indirectly via sudo/env/xargs (allowing flag tokens) or
        # find's -exec/-ok family. Built by _wrapped_matcher so the anchor and
        # wrapper alternation are applied by construction.
        [_wrapped_matcher("rm")],
        ["Bash(rm:*)"],
        "Use 'saferm delete --description \"why\" file1 file2' instead of 'rm'",
    ),
    _hard_deny(
        "git-add-bulk",
        # Matches git add -A/-u/-U/--all/. but NOT a plain ``git add file``.
        [_cmd(r"git\s+add\s+(-[AuU]|--all|\.)")],
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
        [_cmd(r"git\s+stash")],
        ["Bash(git stash:*)"],
        "Use 'safegit commit' on a temporary branch instead of 'git stash'",
    ),
    _hard_deny(
        "git-restore",
        [_cmd(r"git\s+restore")],
        ["Bash(git restore:*)"],
        "Use the Edit tool to revert specific lines instead of 'git restore'",
    ),
    _hard_deny(
        "git-checkout-file",
        # The dashdash form ``git checkout -- <path>``. Must be evaluated BEFORE
        # git-checkout so its file-specific advice wins. Owns no settings rule
        # of its own (covered by git-checkout's Bash(git checkout:*)).
        [_cmd(r"git\s+checkout\s+--\s")],
        [],
        "Use the Edit tool to revert specific lines instead of 'git checkout -- file'",
    ),
    _hard_deny(
        "git-checkout",
        # All other forms of git checkout.
        [_cmd(r"git\s+checkout(\s|$)")],
        ["Bash(git checkout:*)"],
        "'git checkout' is deprecated here; use 'git switch' for branches "
        "(plain git switch is allowed) or the Edit tool to revert files",
    ),
    _hard_deny(
        "git-push-delete",
        # git push ... --delete (and the -d short form / argument-order
        # variants). Must be evaluated BEFORE the ESCALATE push rule so the
        # deletion-specific advice fires first.
        [_cmd(r"git\s+push\b.*(--delete|\s-d(\s|$))")],
        ["Bash(git push origin --delete*)"],
        "Deleting remote branches is destructive; ask the user to do this deliberately.",
    ),
    # -- ESCALATE ---------------------------------------------------------
    _escalate(
        "push",
        [_cmd(r"(git|safegit|\./safegit)\s+push(\s|$)")],
        [
            "Bash(git push:*)",
            "Bash(safegit push:*)",
            "Bash(./safegit push:*)",
        ],
        "Pushes happen only via rlsbl (rlsbl release run / rlsbl push).",
    ),
    _escalate(
        "git-reset",
        [_cmd(r"git\s+reset(\s|$)")],
        ["Bash(git reset *)"],
        "git reset is destructive in shared worktrees.",
    ),
    _escalate(
        "git-switch-force",
        # git switch -f / --force, but NOT a plain ``git switch <branch>``.
        [_cmd(r"git\s+switch\s+(-f|--force)(\s|$)")],
        [
            "Bash(git switch -f*)",
            "Bash(git switch --force*)",
        ],
        "Forced switch destroys uncommitted work in shared worktrees.",
    ),
    _escalate(
        "gh-workflow-run",
        [_cmd(r"gh\s+workflow\s+run(\s|$)")],
        ["Bash(gh workflow run*)"],
        "Triggering CI workflows is an outward-facing action.",
    ),
    _escalate(
        "saferm-purge",
        [_cmd(r"saferm\s+purge(\s|$)")],
        ["Bash(saferm purge:*)"],
        "saferm purge permanently destroys archived files.",
    ),
    _escalate(
        "git-rebase",
        [_cmd(r"git\s+rebase(\s|$)")],
        ["Bash(git rebase *)"],
        "Rebase rewrites history in shared worktrees.",
    ),
    _escalate(
        "safegit-rewrite-author",
        [_cmd(r"safegit\s+rewrite-author(\s|$)")],
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


# ---------------------------------------------------------------------------
# Bash hook-script generation (Phase 2)
#
# These functions turn the canonical model above into the actual bash sources
# deployed as Claude Code hooks. They are the ONLY place in the codebase that
# emits bash. ``hook_scripts.py`` imports the generated strings at module load;
# this module must NEVER import ``hook_scripts`` (that would be a cycle).
# ---------------------------------------------------------------------------


def _bash_squote(s: str) -> str:
    """Return *s* wrapped as a bash single-quoted literal (incl. the quotes).

    Single quotes inside *s* are handled with the standard ``'\\''`` splice so
    the result is safe to drop verbatim into a bash script. ERE metacharacters
    (backslashes, pipes, dollar signs) are preserved literally because single
    quotes suppress all shell interpretation -- exactly what grep -qE needs.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _bash_dquote_body(s: str) -> str:
    """Escape *s* for embedding INSIDE a bash double-quoted string.

    Returns only the body (no surrounding quotes). Escapes the four characters
    bash still interprets inside double quotes: backslash, backtick, dollar,
    and double quote. jq (via ``--arg``) handles JSON escaping downstream, so
    the message reaches Claude Code with its quotes intact.
    """
    return (
        s.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace('"', '\\"')
    )


def _match_condition(rule: GuardrailRule) -> str:
    """Build the shell test that matches *rule*'s command patterns.

    Each pattern becomes a ``printf ... | grep -qE '<pat>'`` pipeline; multiple
    patterns are OR-joined with ``||`` so the rule fires if ANY pattern hits.
    """
    parts = [
        'printf \'%s\' "$command" | grep -qE ' + _bash_squote(pattern)
        for pattern in rule.hook_patterns
    ]
    return " || ".join(parts)


_BLOCKER_HEADER = '''\
#!/usr/bin/env bash
# PreToolUse hook that blocks unsafe commands and suggests safe alternatives.
#
# GENERATED by claudewheel.guardrail.generate_blocker_script() from the
# canonical guardrail model. Do NOT edit by hand -- edit guardrail.py and
# regenerate instead.
#
# Reads JSON from stdin (Claude Code's PreToolUse payload). For Bash tool calls
# it matches the command string against the guardrail patterns:
#   - HARD_DENY rules deny for everyone (main agent and subagents).
#   - ESCALATE rules deny ONLY subagents; the main agent falls through silently
#     so the settings ask rule prompts the user.
# The agent_id field is present in the payload only for subagent calls, so a
# non-empty agent_id is how the hook tells a subagent apart from the main agent.

set -uo pipefail

input=$(cat 2>/dev/null || true)
[[ -z "$input" ]] && exit 0

tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" != "Bash" ]] && exit 0

command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -z "$command" ]] && exit 0

agent_id=$(printf '%s' "$input" | jq -r '.agent_id // empty' 2>/dev/null)

deny() {
    # Build the JSON with jq so the reason string is escaped correctly.
    # Hand-interpolating $1 into JSON breaks when the message contains quotes,
    # producing unparseable output that Claude Code silently discards.
    jq -cn --arg reason "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $reason}}'
    exit 0
}
'''


def generate_blocker_script() -> str:
    """Emit the full bash source for the ``hook-block-unsafe-commands`` hook.

    Walks the canonical model twice -- HARD_DENY rules first, then ESCALATE
    rules -- each in RULES order (ordering is load-bearing: the more-specific
    rule must precede the general rule it overlaps). HARD_DENY denies everyone
    (subagent advice when agent_id is set, main advice otherwise); ESCALATE
    denies only subagents and lets the main agent fall through.
    """
    lines: list[str] = [_BLOCKER_HEADER]

    for rule in rules_by_tier(Tier.HARD_DENY):
        assert rule.main_advice is not None
        assert rule.subagent_advice is not None
        lines.append(f"# {rule.key} (HARD_DENY)")
        lines.append(f"if {_match_condition(rule)}; then")
        lines.append('    if [[ -n "$agent_id" ]]; then')
        lines.append(f'        deny "{_bash_dquote_body(rule.subagent_advice)}"')
        lines.append("    else")
        lines.append(f'        deny "{_bash_dquote_body(rule.main_advice)}"')
        lines.append("    fi")
        lines.append("fi")
        lines.append("")

    for rule in rules_by_tier(Tier.ESCALATE):
        assert rule.subagent_advice is not None
        lines.append(f"# {rule.key} (ESCALATE, subagent-only)")
        lines.append(
            f'if [[ -n "$agent_id" ]] && ( {_match_condition(rule)} ); then'
        )
        lines.append(f'    deny "{_bash_dquote_body(rule.subagent_advice)}"')
        lines.append("fi")
        lines.append("")

    lines.append("exit 0")
    return "\n".join(lines) + "\n"


_ADVISE_HEADER = '''\
#!/usr/bin/env bash
# PostToolUse hook that nudges the agent after certain commands run.
#
# GENERATED by claudewheel.guardrail.generate_advise_script() from the
# canonical guardrail model. Do NOT edit by hand -- edit guardrail.py and
# regenerate instead.
#
# Reads JSON from stdin (Claude Code's PostToolUse payload). For Bash tool
# calls whose command matches an ADVISE pattern, it emits additionalContext
# advice. The command has already run; this only nudges. Emits nothing (empty
# stdout, exit 0) when no pattern matches.

set -uo pipefail

input=$(cat 2>/dev/null || true)
[[ -z "$input" ]] && exit 0

tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null)
[[ "$tool_name" != "Bash" ]] && exit 0

command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)
[[ -z "$command" ]] && exit 0

advise() {
    # Build the JSON with jq so the advice string is escaped correctly.
    jq -cn --arg ctx "$1" '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
    exit 0
}
'''


def generate_advise_script() -> str:
    """Emit the full bash source for the ``hook-advise-commands`` hook.

    Walks the ADVISE-tier rules in RULES order. Each matching rule emits its
    advice as additionalContext via jq and exits. Non-matching commands produce
    empty stdout and exit 0.
    """
    lines: list[str] = [_ADVISE_HEADER]

    for rule in rules_by_tier(Tier.ADVISE):
        assert rule.main_advice is not None
        lines.append(f"# {rule.key} (ADVISE)")
        lines.append(f"if {_match_condition(rule)}; then")
        lines.append(f'    advise "{_bash_dquote_body(rule.main_advice)}"')
        lines.append("fi")
        lines.append("")

    lines.append("exit 0")
    return "\n".join(lines) + "\n"
