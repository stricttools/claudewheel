# Guardrail hooks and permission rules: matcher overhaul, new rules, ask-tier disposition

## Context

The canonical guardrail model lives in `claudewheel/guardrail.py`: a `RULES` tuple across
tiers (HARD_DENY, ESCALATE, ADVISE, ASK) from which the bash hook scripts
(`claudewheel/hook_scripts.py`, deployed to `~/.claudewheel/scripts/`), the settings
`deny`/`ask` arrays, the `--disallowedTools` launch list, and the docs table are all
generated. Two hook scripts are hand-written templates outside the model
(`hook-timestamp`, `hook-block-worktree`). Reconciliation makes profile settings exactly
canonical but deploys only MISSING hook scripts, never refreshes changed ones.

Everything below was established 2026-09-06 by direct measurement (regex evaluation
against the shipped patterns, execution of the deployed scripts, reading the installed
Claude Code 2.1.236 binary, and Claude Code's official docs via
`curl https://code.claude.com/docs/llms-full.txt` — note: WebFetch truncates that file and
produces confident false negatives; always curl + grep it locally).

## Established facts: the matcher under-bounds everything

- Only the `rm` and `kill` rules use `_wrapped_matcher` (wrapper coverage for
  sudo/env/xargs and find's `-exec` family). The other hook-bearing rules use `_cmd`,
  which is `SEP + literal` with NO wrapper branch: `sudo git stash`, `xargs git checkout`,
  `env git reset`, `find . -exec git restore {} \;` all bypass today.
- `SEP` omits `$(`, backtick, and `{` as separators; ten rules end in `(\s|$)`, which
  fails against `)`, backtick, and `}` — so `$(git checkout)` and `` `git push` `` bypass
  even if `SEP` is widened. Both ends must be fixed together.
- No path-prefix allowance: `/bin/rm`, `/usr/bin/git` bypass every rule. A measured probe
  showed a `([^\s]*/)?` prefix group introduces zero false positives (it requires a
  literal `/` immediately before the command word).
- `do`/`then`/`else` bodies bypass everything (`for f in *; do rm $f; done`). These are
  words, not punctuation — they need a keyword alternation, with a different
  false-positive profile than punctuation (open decision below).
- `_wrapped_matcher`'s wrapper-argument allowance is `(-\S+\s+)*` (flag-only):
  `env FOO=1 rm x`, `sudo -u root rm x`, `timeout 5 rm x`, `nice -n 10 rm x` bypass.
  Widening to arbitrary tokens admits false positives such as `sudo tar -xf a.tar rm `
  (open decision: blanket allowance vs a per-wrapper arity table).
- The matcher is quote-blind: a `|` or `;` inside a quoted string reads as a separator, so
  `grep -n "a\|git stash"` is hard-denied though nothing runs `git stash`. Accepted as a
  known cost for deny-tier rules, but record it; nested shells (`bash -c '...'`, `eval`)
  are unreachable by any regex and need an explicit in-scope/out-of-scope ruling.
- Measured bypass rates on the live patterns: the `rm` HARD_DENY is bypassed by 18 of 23
  ordinary shell forms tested; the 13 `_cmd`-built rules by 22 of 23 (only the bare form
  fires).
- The "no rule hand-writes a raw pattern" invariant (comment near the pattern builders)
  is enforced by nothing — add a test asserting every `hook_patterns` entry is
  builder-produced, or a future hand-written rule silently escapes any central fix.
- All eight `SettingsCoverage.FULL` annotations are false even for whitespace variants
  (`git\tstash` matches the hook, no literal glob covers it), none is verified by a test,
  and the FULL definition contradicts the Tier docstrings on whether compound/wrapper
  forms count. After the matcher fix every FULL becomes definitively PARTIAL. Decide:
  redefine FULL as "plain command form only" or downgrade all eight, and add a
  verification test either way.
- `git stash`/`git restore` trailing boundaries were fixed 2026-09-06 (`git stashed` no
  longer denied). The stash rule still denies the read-only `stash list`/`stash show`;
  open ruling: narrow to mutating subcommands, or keep the blanket ban and reword the
  message to say stash is banned as a workflow including inspection. Note rlsbl refuses
  to release while a stash exists, and diagnosing that currently requires the blocked
  `git stash list`.
- With `jq` absent from PATH, the generated blockers' `tool_name=$(... | jq ...)` yields
  empty, the not-Bash test fires, and every blocking hook exits 0 silently — all
  guardrails off (measured). Undeclared dependency; decide whether a missing `jq`
  should refuse loudly instead, and how (the scripts run on machines claudewheel does
  not control).

## Established facts: the safegit surface

- `safegit rewrite-author` became a removal stub in safegit 0.19.0 (2026-06-29): it
  parses, prints "use 'safegit author rewrite' instead", exits 1, does nothing. The model
  guarded only that stub until 2026-09-06, when the rule was replaced by an ESCALATE
  guard on `safegit author rewrite` (both `safegit` and `./safegit` spellings). No
  removal of the old spelling is scheduled in safegit's record.
- Still unguarded, measured against the model's own regexes: the `safegit scrub` group
  (`file`/`match`/`run` — rewrites history up to `--entire-history`; `scrub verify` is
  read-only and must stay unguarded), `safegit backup backup` (a remote force-with-lease
  push to `refs/backups/<branch>`; the push rule requires `push` immediately after
  `safegit`, so the intervening token defeats it), `safegit undo` (moves refs;
  `--bypass-session` reaches other sessions' operations), and `safegit reset`/`safegit
  rebase` (the `git-reset`/`git-rebase`/`git-switch-force` rules match only the `git`
  binary while the `push` rule matches `git|safegit|./safegit`).
- Proposed tiers by analogy (each needs a ruling): `scrub file|match|run` ESCALATE or
  HARD_DENY (more destructive than the rebase it would mirror; a HARD_DENY on bare
  `safegit scrub` would not block `rlsbl release scrub`, which is the sanctioned door in
  rlsbl-managed repos — different command string); `undo --bypass-session` ESCALATE with
  plain `undo` left free (it is the documented recovery path) or the whole verb;
  `backup backup` ESCALATE; `doctor --action uninstall` and `hook remove` ESCALATE;
  `commit --amend`, `config set`, `mv` at ADVISE or unguarded.
- Structural fix over per-rule patching: nothing ties a rule's binary alternation to the
  set of binaries implementing the verb. Introduce one shared binary-alternation
  authority used by every verb rule that has a safegit twin, so this gap class cannot
  recur. Also: nothing re-derives the guard surface when safegit gains verbs
  (`backup`, `merge`, `pull`, `cherry-pick`, `scrub` all postdate the model) — consider a
  check that diffs safegit's `--help` verb tree against the verbs the model mentions.

## Established facts: Claude Code's permission machinery (2.1.236, docs + binary)

- Parameterized rules `Tool(param:value)` are documented and supported (since v2.1.178;
  `Agent(type)` enforcement for named spawns fixed in v2.1.186): deny and ask ONLY — the
  matcher has zero allow call sites, so a parameterized allow rule is inert. Values
  support `*`; only top-level scalar input fields match; primary content fields
  (`command`, `file_path`, `path`, `url`, `notebook_path`) are excluded by design.
- The documented spelling for blocking fork subagents is the agent-type rule
  `Agent(fork)` (hides the type from the listing, errors naming the rule).
  `Agent(subagent_type:fork)` is legal but undocumented. Open ruling: which spelling, or
  a PreToolUse hook (better message, sees nested fields, but can fail open).
- `Agent(run_in_background:true)` can never fire in an ordinary interactive session:
  with fork mode default-on the Agent schema omits `run_in_background` entirely, and an
  absent key never matches. The field exists only under `claude -p`/non-interactive or
  `CLAUDE_CODE_FORK_SUBAGENT=0`. Drop the idea or use a hook on something that exists.
- `agent_id` in hook payloads is populated for foreground subagents, background agents,
  AND forks; absent only on the main thread. So the ESCALATE tier (fires when `agent_id`
  non-empty) already covers forks — if forks should instead count as main-thread-like
  (they inherit the parent's whole context), the discriminator is `agent_type == "fork"`.
- Hook decision protocol: a JSON decision object on stdout (permissionDecision
  allow/deny/ask/defer) with exit 0, OR exit 2 with the message on STDERR (stdout is
  ignored on the exit-2 path). Malformed JSON with exit 0 FAILS OPEN (decision
  discarded; schema-invalid → hook error event, syntax-invalid → treated as plain-text
  success). `defer` is print-mode-only and single-tool-call-only — unusable for
  interactive guardrails; recorded here so nobody re-investigates.
- Built-in tool inputs are zod-strip parsed (model-sent unknown keys are gone before
  hooks or rules see them); MCP tool inputs are passthrough unless the server declares
  `additionalProperties: false`.
- The hook payload carries `cwd`, so repository-scoped rules (see the bare-`mv`
  interception below) are feasible via a generator preamble.
- Managed-settings lockdown: `allowManagedPermissionRulesOnly: true` makes Claude Code
  ignore allow/ask/deny in user-scope settings.json — exactly where claudewheel writes
  its rules — while `--disallowedTools` on the launch argv survives (reliably since
  v2.1.257). `strictPluginOnlyCustomization` with "hooks" and `allowManagedHooksOnly`
  similarly disable user-scope hooks. Open ruling: do nothing / detect-and-report in
  health / carry the deny content on the launch flag.
- Shell-parsing bypasses in Claude Code's own rule/hook matching (compound commands,
  subshells) were fixed across 2.1.243-2.1.259; a health check could assert a minimum
  client version for guardrail-bearing profiles.

## New rules wanted (each needs its ruling before build)

- **Fork ban** — spelling ruling above; then one canonical deny entry plus tests.
- **sed ban** — deny vs ADVISE vs none. A command-position matcher already exists and
  passed a 33-case matrix (ERE and PCRE forms, path-prefixed spellings included; known
  evasions: nested shells, variable indirection). The fleet rulebook treats sed as
  dry-run-disciplined rather than banned — the ruling is whether the hook goes further.
- **Bare `mv` in repos** — safegit now ships `safegit mv 'old -> new'` (one subtree
  record per directory pair; refuses sources with uncommitted content edits, exit 19, no
  override; `safegit undo` reverses the commit but leaves files at new paths). The hook
  design: parse plain `mv` invocations, use the payload `cwd` plus
  `git -C <dir> rev-parse` to classify tracked-same-repo (block, print the exact
  `safegit mv` replacement), tracked-cross-repo (block, explain the two-step), untracked
  or non-repo (allow). Unparseable compounds containing an mv token: block with
  "rephrase as a plain mv or use safegit mv" — refusal on ambiguity, since a
  regex-only guard misses in the dangerous direction.
- **Flags-before-subcommand argv shape** — pure policy ruling, deliberately left open
  when filed (2026-08-11): adopt / reject-and-close / scoped to fleet tools. The
  structural fix at the framework's own allowlist has since shipped; the remaining
  question is only whether claudewheel's hook additionally bans the shape.
- **v1.x tag creation, `--yes` anywhere, `go install ...@latest`, `pip install`, writes
  into `/tmp`** — all blocked on prerequisites or rulings: `--yes` and `/tmp` need
  unbounded substring patterns that break the builder invariant (ruling); `/tmp` also
  needs a tool axis (rules currently match Bash only — the generated scripts exit early
  for other tools) and a settings-only deny tier (HARD_DENY structurally requires a hook
  pattern; a `Write(//tmp/**)` glob has no hook half); `pip install` conflicts with the
  standing editable-install instruction (`pip install -e ... --break-system-packages`) —
  exempt `-e` or make it ADVISE; `@latest`: enumerate fleet module paths in the model vs
  a generic ban. All of these see only hand-typed commands — a tool that shells out
  internally is invisible to hooks — so first ruling: is partial coverage worth shipping?
- **Model extensions** implied above: a tool axis (non-Bash matchers; would also let
  `hook-block-worktree` be generated from the model instead of hand-written — it was
  converted to the safe jq emission form and given behavior tests on 2026-09-06, but
  remains outside the model), a settings-only deny tier, and AskUserQuestion rules
  (fields are nested, so only a hook can see `multiSelect`/`preview`; whether a
  PreToolUse deny on AskUserQuestion surfaces its reason usefully is unverified).

## Per-hook disable switch

Wanting: one place that turns a named hook off for all profiles, inside the canonical
model. Options analyzed: a `disabled_hooks` config list filtering `EXPECTED_HOOK_WIRINGS`
(global, granular; softens the exactly-canonical contract), the same with blocking hooks
refused by name, tier-based disabling (wrong axis), or writing Claude Code's
`disableAllHooks` (rejected — set outside managed settings it does NOT disable hooks; it
flips the session to managed-hooks-only and also kills the statusLine and fileSuggestion
helpers). Also on the table: no switch at all, because a project-level
`.claude/settings.json` already serves as the per-repo extension/override point that
exact reconciliation never touches.

## Ask-tier disposition

The ask tier (11 entries, identical across profiles) is not the whole guard: seven
entries are the settings half of ESCALATE rules whose hook half fires only for subagents
(`agent_id` non-empty) — retiring ask wholesale leaves the MAIN agent unguarded on
push/reset/rebase/switch-force/gh-workflow-run/saferm-purge/author-rewrite, and
`Bash(sudo:*)` (settings-only, no hook) with zero enforcement. Ruling: retire wholesale,
retire only hook-backed entries, or keep. Related contradiction to resolve in the same
ruling: the GitHub-credentials todo once proposed putting `gh` write commands UNDER ask.

## Allow-array policy and validation

- `ALLOW_CONFLICTS` is hand-maintained and matched by exact string membership. The
  dead-because-stripped class (allow entries naming a tool in the strip list) is fully
  derivable from `disallowed_tool_names()` with zero upkeep; the
  conflicts-with-deny/ask class is not cleanly derivable (needs glob subsumption plus a
  keep-list for the deliberate keepers such as `Bash(git rm:*)`). Recommended: derive
  the first class, keep the second declared, and route both match sites (reconcile,
  health) through one comparison function.
- The two big profiles carry hundreds of allow entries each (one profile has none).
  Open: is allow per-profile organic state or canonical? A shadowing audit (exact allows
  beaten by prefix ask/deny rules — one such found and pruned already) and a staleness
  check (allow rules whose leading binary is not installed; a couple dozen found
  2026-08/09) are candidate health additions.
- One allow entry in two profiles embeds a live Google Maps API key inside the rule
  string — rotate the key and strip the entry (tracked in the housekeeping todo).
- A validation script idea from an earlier filing survives: extract command-prefix
  patterns from all settings files and validate each against the named CLI's
  `--dump-schema`/help output, for repeat use after upstream renames. The one-off audit
  it proposed was performed 2026-09-06 (all patterns verified live except the safegit
  one, since fixed).

## Standing operational note

Model changes do not reach live profiles by themselves: reconcile updates settings
arrays on the next run of the INSTALLED claudewheel, and hook scripts additionally need
`claudewheel deploy-hooks --all --force-overwrite`. After the next release the operator
step is that deploy plus `reconcile-permissions --dry-run`, then the real run. Whether
reconcile should redeploy content-drifted scripts automatically (today: warn-only
health, deploy only missing) is an open ruling in the settings-authority todo.

## Affected files

`claudewheel/guardrail.py`, `claudewheel/hook_scripts.py`, `claudewheel/defaults.py`,
`claudewheel/reconcile.py`, `claudewheel/health.py`, `tests/test_guardrail.py`,
`tests/test_hook_unsafe_exec.py`, `tests/test_hook_advise_exec.py`,
`tests/test_hook_worktree_exec.py`, `tests/test_shared_settings.py`, docs guardrail table.

## Supersedes

pretool-hook-ban-sed.md, git-guard-blocks-readonly-stash.md, mv-guardrail-hook-safemv.md,
pretooluse-hook-flags-before-subcommand-ban.md, disable-fork-subagent-type.md,
global-per-hook-disable-switch.md, retire-ask-tier-permissions.md,
dead-permission-rules.md (its fix shipped 2026-09-06; its audit was performed; the
validation-script idea and the live-file deploy step live on here).

## Effort

The matcher overhaul is one focused session once its three rulings are made (wrapper
breadth, keyword separators, FULL semantics). Each new rule is small on its own; the
tool axis and settings-only tier are the two model extensions of substance.
