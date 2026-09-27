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
