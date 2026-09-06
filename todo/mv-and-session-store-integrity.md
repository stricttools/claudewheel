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

## LIVE BUG, highest priority here: the un-bounded JSONL rewrite

`_rewrite_jsonl_file` performs a plain substring replace of the old real path on every
line. A path that is a string prefix of a SIBLING project's path therefore rewrites the
sibling's occurrences inside this project's transcripts. Measured on the real store
2026-09-06: fourteen sibling pairs under the projects root where one name prefixes the
other, and one project's transcripts contained 147 occurrences of such a sibling's
path — a rename of that project would have silently falsified all of them into a path
that never existed. Reachable from the plain command AND from two [y/N] prompts during
a normal `--resume`/`--cont` launch; the dry run prints only aggregate counts, so the
preview cannot reveal it. The anchored discipline already exists in the same module
(`_rewrite_prefixed_path` tests `path == old or path.startswith(old + "/")`) — it was
simply never applied to JSONL bodies.

Owner ruling needed on the boundary rule before the fix:
(a) parse each line as JSON and rewrite only known path-bearing fields (`cwd` and kin) —
    correct, but narrows what gets fixed (paths inside free text stay stale);
(b) boundary-tested string replace (accept the old path only when followed by `/`, `"`,
    whitespace, or end-of-token) — cheap, preserves current reach;
(c) both: structural fields via (a), free text via (b).
Red test first either way: a JSONL line carrying a prefix-sibling path, asserted
untouched (mirror the existing `githubRepoPaths` near-miss test).

Related rewrite facts: the rewrite walks nested files including subagent transcripts
(`<uuid>/subagents/*.jsonl`), which carry a DIFFERENT project's cwd — prefix-correct for
a true prefix rewrite but part of the same audit; `history.jsonl` is skipped by design
("append-only, not critical") so stale paths survive there — include or keep excluding
(ruling); `sessions-index.json` is never rewritten at all, and most of the store's
copies carry a stale `originalPath` from past moves — rule: rewrite it, delete it, or
establish whether the client even reads it.

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

## Store repair and recovery

- The encoder fix means dirs encoded under the OLD rule (underscore projects) now
  decode — but a store-repair pass over existing dirs was deliberately NOT written.
  Measured 2026-09-06: dozens of store dirs (48 of 294) had underscore-bearing real
  paths and were unreachable by claudewheel's own name computation. Rule whether a
  one-time repair/verify pass ships (it may now be unnecessary — decode goes through
  matching, not exact reconstruction; verify before building anything).
- `run_mv` has no cross-step recovery: the real-dir rename, the store-dir renames +
  JSONL rewrites, and the registry-key renames are three independent loops; an error in
  the middle leaves the registry pointing at a path that no longer exists. `--post-hoc`
  completes an interrupted run (verified) and the refusal now names it; a written
  in-progress marker (the profile-rename breadcrumb pattern is the in-repo precedent)
  would make recovery mechanical instead of documented. Ruling: marker or message-only.
- Minor guard, noted not built: a registry file whose top level is not a JSON object
  still fails later with a raw AttributeError; the hard-error helper could refuse it
  by name.

## Affected files

`claudewheel/mv.py`, `claudewheel/shared_store.py`, `claudewheel/cli.py` (interception
flows), tests `tests/test_mv.py`, `tests/test_shared_store.py`.

## Supersedes

mv-orphan-migration.md.

## Effort

The boundary fix is small once ruled and is the urgent piece. The orphan design is a
focused session (decode source + destination relaxation + tests over fixture stores).
Repair pass and marker are small follow-ons after their rulings.
