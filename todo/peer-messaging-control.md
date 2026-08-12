# Peer-messaging control: disable cross-session messaging as a claudewheel feature

## Context

Claude Code 2.1.224 (2026-08-07) added local cross-session messaging:
every session binds a unix socket (`$XDG_RUNTIME_DIR/cc-socks/<pid>.sock`),
registers it in `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json`, and gains two
tools — `SendMessage` (message any discovered session) and `ListAgents`
(enumerate live peers). The feature is on with nothing to enable, has no
dedicated off switch (verified by binary inspection of 2.1.226: the only
related env var can only force it ON; the flag-override env var is dead
code), and subagents inherit both tools by default.

Two delivery behaviors compound it: inbound messages are injected into
the recipient's running turn (no allowlist), and a receiving session in
bypass-permissions mode delivers immediately when the sender *claims*
bypass mode — an attribute that is self-asserted and verified against
nothing. Sessions launched by claudewheel typically run bypass, so
delivery is silent in practice.

## The incident (anonymized)

On a machine running several claudewheel-launched sessions on unrelated
work: an orchestration session spawned a task subagent; the subagent hit
a blocker, used `ListAgents` to enumerate live sessions, guessed which
one "owned" the code it was blocked on, and broadcast a technical report
to at least two unrelated sessions — injected mid-turn into each, with
no prompt on either end. The guess was wrong in every case. The
subagent's own parent session never saw the outgoing message (subagents
send autonomously), and the subagent then stalled itself waiting for a
notification mechanism that does not exist. The recipients burned tokens
reading and reasoning about a foreign project's bug report; one replied;
the misroute took three sessions' attention to untangle. The blast
radius was limited to one profile only because claudewheel gives each
profile its own real `sessions/` directory — an accidental benefit worth
making deliberate.

## Problem

There is no upstream way to turn this off at launch, and the pieces that
do work are scattered across three different mechanisms a user must know
about and hand-edit per profile. This is exactly the kind of composite
launch-time posture claudewheel exists to own. A subagent in one window
being able to message sessions doing separate work in other windows
should be impossible by default, not prevented by per-user folklore.

## Verified levers (client 2.1.226)

- `disallowedTools` (shared-settings → `--disallowedTools` on every
  launch): removes tools from the schema entirely, main session and
  subagents, mechanically. Proven live with the existing entries.
- `"crossSessionInbound": "refuse"` in a profile's `settings.json`:
  inbound messages dropped before reaching the model. Needed even with
  the tools removed machine-wide — any same-uid process can write to
  the socket without being a Claude session. (Cosmetic residue: the
  socket still binds and the session still appears in peers' listings;
  upstream territory.)
- `"isolatePeerMachines": true`: forces approval for off-machine sends
  even under bypass.
- PreToolUse hook branching on `agent_id` / `tool_input.to`: the only
  way to allow main-session sends while denying subagent or off-session
  sends (`permissions.deny` cannot scope to subagents).

## Proposed feature

A `peer_messaging` key with three values, materialized by the same
reconcile path that already deploys shared settings and hooks:

- `"off"` — `SendMessage` + `ListAgents` into `disallowedTools`,
  `crossSessionInbound: "refuse"` + `isolatePeerMachines: true` into
  every profile's settings.
- `"main-only"` — tools stay; deploy a PreToolUse hook denying
  `SendMessage`/`ListAgents` when `agent_id` is present (subagent
  caller) and denying `SendMessage` when the recipient is not one of
  the session's own agents; inbound stays on. For users who want
  main-to-main coordination but never subagent sends.
- `"on"` — upstream behavior, no intervention.

### Placement options

- **A. Global key in shared settings (recommended), default `"off"`.**
  One declared intention, every profile, new profiles inherit it.
  Pro: matches the incident lesson (default-deny); zero per-profile
  bookkeeping. Con: a genuinely mixed-use machine needs the per-profile
  override below anyway.
- **B. Per-profile key with a global default.** Pro: profiles that
  legitimately coordinate can opt up to `"main-only"`/`"on"` while the
  rest stay off. Con: slightly more config surface. A + B compose: a
  global default plus per-profile override is the complete shape.
- **C. Do nothing in claudewheel; document the manual edits.** Rejected
  by the requester: this is posture, and posture is claudewheel's job.

Also worth doing regardless of option: document that per-profile
`sessions/` directories are a deliberate isolation boundary (they
already limit discovery to same-profile sessions) so a future
"centralize sessions into shared/" cleanup doesn't silently widen the
blast radius.

## Affected areas (verify against current code)

- Shared-settings schema + whatever validates/reconciles it
- The settings/hook deployment path into per-profile `settings.json`
- Launch flag assembly (`--disallowedTools`)
- Profile scaffolding defaults for new profiles
- Docs (README/profile docs): the new key, the three modes, and the
  isolation-boundary note

## Effort

Small: the mechanisms (disallowedTools passthrough, hooks propagation,
settings reconcile) all exist; this is one key fanning out into them,
plus the hook script for `"main-only"` and docs. The hook script is the
only new code of substance.
