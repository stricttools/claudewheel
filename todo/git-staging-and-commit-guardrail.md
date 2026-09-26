# Git staging and commit guardrail: policy contradiction, false denials, bulk-add bypasses

## Context

The `git-add-bulk` HARD_DENY in `claudewheel/guardrail.py` is the only guard on how
agents stage and commit. Its pattern is `_cmd(r"git\s+add\s+(-[AuU]|--all|\.)")`,
commented "Matches git add -A/-u/-U/--all/. but NOT a plain `git add file`", and
`tests/test_deploy_hooks.py::test_git_add_should_not_match_specific_files` pins that
scope ("safegit might not be needed"). There is no rule for `git commit`.

Prompting incident (2026-09-27): in a bypass-permissions session, the main agent ran
`git add todo/<file>.md && git commit -m "..."` in a gamehome checkout. The hook let it
through, and in bypass mode nothing prompted. The commit was correct (one file), but it
skipped the `safegit commit -m "..." -- files` workflow that every git deny message
points to, and the user expected the guardrails to prevent raw git usage.

Everything below was measured 2026-09-27 by running the deployed pattern through
`grep -qE` and reading the work profile's `settings.json`.

## The policy contradicts itself

- Deny messages for `git-add-bulk` and `git-stash` say "Use 'safegit commit ...'
  instead of 'git add'" and "commit ... with 'safegit commit'", which reads as
  "never use raw git for staging or committing".
- The shipped changelog (0.19.0) says the hook "blocks raw `git add`". It blocks only
  the bulk forms.
- The test docstring says plain `git add <file>` is deliberately allowed.
- `ALLOW_CONFLICTS` scrubs `Bash(git add:*)` from allow arrays, so outside bypass mode
  a plain `git add <file>` prompts. But the work profile's allow array carries
  `Bash(git commit:*)` (plus `git rm`, `git mv`, `git revert`, `git cherry-pick`,
  `git pull`), so raw commits are pre-approved.
- In bypass-permissions mode (the common unattended mode), ask and allow rules are
  moot, and the hook is the only guard. The effective policy there is "anything except
  the bulk-add spellings".

Ruling needed, one of:

1. **Explicit paths are fine.** Keep raw `git add <path>` and `git commit` allowed.
   Fix the pattern (below), reword the deny message ("stage explicit paths, or use
   safegit commit") and correct the changelog wording.
2. **safegit only.** HARD_DENY every raw `git add` and `git commit` (and probably
   `git commit --amend`, `git rm`, `git mv` given `safegit mv` exists), steering to
   `safegit commit -m "msg" -- files`. Remove `Bash(git commit:*)` (and the others
   chosen) via `ALLOW_CONFLICTS`, and add matching deny globs. This matches the deny
   messages' intent and safegit's design (atomic stage+commit, worktree lock, trailers,
   `safegit undo`), but breaks any tooling or instruction that tells agents to
   `git commit` (grep the fleet's CLAUDE.md files and skills first).
3. **Hybrid.** safegit only for subagents (ESCALATE-style, keyed on `agent_id`, where
   concurrent commits in shared worktrees are the real hazard) and explicit paths for
   the main agent.

## False denials in the current pattern

The trailing `\.` has no boundary, so any path starting with a dot is treated as
`git add .`:

| Command | Result |
|---|---|
| `git add .gitignore` | DENIED |
| `git add .claude/settings.json` | DENIED |
| `git add ./src/a.ts` | DENIED |
| `git add ../other` | DENIED |

This is the same class as the `git stash`/`git restore` trailing-boundary fix of
2026-09-06. The bulk forms `.` and `./` need to be matched as whole arguments
(`\.(/)?(\s|$)`), not as a prefix.

## Bulk-add spellings that bypass

| Command | Result | Why |
|---|---|---|
| `git add -f .` / `git add -N .` | allowed | any flag before `.` defeats the fixed `add\s+(...)` position |
| `git add -v -A` | allowed | same: the bulk flag is not the first argument |
| `git add --update` | allowed | long form of `-u` is not listed |
| `git add -- .` | allowed | `--` before the pathspec |
| `git add :/` / `git add ':(top)'` | allowed | pathspec magic for the whole tree |
| `git add *` | allowed | shell glob; arguably bulk (open ruling) |
| `git -C repo add -A` / `git -c k=v add -A` | allowed | git global options before the subcommand |
| `git commit -a -m m` / `git commit -am m` / `git commit --all` | allowed | bulk staging with no `git add` at all |

Fix direction: match `git add` followed by any argument list containing a bulk token
(`-A`, `-u`, `-U`, `--all`, `--update`, `--no-ignore-removal`, a bare `.`/`./`, `:/`,
`:(top)`, `*`), bounded by the shell separator so tokens from a later command do not
leak in. Add a `git-commit-all` rule for `-a`/`--all` and combined short flags that
include `a` (`-am`, `-av`). Each spelling above becomes a test case, in both the
should-match and should-not-match directions.

## Git global options: a gap for every git rule

Every `_cmd(r"git\s+<sub>...")` rule (`stash`, `restore`, `checkout`, `push`,
`reset`, `rebase`, `switch -f`, `add`) requires the subcommand immediately after
`git`. `git -C <dir> <sub>`, `git -c key=val <sub>`, `git --git-dir=... <sub>`,
`git --work-tree=... <sub>` and `git --no-pager <sub>` all bypass. `git -C` is a normal
agent spelling (it avoids `cd`), so this is not an exotic evasion. Fix once, in a
shared git-verb builder that allows `(\s+(-C|-c)\s+\S+|\s+--\S+)*` between `git` and
the subcommand, and use it for every git rule. This belongs with the matcher overhaul
but is not listed there yet; see below.

## Out of scope here (tracked elsewhere)

Wrappers (`sudo`/`env`/`xargs`/`find -exec`), `$(...)`/backtick/`{` separators,
`do`/`then` bodies, path-prefixed binaries (`/usr/bin/git`), quote-blindness, the
FULL/PARTIAL coverage semantics, safegit verbs lacking guards, and the jq fail-open
are all recorded in `todo/guardrail-hooks-and-permission-rules-overhaul.md`. The
`(git add -A)` subshell and `command git add -A` bypasses measured here are instances
of those. Add the git global-options gap to that todo's matcher list, or do it here
and cross-reference.

## Verification

- Regex matrix tests for every table row above, both directions, run against the
  generated script (not only the Python pattern), as `tests/test_hook_unsafe_exec.py`
  does.
- If ruling 2 or 3: tests that `git commit` and `git add <file>` are denied with the
  safegit message, that `safegit commit -m m -- f` is allowed, and that reconcile
  removes the chosen allow entries.
- After release: `claudewheel deploy-hooks --all --force-overwrite` and
  `reconcile-permissions --dry-run`, then the real run (the standing operational note
  in the overhaul todo).

## Affected files

`claudewheel/guardrail.py`, `claudewheel/hook_scripts.py`, `tests/test_deploy_hooks.py`,
`tests/test_hook_unsafe_exec.py`, `tests/test_guardrail.py`, the docs guardrail table,
and possibly fleet CLAUDE.md files or skills that instruct raw `git commit`.

## Effort

Pattern fixes and tests: S. Git global-options builder across all git rules: S-M.
Ruling 2 or 3 plus the allow/deny array changes and a fleet grep: M.
