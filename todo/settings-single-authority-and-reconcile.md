# shared-settings.json as single authority; reconcile and health unification

## Context

Today the canonical settings model lives in code (`defaults.build_canonical_shared_settings`,
`guardrail.py`); `shared-settings.json` is a derived artifact that reconcile rewrites
from code, and per-profile `settings.json` files are made exactly canonical for a fixed
key set (`hooks`, the claudewheel-namespaced disallowed-tools list, `permissions`
deny/ask exact, allow pruned of declared conflicts). The launch preflight runs this
reconcile on every launch inside a blanket exception swallow. An owner-decided design
(recorded 2026-08-11) inverts the authority: the FILE becomes the single authority for
all settings keys, code canonical becomes bootstrap-only, every launch diffs all
profiles against shared and resolves drift interactively.

## The decided design (owner decisions, 2026-08-11; the one marked [rec] was an
accepted recommendation and is more weakly held)

1. shared-settings.json becomes the single authority for ALL settings keys, guardrails
   included. Code canonical materializes the file when absent, never overwrites it.
2. Every key is shared; no profile-owned keys, no exclusion list. Profiles are
   credentials plus nothing.
3. Every launch checks ALL profiles against shared.
4. Interactive drift resolution [rec]: per differing key — apply shared to profile,
   promote profile to shared, or leave for this launch.
5. Headless launches abort on drift with a hard error naming the keys.
6. The reconcile report becomes visible; the exception swallow goes away.
7. No tamper protection beyond a warning key at the top of the file.
8. profileDefaults and the wizard seeding path are superseded and deleted.
9. Set-semantics comparison for order-meaningless lists.
10. permissions.allow churn: prompt with batch-promote; revisit auto set-union if noisy.

Open riders from that design: rename profile → account (needs explicit owner approval;
spans the two downstream consumers of the profile API), and where per-profile reconcile
metadata lives if any is needed.

## Evidence updates (2026-09-06) that the implementation must absorb

- Empirical profile state moved: 17 of 21 top-level keys byte-identical across the three
  profiles; differing keys are `autoCompactEnabled`, `enabledMcpjsonServers`, `model`,
  and `permissions` (allow arrays: two profiles identical with hundreds of entries, one
  empty). `remoteControlAtStartup` no longer appears anywhere.
- Two peer-messaging keys (`crossSessionInbound: "refuse"`, `isolatePeerMachines: true`)
  exist ONLY in the live files (hand-added 2026-08-12) and nowhere in code — a
  regenerated shared file or a new profile silently loses them. The
  session-capability-stripping todo owns their adoption; this design must not delete
  them in the interim.
- Settings-`env` propagation is verified working: a top-level `"env": {...}` in a
  profile's own settings.json is applied by Claude Code (capture-tested byte-identical
  to setting the process env var). shared-settings.json itself never reaches Claude
  Code (no symlink, no `--settings` flag; only the wizard seed and reconcile consume it).
- Reconcile robustness fixes shipped 2026-09-06/07: non-dict nested containers
  (`profileDefaults`/`claudewheel`/`permissions` as null) no longer crash; per owner
  ruling they are a loud per-target error (skip with reason naming file and key), never
  silently normalized. A sibling gap remains: a non-LIST value inside permissions
  (`"deny": null`) still breaks the apply step — needs the list-level counterpart of the
  same guard in `permission.add_rule`/`remove_rule`.

## Health-vs-canonical disagreements to eliminate (all verified by probe)

The same fact is declared in multiple places with different values; the reduction is one
authority with everything else derived:

- `cleanupPeriodDays`: canonical writes 3650; health accepts >= 365; the health test
  fixture uses 365; two docs pages state each value. A profile at 365 passes health and
  is never corrected.
- `permissions.disableAutoMode == "disable"`: required by health, written only by the
  wizard, absent from canonical entirely. It is a real client key, distinct from
  `defaultMode` (binary-verified) — the fix is adding it to canonical, deriving the
  health check, deleting the wizard's extra write.
- `includeGitInstructions`: in canonical, checked by nothing (mirror image).
- `disallowedTools`: health checks a SUBSET (extras invisible), reconcile enforces list
  equality — a profile with an extra entry passes health and is silently rewritten next
  launch. One shared comparison predicate should serve both.
- `check_shared_settings_drift` reads its expectations from the FILE that reconcile
  itself writes from code — after one reconcile it is redundant with the code-model
  checks; against a hand-edited file it blames the wrong side. But it is currently the
  ONLY exactness check on the profile side for hooks and the disallowed list, so repoint
  it at the code model rather than retiring it first.
- Order: reconcile enforces order for `hooks` (compare by ==) and the disallowed list,
  but only set-membership for deny/ask; health compares string lists as sets and its
  dict-list branch can detect a difference yet emit zero diff lines (reordered hooks:
  rewritten by reconcile, invisible to health). Decide whether exact-canonical includes
  order, then encode it in the one shared predicate.
- Cross-cutting: four of the six `check_settings_defaults` assertions name
  `patch-profiles` as the remedy, but reconcile cannot write scalar keys — the remedy is
  false. The fork: reconcile gains a scalar-keys pass for profileDefaults' non-permission
  keys, or health sheds the unrepairable checks. This decision shapes items 1-3 above.

## Silent-degradation items owned here

- The launch preflight wraps the whole reconcile in a blanket exception swallow and
  discards the change report it computes. Under the decided design both go away (the
  report becomes the drift prompt); until then, even loud per-target errors are
  invisible at launch. Do not fix piecemeal without deciding the design's timing.
- Reconcile prunes user-added hook entries and permission rules by design (exactness),
  with nothing retained. Consent is handled (the two CLI commands are consequential);
  recovery is not. Open ruling: should the exact reconcile archive or emit what it
  prunes? Under the inverted design, hand edits to shared become legal and the pruning
  concern moves to the per-profile side only.
- Hook SCRIPT content is exempt from exactness: reconcile deploys only missing scripts,
  a claudewheel upgrade leaves old blocker scripts running, detection is warn-only
  health pointing at a manual force-overwrite. Ruling: should reconcile make script
  content exact too?
- profile_info displays the same scalar keys with no notion of canonical (fourth
  reader). Fold into the derivation.

## Affected files

`claudewheel/reconcile.py`, `claudewheel/preflight.py`, `claudewheel/defaults.py`,
`claudewheel/guardrail.py`, `claudewheel/health.py`, `claudewheel/wizard.py`,
`claudewheel/config.py`, `claudewheel/permission.py`, `claudewheel/profile_info.py`,
`claudewheel/cli.py`, `claudewheel/ui.py`, docs guardrail/health pages; tests:
`test_reconcile*.py`, `test_shared_settings.py`, `test_preflight.py`, `test_health.py`,
`tests/wheelhelpers.py`.

## Supersedes

shared-settings-single-authority.md (the decided design above is carried from it),
reconcile-silently-prunes-user-hooks.md (its exception-handling half was partially
addressed by the loud-error work of 2026-09-07; its pruning question is the retention
ruling above; its unforgeable-file option is superseded by the inverted design unless
the owner revives it).

## Effort

Medium-large: the inversion is a full working session; the health/canonical unification
is a second focused pass that should ride the same release so the two reference points
never coexist.
