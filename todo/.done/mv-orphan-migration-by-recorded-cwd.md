# mv and the shared session store: rewrite safety, orphan migration, store repair

## Context

`claudewheel mv` renames a project directory and migrates session data: encoded project
dirs under the shared store, real-path strings inside session JSONL files, and the
per-profile registry (`.claude.json` `projects{}` keys). Path encoding is lossy (`/`,
`.`, `_`, `-` all encode to `-`), so encoded names are decoded by matching against known
registry keys and real directories. Fixed 2026-09-06/07 and already committed: the
encoder now collapses `_` like Claude Code does; an unreadable registry file is a hard
error naming the file (both discovery and update passes); the two launch-interception
migration calls report errors instead of crashing the launch; an interrupted move's
refusal names the working completion command (`claudewheel mv --post-hoc OLD NEW`).

## Orphan migration (the old proposal, corrected by measurement)

Undecodable encoded dirs abort the whole move today. The old proposal (a flag doing
blind prefix rewrites) is REFUTED for the dangerous class: a blind rewrite cannot tell
a child from a sibling and would move another project's data. What the measurements
support instead:

- The store's session JSONL lines carry a top-level `cwd` field naming the real path
  (present on most user/assistant lines; NOT reliably on line one; absent in a few
  metadata-only files). Measured store-wide 2026-09-06: of 130 orphan dirs, 123 had a
  single consistent top-level cwd; 1 had conflicting cwds; 6 had no source at all
  (empty shells). Restriction that must hold: read TOP-LEVEL `*.jsonl` only — never
  recurse into `subagents/` (their cwd belongs to other projects; recursing produced
  thousands of false mismatches in the measurement).
- Design: add the cwd as a THIRD decode source in `_discover_descendants`, consulted
  only when registry keys and filesystem decoding both fail (strictly additive; no
  currently-resolving candidate changes answer). The decoded path flows into the
  existing child-vs-sibling test unchanged. On conflicting cwds: hard-error like the
  existing ambiguity branch — never majority-vote (the one real conflicting dir's
  majority is the WRONG path). Open rulings: fallback-only (recommended) vs
  always-cross-check; and what the 6 no-source shells do (permanent hard error on any
  ancestor move, vs a skip — noting a skip flag is an escape hatch under the fleet's
  no-escape-hatch rule).
- The decode source alone does not unblock the real cases: many orphans are orphans
  BECAUSE their source directory was deleted (session data outliving the project), and
  `_verify_destinations` then refuses for a different reason (destination directory
  will not exist). Ruling: a descendant whose session data exists but whose source dir
  is gone should have its store dir renamed anyway (so resume works under the new path)
  without requiring an on-disk directory. The two functions must change together.
