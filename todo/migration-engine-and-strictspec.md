# Versioned-config migrations: engine fixes and the strictspec adoption path

STATUS: the strictspec adoption itself is PARKED by owner ruling (2026-09-06) — not in
the current workstream. The engine facts and small fixes below stand on their own; the
adoption section preserves everything learned so the future effort starts warm.

## Engine facts (verified 2026-09-06, several by experiment)

- One GLOBAL `_schema_version` lives in config.json while the five migrations mutate
  OTHER documents (one touches segments.json, four touch options.json). Consequence,
  proven by experiment: `reset-options` deletes options.json, the counter still says
  latest, so regeneration SKIPS all migrations — correct today only because the shipped
  defaults happen to equal the post-migration shape (an injected sixth migration showed
  a fresh install and a post-reset regeneration producing different files from the same
  code). Remedies to choose among: per-document version stamps; `reset-options` also
  resetting the counter; or a test enforcing that shipped defaults always equal the
  post-migration shape (the invariant that currently holds by coincidence).
- The `theme` parameter threaded through every migration is dead — no migration ever
  used it, and it alone motivates the two-pass deep-copy machinery in the runner.
  Wired-but-never-used: decide delete (simplifies the engine materially) or keep.
- A fresh install writes config.json TWICE (once at version 0 from defaults, once at
  latest after replaying no-op migrations); shipping the counter's default at the
  latest migration number (or deferring the first write) removes the churn. Verified:
  fresh-install output is otherwise byte-stable across repeated constructions.
- Corrupt-file policy: the loaders silently fall back to defaults on an unparseable
  config/options/state file, and the next write makes the loss permanent (pinned values
  are the unrecoverable part). The by-identity aliasing bug in that fallback was fixed
  2026-09-06 (deep copies now); the POLICY question — keep silent fallback vs refuse
  naming the file — remains open and interacts with the loud-malformed-value stance the
  reconcile now takes.

## The strictspec adoption path (parked; constraints established from its spec)

- strictspec's bootstrap contract requires a PER-CONSUMER one-time conversion script
  that stamps `format_version` into every existing document — stamp, never reshape,
  refuse ambiguous inputs; deliberately no CLI command and no shared stamper. The
  version marker is read only from the document's own root: an external counter in a
  different file has no standing.
- The counter-truthfulness hazard: copying the global counter into a per-document
  marker is only honest if every counter increment corresponded to a migration of THAT
  document. Prerequisite audit: record, per historical version number, which document
  its migration mutated; any increment that skipped options.json makes a blind copy a
  false stamp — exactly what "refuse ambiguous inputs" forbids.
- strictspec's migration op set is closed (thirteen ops; no op may compute a new value
  from an existing value; predicates are equality/presence only). Any historical
  migration that computed values must be restated as literal set/merge operations —
  audit the five for this before planning a port.
- Seeding sites per unversioned document: state.json (constructor, beside the existing
  migration calls); shared-settings.json (its ensure-step writes only when absent — an
  existing file needs a second branch to receive the marker; reconcile leaves
  non-guardrail keys alone so the marker survives); the shared inode map (its writer
  early-returns on the common unchanged path — the marker write must precede that); the
  per-profile token file has NO clean seeding site (stamping means rewriting a secret
  during a read) — the honest exception is emit-on-write with unmarked entries accepted
  as legacy until next write, chosen explicitly rather than defaulted into.
- Scope guard from the original filing, still correct: only wholly-owned documents.
  Per-profile settings.json follows the client's schema on the client's schedule and is
  never versioned by this repo.

## Affected files

`claudewheel/config.py`, `claudewheel/appdata.py`, `claudewheel/defaults.py`,
`claudewheel/state.py`, `claudewheel/profile_data.py`, `claudewheel/shared_store.py`,
tests `tests/test_migration.py`, `tests/test_appdata.py`.

## Supersedes

adopt-strictspec-for-owned-configs.md.

## Effort

Engine fixes: small, mostly rulings. Adoption: medium — the audit, the seeder, the
migration restatement, and the counter retirement are real work in the prescribed order
(audit first, seed second, retire the global counter only after every install has been
converted).
