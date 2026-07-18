# mv: migrate sessions of nested (descendant) project dirs, and rewrite githubRepoPaths

## Context

`claudewheel mv OLD NEW` renames a project directory and migrates its session data. Claude Code stores per-project data in flat sibling dirs named by an encoded absolute path (`SharedStore.encode_path`, `shared_store.py:46-49`: `/`→`-`, `.`→`-`), plus path-keyed entries in each profile's `.claude.json`. Because the encoding flattens the hierarchy, a project at `OLD/child` is a *sibling* encoded dir, not something physically inside `OLD`'s project dir.

## Problem

When the moved directory CONTAINS other CC project dirs (moving a parent/workspace dir, reorganizing a tree), `mv` silently leaves every descendant's session data stale:

1. **Exact-match-only key logic.** The `projects/` dir rename acts on a single `old_project = projects / old_encoded` (`mv.py:194-232`); `.claude.json` uses a plain dict membership test on `old_path` (`_update_claude_json`, `mv.py:105-113`). No prefix/descendant handling exists anywhere in `mv.py` (grep for `startswith`/nested/descendant: nothing). Observed in practice: moving a parent updated 0 keys while 15 descendant project keys stayed stale; each required a manual per-child `claudewheel mv --post-hoc OLD/child NEW/child`.
2. **JSONL cwd rewrite never reaches children.** `_rewrite_jsonl_file` (`mv.py:54-86`) is substring-based and *would* catch descendant paths, but `mv.py:240-244` scans only the parent's own renamed dir — descendants' JSONL files live in sibling dirs that are never scanned.
3. **`githubRepoPaths` is never rewritten at all** — for any move, exact or nested, even with `--post-hoc`. `grep githubRepoPaths` over `claudewheel/*.py` returns zero hits. Repo→local-path mappings pointing into a moved tree stay stale forever. Observed: after a tree reorganization, 6 stale `githubRepoPaths` entries remained across profiles after all project keys had been fixed.
4. **Nothing acknowledges the case.** No flag, doc (`docs/cli-mv.md`, `docs/claudewheel-mv.md`), code comment, or test (`tests/test_mv.py` covers only single exact-dir renames/merges) mentions nested projects.

Not affected (correctly path-agnostic, need no changes): the UUID-keyed stores `session-env/`, `file-history/`, `tasks/`, `todos/`, `paste-cache/` (`migrate.py:68-85`).

## Solutions

### A. Prefix-aware migration in mv itself (most correct)

For `OLD → NEW`, enumerate every path-keyed entry with `key == OLD or key.startswith(OLD + os.sep)` and migrate each:

- `projects/` encoded dirs in every profile + shared dir. Prefix-match on *decoded/real* paths, then re-encode — the encoded string is ambiguous (`-` collapses both `/` and `.`, so `foo-bar` and `foo/bar` encode identically) and cannot be safely prefix-matched directly.
- `.claude.json` `projects{}` keys: exact + descendants.
- `githubRepoPaths`: rewrite any path entry equal to or under `OLD` (this sub-fix is warranted independently, even for exact moves).
- JSONL cwd rewrite: feed all affected descendant `projects/<encoded>` dirs into `scan_dirs`, not just the parent's.
- Destination resolution: do NOT assume lockstep `NEW + suffix` — a child may have been relocated to a different destination than the parent (child moved out separately before/after the parent move). Resolve each stale key's true on-disk destination via the existing inode map (`check_inode_renames`, `health.py:619-651` / `inodes.json`) and fall back to `NEW + suffix` only when the suffix path actually exists there.
- Ordering: process longest paths first to prevent prefix shadowing (same pattern as `import_.py:67-69`); keep existing merge-on-collision behavior (`mv.py:198-225`) per child.

Pros: parent moves become safe and complete; matches user intuition of "move this tree". Cons: the largest change; needs careful tests (nested, diverged-destination child, collision merge).

### B. Detect-and-refuse (hard error) plus per-child instructions

Before moving, scan for descendant project keys; if any exist, error out listing the exact per-child `--post-hoc` commands to run after a manual move. Pros: small, honest, no silent staleness; fits the hard-errors-over-warnings philosophy. Cons: pushes the work onto the caller; githubRepoPaths still needs its own fix.

### C. Detect-and-migrate via health/post-hoc sweep

Extend `check_inode_renames` (`health.py`) to also detect stale descendant keys and stale `githubRepoPaths`, and have a repair command fix them inode-backed. Pros: also heals moves done outside claudewheel. Cons: reactive rather than atomic with the move; mv stays silently incomplete until the sweep runs.

Recommendation: A, with C's inode-backed resolution as the destination oracle; B's descendant scan is a cheap first commit that removes the silent-corruption window immediately.

In all cases add tests: nested child under moved parent, child with diverged destination, githubRepoPaths rewriting (exact and nested), encoded-path ambiguity (sibling dir named like a child).

## Affected files

- `claudewheel/mv.py` (`run_mv` 119-279, `_update_claude_json` 89-116, `_rewrite_jsonl_file` 54-86)
- `claudewheel/shared_store.py` (`encode_path` 46-49 — ambiguity constraint)
- `claudewheel/health.py` (`check_inode_renames` 619-651 — destination oracle)
- `claudewheel/cli.py` (632-648 mv wiring)
- `tests/test_mv.py`, `docs/cli-mv.md`, `docs/claudewheel-mv.md`

## Effort

Medium. Option B alone: small (half a day with tests). Option A fully (prefix-aware over real paths + githubRepoPaths + child JSONL scan + inode-backed destinations + ordering/collision tests): 1-2 days.
