# mv: no migration path for orphaned session data under the moved prefix

## Context

`claudewheel mv` is check-then-act: before moving anything it discovers every
project keyed at the old path or nested under it, from two sources — encoded
dir names under each profile's `projects/` and `projects{}` keys in each
profile's `.claude.json`. Because the path encoding is lossy (`/`, `.`, and
literal `-` all encode to `-`), an encoded name is resolved back to a real
path via the known keys plus the directories that actually exist under the
moved tree. Two refusals protect the move:

- an encoded dir with no key and no on-disk directory that decodes to it
  ("cannot safely migrate: ... could not be unambiguously decoded")
- a discovered descendant (typically from a `.claude.json` key) whose
  destination directory will not exist under the new root
  ("descendant project paths have no matching directory ... nothing was migrated")

Both refusals are correct. But a long-lived tree accumulates exactly this
state: subdirectories get deleted or reorganized over months while their
session dirs and project keys persist. Moving such a tree then hard-errors,
and there is no built-in way forward — the operator must either delete the
orphaned session data (losing history) or hand-migrate it.

## Problem

Migrating an orphan is fully deterministic even though decoding is not:

- encoded dir: replace the encoded old-root prefix with the encoded new-root
  prefix (string-level; the lossy tail is preserved verbatim), and rewrite
  the old real-path prefix inside its `*.jsonl` files (skipping
  `history.jsonl`), which is exactly what `run_mv` does for decodable
  projects.
- `.claude.json` key: rename the key by real-path prefix.

There is also a knock-on that makes piecemeal manual fixing tedious: a
`.claude.json` key is often the only decode anchor for its encoded dir, so
fixing keys first newly orphans their dirs (and vice versa an encoded dir fix
can drop a key's corroboration). Each fix round surfaces the next refusal;
a real-world two-tree migration took four dry-run rounds. A built-in should
migrate a key and its encoded dir atomically.

## Proposed solutions

1. **Explicit orphan flag on mv** (e.g. `--migrate-orphans`): after normal
   discovery, any encoded dir or key under the old prefix that is undecodable
   or destination-less is migrated by pure prefix rewrite instead of aborting
   the move. Off by default so the current strict behavior stays the default;
   the flag names the semantics (string-level, no decode guarantee). The
   dry-run lists each orphan with its rewrite so the operator reviews exactly
   what the lossy rename will produce.
   - Pros: one pass, no knock-on rounds, consistent JSONL rewrite with the
     rest of the move; keeps strictness as the default.
   - Cons: a lossy rename can theoretically fold two distinct old paths onto
     one new encoded name — needs the same collision/merge handling
     `_rename_project_dir` already has.

2. **Separate subcommand** (e.g. `c mv-orphans <old> <new>`): standalone
   migration of orphans only, run before a strict `mv`.
   - Pros: keeps `mv` untouched; the operator sequence mirrors what manual
     fixing does today.
   - Cons: two commands for one conceptual move; the knock-on ordering
     problem (key rename un-anchoring its dir) must still be handled inside
     it.

3. **Do nothing; document the manual recipe** in mv's error messages (the
   refusal currently names the orphans but not the way forward).
   - Pros: zero code.
   - Cons: every affected move requires hand-written scripts; the JSONL
     rewrite and expected-count checks are easy to get subtly wrong by hand.

Option 1 fits the existing shape best: discovery already produces the full
orphan list, `_plan_migrations` already orders prefix rewrites safely, and
the JSONL rewrite helper is already there.

## Affected files

- `claudewheel/mv.py` — `_discover_descendants` (return orphans instead of
  raising when the flag is set), `_verify_destinations` (exempt
  flag-migrated orphans), `run_mv` (plan + execute orphan migrations through
  the same rename/rewrite helpers), CLI flag registration.
- Tests: orphan encoded dir (no key, no dir), orphan key (no dir), the
  key-plus-encoded-dir pair migrated atomically, lossy-collision merge.

## Effort

Small-to-medium: the mechanisms (prefix planning, dir rename/merge, JSONL
rewrite, key rename) all exist; the work is routing orphans through them
behind an explicit flag plus tests for the new paths.
