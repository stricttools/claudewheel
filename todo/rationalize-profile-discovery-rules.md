# Rationalize profile discovery qualification rules

## Context

Profile discovery encodes three asymmetric qualification rules (today in
`discovery.discover_profiles()`; after the Workspace/ProfileStore refactor, in the
ProfileStore enumeration — this todo applies to wherever the rules live when picked up):

1. The "default" profile (`~/.claude`) qualifies ONLY if `.credentials.json` exists.
   A `~/.claude` containing `settings.json` but no credentials file is invisible.
2. A `profiles/<name>` dir qualifies via `.credentials.json` OR `settings.json`.
3. A tokens.json entry alone NEVER qualifies a profile — the dir must also exist.
   A token entry whose dir is missing is silently invisible, with no warning anywhere.

During the Workspace/ProfileStore refactor these rules were deliberately preserved
exactly as-is (parity-tested), to avoid conflating a behavior change with an
architecture change. This todo is the deferred follow-up.

## Problem

The rules are internally inconsistent (rule 1 vs rule 2 treat the same evidence
differently) and rule 3 creates a silent-invisibility case: a stale or orphaned
tokens.json entry simply vanishes from every listing with no diagnostic, which
contradicts the no-silent-degradation principle.

## Proposed solutions

1. **Preserve + surface anomalies.** Keep enumeration behavior identical, but expose
   anomalies (token entry with no dir; `~/.claude` with settings but no credentials)
   as structured findings that the health command reports.
   - Pros: zero behavior change for the TUI and resolution; the silent cases become
     diagnosable; small addition to health.
   - Cons: the asymmetry itself remains.

2. **Rationalize the rules.** Unify qualification: default qualifies like any other
   dir (credentials OR settings); a token entry with no dir becomes either a visible
   broken profile or a hard error.
   - Pros: one consistent rule set; most correct long-term.
   - Cons: changes which profiles the TUI shows and what programmatic resolution
     accepts; needs its own test delta and a changelog entry (user-facing).

3. **Both, staged:** ship (1) first to make anomalies visible, gather whether any
   real workspace actually hits them, then ship (2) with confidence.
   - Most correct overall.

## Affected files

- Profile enumeration (post-refactor: the ProfileStore module; pre-refactor:
  `claudewheel/discovery.py`).
- `claudewheel/health.py` — anomaly reporting (option 1/3).
- Parity tests that pin the current rules (they intentionally fail on any rule
  change and must be updated deliberately).

## Effort

Option 1: small (~1-2 hours with tests). Option 2: medium (~half a day including
TUI verification and changelog). Option 3: sum of both, staged across releases.
