# Attachments kill switch and conditional context injection (the final feature release)

## Scope ruling (owner, 2026-09-06)

This file is the LAST minor release of the current workstream: adopt the attachments
kill switch fleet-wide and ship conditional context injection. Everything here is
build-and-stage until the owner flips each piece on for live profiles.

## Part 1: CLAUDE_CODE_DISABLE_ATTACHMENTS fleet-wide

Facts established 2026-09-06 (binary 2.1.236 + captured API requests + official docs):

- The env var is officially documented with ONE line: "@-mentions are sent as plain text
  instead of being expanded". Every other effect is undocumented behavior. (Docs
  retrieval note: `curl https://code.claude.com/docs/llms-full.txt` and grep locally;
  WebFetch truncates it and returns confident false negatives.)
- Verified effects: the per-turn attachment builder short-circuits, deliberately
  preserving queued user input and the agent-listing delta; killed: at-mention content,
  changed-files diffs, the skills listing block, MCP resources, plan/auto-mode blocks,
  reminders. `CLAUDE_CODE_SIMPLE` (= `--bare`) is a superset switch.
- Known costs, both upstream-known and closed as not-planned: subdirectory CLAUDE.md
  lazy loading dies with it; and the POST-COMPACTION restore path is NOT guarded — after
  every compaction the client re-reads up to five recently-modified files (5,000-token
  cap each; over-cap files degrade to path references, so the realistic ceiling is
  ~25K tokens, not the ~50K once measured — that figure conflated the separate skill
  re-injection budget). The compaction hole is unreported upstream; if filing, attach to
  the open issue about post-compaction instruction re-injection rather than a new one.
- Delivery channel verified: a top-level `"env": {"CLAUDE_CODE_DISABLE_ATTACHMENTS":
  "1"}` inside a profile's own settings.json IS applied by the client
  (capture-confirmed byte-identical to a process env var). So the choice is settings-env
  (rides the canonical model and its drift checks) vs launch-environment injection
  (rides `PROFILE_ENV_KEYS` and its symmetry test). Boolean env parsing is strict:
  only `1/true/yes/on` count.
- Open decisions: channel (above); a per-launch TUI toggle so a planning session can
  recover plan mode (the one real casualty); acceptance of the two costs; whether to
  re-verify the subdirectory-CLAUDE.md collateral on the then-current client before
  enabling (the closed upstream issue was filed against a much older build).

## Part 2: the session temp directory

The harness injects a system-prompt section steering all temporary files into
`/tmp/claude-<uid>/<munged-cwd>/<session-uuid>/...`, which conflicts with the fleet
rule against /tmp usage. Facts established 2026-09-06:

- No dedicated off-switch exists; the local feature-flag override mechanism is compiled
  out of release builds. The prompt section is emitted only when the Artifact-tool
  eligibility check passes AND/OR a server-side feature flag is on; the section is
  therefore not observable under a local capture harness (the flag evaluates off with a
  fake key), so suppression via `CLAUDE_CODE_DISABLE_ARTIFACT=1` + `DISABLE_GROWTHBOOK=1`
  is mechanically plausible but EMPIRICALLY UNCONFIRMED for a real authenticated
  session — confirming it needs a flags-passthrough probe with a real credential.
  Cost of that route: loses the Artifact tool and all remote feature flags.
- `CLAUDE_CODE_TMPDIR` relocates the entire tree (code-verified chain, ownership
  hardening: target must exist, be owned by the uid, mode 0700). It also moves the
  peer-messaging socket, whose path has a ~104-byte limit — a long directory breaks it.
  Relocation changes only the path; the model still gets the section.
- Options: relocate (cheapest, keeps features), suppress (costs Artifact + flags,
  unconfirmed), or prose-only (status quo: the fleet rules file overrides per session).
- claudewheel's own cleanup surface for the tree lives in `claudewheel/scratchpad.py`
  and the preflight cleanup prompt (see the confirm-key todo for its UX).

## Part 3: conditional context injection

The centerpiece feature: fleet authoring tools (rlsbl, selfdoc, strictcli, and any
opt-in dev tool) each ship condition/context pairs — a condition script (bash or
python) that receives the session's working directory and answers yes/no, and a
markdown fragment injected into the session's system prompt when the condition holds,
framed by a header naming the satisfied condition ("This directory is a project whose
releases are managed by rlsbl"). Fragments are overviews plus breadcrumbs (run
`<tool> --help`, read `<file>`), not full documentation. Pairs live under
`~/.claudewheel/conditional-context/<tool>/<pair>.{sh,py,md}`.

Why this shape: one machine-wide file per tool (updated with the tool) instead of
scaffold-copied guidance drifting in every repo; launch-time system-prompt injection is
immune to the attachments kill switch; it is the agreed destination for the per-tool
guidance currently living in the operator's global rules file.

Open design decisions — resolve with the owner BEFORE building; none may be picked
silently:

1. Registration path: a claudewheel command tools invoke at install/scaffold time; a
   step in each tool's own installer; or symlinks into the tool checkouts (with
   editable installs, symlinks keep context current automatically).
2. Condition contract: how the cwd is passed (argv vs stdin), how the boolean returns
   (stdout token vs exit status), and — the real tension — what a BROKEN condition does:
   abort the launch (hard-error doctrine) vs loud skip vs silent skip (the preflight's
   never-block-the-launch convention). These pull opposite ways; owner rules.
3. Injection mechanism: `--append-system-prompt` (or its file variant) on the built
   invocation vs a SessionStart hook emitting the content — including whether
   conditional context must reach SUBAGENTS (an appended system prompt reaches only the
   main session; subagents assemble their own prompts).
4. Bookkeeping: per-condition timeout; ordering and separators when several conditions
   hold; the framing-header template; whether a tool ships multiple pairs and how they
   are namespaced.

Elements salvageable from the earlier tag-based design (superseded as a whole, kept
here as candidate ingredients): deterministic assembly for prompt-cache stability (same
input set must produce byte-identical output; no timestamps); a practical size ceiling
(assembled context under roughly 15K tokens — the client's own prompt is already large,
and quality degrades well before 60K total); fragments under ~200 lines each;
tool-shipped fragments discoverable from installed packages via importlib.resources; an
implication relation between tags/conditions if pair explosion ever demands it; a
committed per-project declaration file only if auto-detection proves insufficient
(conditions probing the cwd may make declarations unnecessary — that was the main
advance over the tag design).

Related facts for the implementation: everything effectful goes through
`claudewheel.effects` (AST-enforced); a deploy path analogous to hook-script deployment
is the natural shape; a health check for orphaned/stale pairs mirrors the hook drift
checks; real-execution tests (fabricated condition against a fabricated cwd) mirror the
hook exec-test pattern.

## Supersedes

disable-attachments-env-var.md, disable-harness-scratchpad.md,
tag-based-context-injection.md, and the conditional-context/attachments/file-mention
items of session-governance-and-conditional-context.md (the file-mention-picker kill —
ruled, mechanism verified — lives in session-capability-stripping.md; the guardrail
delta items live in guardrail-hooks-and-permission-rules-overhaul.md; the Opus
agent-definitions item lives in launcher-instrumentation.md).

## Effort

Attachments adoption: small once its three decisions are made. Temp directory: small
after its ruling (relocation) or an experiment (suppression). Conditional context:
large — a new subsystem with a cross-tool contract; its four decisions are the critical
path.
