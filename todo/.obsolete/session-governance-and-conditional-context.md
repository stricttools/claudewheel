# Session governance and conditional context — what claudewheel must deliver

## Context

This is a handoff from a design session whose goal is to move enforcement and
context-provisioning OUT of a large, always-loaded global rules file and INTO
claudewheel's mechanical layer, plus a set of session-hardening configuration
changes. The payoff: rules gain real enforcement (a block at the moment of the
mistake instead of prose the agent may skim past), the global rules file shrinks
to only what has no invocation moment, and per-tool guidance stops drifting
across dozens of scaffold-generated copies.

Every item below is independently shippable. Some may already have partial or
overlapping todos in this directory (candidates the design session was told
about: an attachments-env-var todo, a peer-messaging-control todo, a
fork-subagent-type todo, a harness-scratchpad todo). Deduplicate at your
discretion — duplicates are expected and fine.

**Adoption discipline that applies to every item:** the requester wants each
piece BUILT and TESTED in the repo, but NOT deployed into live profiles or used
to replace global-rules text until they approve that specific adoption. Build,
add tests, stage; do not flip anything on for real sessions without explicit
sign-off. The one exception already executed: the stale
`hook-block-unsafe-commands` deployed script was force-overwritten this session
to drop a dead `rlsbl push` mention, so that drift is already resolved.

All binary-level facts below were established against the installed Claude Code
build 2.1.236 (cross-checked 2.1.233 and 2.1.241). Claude Code auto-updates, so
re-verify the load-bearing internals against the current build before relying on
them; where a fact rests on undocumented internals it is flagged as such.

---

## Item 1: CONDITIONAL CONTEXT (new feature — the centerpiece)

### The idea

Fleet authoring tools (rlsbl, selfdoc, strictcli, and any other opt-in dev tool)
each ship one or more pairs of files:

- a CONDITION: a bash or python script (author's choice per pair), and
- a CONTEXT: a markdown file.

The pairs are copied into
`~/.claudewheel/conditional-context/<contributing-tool>/<pair-name>.{sh,py,md}`.

At session launch, claudewheel runs every condition script with exactly one
input — the session's cwd — and reads exactly one output — a boolean. For each
condition that returns true, claudewheel concatenates that pair's markdown into
the session's system prompt, wrapped in framing that names the satisfied
condition, e.g. "This directory is a project whose releases are managed by
rlsbl" or "This project uses selfdoc for its docs." The markdown gives an
instant overview of the tools that govern this directory plus breadcrumbs
(call `<tool> --help`, read `<file>`) rather than full documentation.

### Why this is the right shape

- Single authority per tool: the guidance lives in ONE machine-wide file per
  tool, updated when the tool updates, instead of N drifting copies scaffolded
  into every repo.
- Immune to the attachments kill switch (Item 2): it is launch-time system-prompt
  injection, not part of Claude Code's per-turn attachment pipeline.
- Breadcrumbs over bodies: the session pays tokens only for a pointer and learns
  where depth lives.
- It is the agreed home for the per-tool builder-guidance that is being trimmed
  from the global rules file (e.g. strictcli's registration-time conventions,
  rlsbl's release invariants): the condition is "this project depends on / is
  managed by <tool>", the context is the guidance.

### Open design decisions (resolve WITH the requester before building — do not
pick silently; these change user-visible behavior)

1. **Registration path** — how pairs travel from a tool's repo into
   `~/.claudewheel/conditional-context/`. Options: a claudewheel command the
   tool calls at install/scaffold time; a step in each tool's own install
   mechanism; or symlinks into the tool checkouts (symlinks + editable installs
   would keep context automatically current, matching the fleet's always-latest
   posture). Recommend surfacing these with trade-offs.
2. **Condition contract** — cwd delivered how (argv vs stdin), boolean returned
   how (stdout `true`/`false`, or exit status), and — the real tension — what a
   BROKEN condition script does. The fleet philosophy is hard-error-not-warn,
   but claudewheel's launch preflight convention is never-block-the-launch
   (the guardrail reconcile is wrapped in a swallow-all handler on purpose).
   These pull opposite ways; the requester must rule on whether a failing
   condition aborts the launch, is skipped loudly, or is skipped silently.
3. **Injection mechanism** — `--append-system-prompt` on the launched `claude`
   invocation (claudewheel already constructs that invocation) vs a SessionStart
   hook emitting the content. Includes the subagent question: an appended system
   prompt reaches only the main session; subagents assemble their own prompts,
   so decide whether conditional context must reach workers and, if so, how.
4. **Bookkeeping** — per-condition timeout; ordering and separation when several
   conditions are true at once; the exact framing-header template; whether a
   tool can ship multiple pairs and how they are namespaced under the tool dir.

### Affected areas (new module + wiring)

- A new deploy path analogous to `hook_scripts.deploy_scripts()` for copying
  condition/context pairs into `~/.claudewheel/conditional-context/`, going
  through `claudewheel.effects` (the mandatory subprocess/filesystem chokepoint).
- The launch flow (`preflight.py` / the invocation builder) to run conditions
  and assemble the appended system prompt.
- A `claudewheel health` check for orphaned/stale pairs, mirroring the existing
  hook-drift checks.
- Tests: a real-execution harness (run a fabricated condition against a fabricated
  cwd, assert the markdown is/ isn't injected), modeled on the existing hook
  exec-test pattern; every mock specced per the repo's autospec requirement.
- Docs: a concept page; if the pair registry is model-derived, a generated table.

### Effort: L (new subsystem, cross-repo contract with the contributing tools).

---

## Item 2: Adopt `CLAUDE_CODE_DISABLE_ATTACHMENTS=1` fleet-wide

### What we need

claudewheel sets `CLAUDE_CODE_DISABLE_ATTACHMENTS=1` in the session environment
of every launched session, across all profiles.

### Why / evidence

This env var short-circuits Claude Code's entire per-turn attachment pipeline.
Probed empirically this session (a session restarted with the flag set):
subagent completion results still arrive (they travel on the message queue,
which the disabled branch explicitly preserves), hooks still fire, and
deferred-tool loading still works. What dies is wanted or accepted: mid-session
downward CLAUDE.md discovery (the intrusive injection of a subdirectory's
CLAUDE.md when a session starts touching files there — the requester wants this
GONE, no compensation), file-mention content injection, changed-files notices,
and assorted reminders. The launch-time CLAUDE.md chain (cwd, ancestors, user
memory, imports) is a DIFFERENT mechanism and is untouched by this flag — do not
also set `CLAUDE_CODE_DISABLE_CLAUDE_MDS`.

### Affected areas

- Wherever claudewheel composes the launched session's environment. Locate the
  env-composition point (the launcher clearly can set session env — this
  session was started with the var set); add the var as canonical.
- Consider whether it belongs alongside the canonical settings model so it is
  covered by a drift/health check rather than being a loose literal.

### Effort: S. Reverting is deleting one line.

---

## Item 3: Kill cross-session messaging (both directions)

### What we need, in the canonical shared-settings model

- Inbound: the setting `crossSessionInbound: "refuse"`. This drops incoming
  messages from other sessions (local socket peers, cloud, Remote Control)
  before they reach the conversation. Note the DEFAULT when unset is auto-accept
  from any same-permission-mode session, so this key is required, not
  redundant; `"hold"` is insufficient (it parks and surfaces, does not drop).
- Outbound: `permissions.deny` entries for `SendMessage`, `ListAgents`, and
  `ListPeers` (ListPeers is an alias of ListAgents — deny both names).

### Placement (already decided)

Into claudewheel's canonical settings model, so the launch-time reconcile writes
the keys into every profile's `settings.json` and the shared settings, under the
existing single-authority machinery. The requester accepted the one theoretical
hole (a `--settings` flag or an excluded user-settings source could override the
inbound key) because nothing on this machine launches sessions that way; managed
settings was considered and declined to avoid a second configuration authority.

### Affected areas

- `defaults.py` canonical builder (settings shape), the reconcile writer, and
  the canonical-permissions/settings drift health checks and their tests.
- Confirm the deny-rule matcher actually refuses these tool NAMES (the design
  session did not trace a settings deny rule end-to-end through the permission
  matcher for these specific names — verify by observing the tools refuse).

### Effort: M.

---

## Item 4: Neuter the `@` file-mention picker

### What we need

Set `fileSuggestion` in the canonical settings to a command whose output is
empty, e.g. `{ "type": "command", "command": "true" }`. In the installed builds,
a custom `fileSuggestion` command REPLACES the built-in file index as the
popup's suggestion source (early return; the built-in index becomes
unreachable), and empty output yields no suggestions, so the `@` popup never
appears. This removes the popup's Enter-accepts-the-highlighted-path hazard.

### Why

A separate design decision established a protocol-invocation convention that uses
bare names (no sigil), so the `@` picker is pure friction with no offsetting use.
Killing the popup is independent of that convention and wanted on its own.

### Caveats to record in the implementation

- This rests on UNDOCUMENTED internals ("custom command with empty output ->
  empty list -> no popup"), verified in three builds but not contracted. A future
  auto-update could change it; a health check that notices the popup behavior
  regressed would be defensive but is not required.
- It only neuters the popup's file lane. Agent-name and MCP-resource mentions
  still share the popup surface. And at SUBMIT time an `@token` that exactly
  matches a real relative path still injects that file — but Item 2's attachments
  flag removes that injection path entirely, so with both items adopted the
  submit-time risk is gone too. Note the ordering dependency: the `@`-injection
  concern is fully closed only once Item 2 is live.

### Affected areas: `defaults.py` canonical settings + drift check + tests.

### Effort: S.

---

## Item 5: Guardrail hook-suite delta (new rules on the existing model)

The repo already ships a mature generated guardrail system (a single rule model
producing bash hooks, settings globs, and docs tables, across HARD_DENY /
ESCALATE / ADVISE / ASK tiers). Most of what the design session wanted already
exists (rm, the destructive-git family, push as subagent-blocking, worktree
isolation). This item is the DELTA — new rules/matchers to add to that model,
each with the block message carrying the corrective remedy, each covered by the
existing three test layers (model contract, real-script execution against
fabricated tool-call JSON, deployment/reconcile). Build and test; do not deploy
without sign-off.

New rules/checks wanted:

- **Any `v1.x` (or higher) tag creation** — hard block. Rationale: the Go module
  proxy makes a stray 1.x tag a permanent `@latest` for that module forever, so
  this is a genuine hazard, not a style rule. Cover the tag-creating command
  forms.
- **`--yes` anywhere in a Bash command line** — block, pointing at
  `--approve-consequential`. (The fleet CLIs ban `--yes` at registration; this
  catches it before it is even attempted.)
- **`go install ...@latest` for internal modules** — block, pointing at `@v0`.
  Scope to the fleet's own module paths.
- **`pip install` (not via uv)** — advise or block, pointing at `uv`.
- **Writes into `/tmp` or the harness session-temp directory** — block, pointing
  at a real in-project or user-visible destination. This spans Write/Edit and
  Bash redirections, so it is path-based, not a single-command matcher; a
  harness-scratchpad todo may already exist here.

New hook scripts (new event/matcher, five-file changes on the existing pattern):

- **Agent-tool calls that are misconfigured** — extend the existing Agent-matcher
  PreToolUse hook (the worktree blocker) to also block `subagent_type: "fork"`
  and `run_in_background: true`, and — if the tool input exposes it — flag/deny
  Agent calls that carry no model override (the always-spawn-on-Opus rule; a
  fork-subagent-type todo may already exist). Verify what the Agent tool input
  JSON actually exposes to a PreToolUse hook before promising the model-override
  check; if the parameter is invisible to the hook, say so and fall back to the
  agent-definitions approach in Item 6.
- **`AskUserQuestion` with `multiSelect: true` or a `preview` field** — block.
  The requester's standing rules forbid both entirely.

Also fix the one dead existing rule found: the `safegit rewrite-author` guard
(both its hook pattern and its settings glob) matches a command spelling that no
longer exists after the upstream rename to `safegit author rewrite` — update it
to the current spelling so it guards something again.

### Effort: M per rule cluster; the model makes a Bash-command rule a one-file
change plus tests, and a new event/matcher a five-file change.

---

## Item 6: Standard subagent definitions on Opus — define, stage, do NOT auto-adopt

### What we need

Define a small standard set of Claude Code agent definitions carrying
`model: opus` in frontmatter, so an Agent call that names such a type runs on
Opus with no per-call parameter — turning the always-spawn-on-Opus rule from
prompt memory into structure. Candidate set: a general worker (Opus,
general-purpose), an auditor (Opus, fresh-context spec verification — matches the
requirement that an auditor knows nothing of the implementation), and an
implementor (Opus).

### Constraint

DEFINE and stage only. The requester wants substantial testing comparing these
against the current global-rules-driven way of spawning BEFORE committing to
using them, and before claudewheel deploys them to profiles. So: author the
definitions, decide the deployment mechanism (how claudewheel would sync
`.claude/agents/` into profiles, analogous to how it deploys hooks), but do not
turn deployment on.

### Effort: M (definitions are small; the deployment/sync mechanism is the work).

---

## Cross-cutting implementation notes

- **Effects chokepoint:** all new file/subprocess/network work in `claudewheel/`
  must go through `claudewheel.effects`; an AST test enforces this.
- **selfdoc:** `README.md` and `CLAUDE.md` are generated and read-only — edit the
  `docs/_*.md` templates and regenerate; new guardrail rules appear in the
  generated tables automatically.
- **Tests:** follow the existing layered pattern — an independent hand-duplicated
  contract test for the model, a real-execution test that runs the actual script
  against fabricated stdin JSON, and reconcile/deployment tests. Every mock must
  be specced (autospec/spec/spec_set or patch.dict), enforced as a preflight
  external check with no bypass.
- **Block mechanism:** existing hooks deny via the JSON
  `permissionDecision: "deny"` envelope with exit 0 (not exit 2), with the remedy
  in `permissionDecisionReason` and JSON escaping done by `jq --arg`. New blockers
  should match this for consistency and testability.
- **Reconcile refreshes only MISSING scripts**, never changed ones — so any
  edited script needs an explicit force-overwrite redeploy to take effect, and a
  known hazard (filed separately) is that the per-launch reconcile
  whole-replaces the hooks subtree and silently prunes hand-added entries.
- **Remedy truthfulness:** every new block/advice message that names a remedy
  gets a test that performs the remedy and asserts the error clears; every
  "then run X" instruction is executed once in a fixture before it ships.

## Sequencing

- Items 2, 3, 4 are small canonical-settings / env changes and can go together.
- Item 4's submit-time `@`-injection risk is only fully closed once Item 2 is
  live — note the dependency but neither blocks the other's build.
- Item 1 (conditional context) is the largest and has open design decisions that
  need a round with the requester before implementation; it is also what makes
  the biggest cut to the global rules file possible, so it is high value.
- Item 5's model-only rules (v1.x tags, `--yes`, `@latest`, pip, the dead-rule
  fix) are cheap and can land incrementally; the new-hook items (Agent-tool
  extensions, AskUserQuestion) need the Agent/AskUserQuestion input-JSON
  verification first.
- Item 6 is define-and-stage; no deployment until the requester finishes
  comparison testing.

Nothing here is deployed to live profiles or used to replace global-rules text
without the requester's explicit per-item approval.
