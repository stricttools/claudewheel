# A canonical layer for `.claude.json` preferences

## Context

Every profile directory holds two files Claude Code reads: `settings.json`
(settings; the file `patch-profiles` reconciles to the canonical model and
`health` checks at launch) and `.claude.json` (Claude Code's preferences and
caches: onboarding flags, feature caches, and the toggles the in-session
`/config` panel writes).

The canonical settings model now carries `disableAgentView: true`, which
turns off Claude Code's agent view, `--bg`, `/background`, the on-demand
daemon and the left-arrow gesture that moves a running session into a
background session. That is the switch that matters. But the narrower toggle
for the same gesture, `leftArrowOpensAgents`, lives in `.claude.json`, not in
`settings.json`, and claudewheel has no canonical layer for that file at all:
the toggle is `false` in one profile and absent (meaning enabled) in the
others, because `/config` writes only the current profile's file.

Other preferences of the same kind live there too, for example
`defaultToAgentsView`, and the file also carries state claudewheel must never
touch (caches, onboarding progress, per-project trust).

## Problem

A preference that differs across profiles without anyone deciding it is
drift, and today nothing reports or corrects it. `health` inspects only
`settings.json` keys it knows; `patch-profiles` writes only `settings.json`.
A user who turns a preference off in `/config` gets it off in one profile and
on in the rest, silently.

## Solutions

### A second canonical map, for `.claude.json` (recommended)

Add a canonical preferences map beside `CANONICAL_PROFILE_SETTINGS`, applied
by the same reconcile and checked by the same health loop, but targeting each
profile's `.claude.json`. Initial entries: `leftArrowOpensAgents: false`,
`defaultToAgentsView: false`. The map is an allowlist of keys claudewheel
manages; every other key in the file is left untouched, since most of the
file is Claude Code's own state.

- Pros: the same enforcement model the settings already have; drift becomes a
  launch-time warning and a one-command fix.
- Cons: `.claude.json` is rewritten by Claude Code itself at runtime, so the
  reconcile must write atomically and only when a managed key differs, to
  avoid clobbering a concurrent write by a running session.

### Do nothing beyond `disableAgentView`

With the agent view disabled everywhere, the left-arrow preference is inert.

- Pros: no new code.
- Cons: the preferences file stays unmanaged, and the next preference of this
  kind drifts the same way.

## Affected files

- `claudewheel/defaults.py` (the new canonical map and its seeding into new
  profiles).
- `claudewheel/reconcile.py` (a `.claude.json` reconcile pass with atomic
  writes).
- `claudewheel/health.py` (a check over the managed preference keys).
- `docs/health.md` and the profiles documentation (the managed keys and why).
- Tests mirroring the existing settings tests for reconcile and health.

## Effort

Small to medium: the mechanisms exist for `settings.json`; the work is a
second target file with an allowlist and the atomic-write caveat.
