# Session capability stripping: skills, peer messaging, file-mention picker, remote control

## Context

claudewheel strips tools via the `--disallowedTools` launch flag (note: a top-level
`disallowedTools` key in settings.json is NOT a client key — the client ignores it; the
flag is the real mechanism, and it is also the only rule surface that survives a
managed-settings lockdown). Bundled skills, slash commands, peer messaging, the `@`
file-mention picker, and Remote Control are separate surfaces with their own settings
keys, currently outside the canonical model. All client facts below verified against
Claude Code 2.1.236 (binary) and the official docs (2026-09-06); the reconcile core
manages only hooks, the claudewheel-namespaced tool list, and permissions — adding any
key below requires extending the canonical model (defaults, reconcile, health, wizard).

## Skills and commands

- `disableBundledSkills: true` removes bundled skills/workflows from the model's view,
  but built-in slash commands stay TYPABLE (they become user-invocable-only) — the
  earlier belief that they are removed entirely was wrong. `/doctor` survives separately
  (needs `skillOverrides: {"doctor": "off"}` or `DISABLE_DOCTOR_COMMAND`; it became a
  bundled skill in v2.1.205).
- `skillOverrides` has FOUR values since v2.1.129: `on`, `name-only` (listed without
  description — a cheap budget lever), `user-invocable-only`, `off`. Since v2.1.199
  `off` also hides from Remote Control and SDK listings. Plugin skills are exempt.
- Open ruling: blanket `disableBundledSkills` vs surgical per-skill `off` — the blanket
  kills `/deep-research` (a sandboxed workflow program) and `/loop` (a front for the
  cron tools the strip list deliberately keeps). Decide the survivors first.
- Related keys, all real: `enableArtifact` (false-from-any-scope wins, v2.1.242;
  removing the Artifact tool also matters to the temp-directory question in the
  attachments todo), `disableRemoteControl`, `disableSkillShellExecution`,
  `skillListingMaxDescChars`/`skillListingBudgetFraction` (direct context-cost levers).

## Peer messaging

- Live state: `crossSessionInbound: "refuse"` and `isolatePeerMachines: true` were
  hand-added to every profile and shared file on 2026-08-12 and exist in NO code — a
  regenerated file or new profile silently loses them. Adoption into the canonical
  model is the main work item.
- Client facts: values are `accept`/`hold`/`refuse`; unset means mode parity
  (auto-deliver only when the sender's permission-mode class matches; an unclassed
  sender is held only while this session bypasses prompts). Stricter values win over
  managed — these keys are designed to be declared. `isolatePeerMachines` forces an ask
  on cross-machine sends even under bypass and cannot be auto-approved.
- Outbound: deny rules for `SendMessage` and `ListAgents` are consulted normally
  (`ListPeers` is a registered alias of `ListAgents` in the rule canonicalizer — a rule
  written either way resolves the same). The documented full-shutdown recipe is exactly
  deny both + inbound refuse. TRAP, verbatim from the docs: denying `SendMessage` also
  removes messaging to one's OWN subagents and teammates — orchestration that resumes
  subagents by message would break. Resolve that conflict before adopting the deny.
- Asymmetry worth encoding in tests: background agents keep `SendMessage` but lose
  `ListAgents`.
- Mode question: is a "main-only" mode (hook denying sends when `agent_id` is present)
  wanted, or just off/on? Global default with per-profile override is the composed shape.
- The per-profile `sessions/` directories are a deliberate isolation boundary (they
  scope peer discovery to same-profile sessions); document that so a future
  centralization does not widen it silently.

## File-mention picker (ruled: kill it)

Owner ruling 2026-09-06: neuter the `@` picker. Mechanism verified in the binary: a
custom `fileSuggestion: {"type": "command", "command": "true"}` early-returns past the
built-in file index, and empty stdout yields no suggestions — no popup. A broken helper
also yields none (no index fallback). Submit-time `@path` content injection is part of
the attachments pipeline and dies with the attachments kill switch (see the attachments
todo — ordering dependency, not a blocker). Implementation is a canonical-model
extension like the other keys here. Caveat: the mechanism rests on verified-but-
undocumented internals; re-verify on client upgrades.

## Remote Control and adjacent

`disableRemoteControl` works from merged settings despite docs wording. Whether it joins
the canonical model is part of the same which-keys-join decision. Version floors for all
keys here are recoverable from the client changelog, not the settings docs — pin the
floors in whatever health check asserts them.

## Affected files

`claudewheel/defaults.py`, `claudewheel/guardrail.py` (if keys join the model proper),
`claudewheel/reconcile.py`, `claudewheel/health.py`, `claudewheel/wizard.py`, docs;
tests for the canonical contract.

## Supersedes

skill-command-stripping-and-session-governance.md (its scheduled-jobs inventory and
session-forensics items moved to launcher-instrumentation.md), peer-messaging-control.md.

## Effort

Medium. The mechanics all exist (flag passthrough, reconcile, health); the work is the
model extension plus the rulings: blanket-vs-surgical skills, which keys join the model,
the SendMessage conflict, and the main-only question.
