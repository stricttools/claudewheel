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

Related rewrite facts: the rewrite walks nested files including subagent transcripts
(`<uuid>/subagents/*.jsonl`), which carry a DIFFERENT project's cwd — prefix-correct for
a true prefix rewrite but part of the same audit; `history.jsonl` is skipped by design
("append-only, not critical") so stale paths survive there — include or keep excluding
(ruling); `sessions-index.json` is never rewritten at all, and most of the store's
copies carry a stale `originalPath` from past moves — rule: rewrite it, delete it, or
establish whether the client even reads it.

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
