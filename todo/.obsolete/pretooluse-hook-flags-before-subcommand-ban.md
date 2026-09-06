# Should the shared PreToolUse hook ban the "flags before subcommand" argv shape?

Filed 2026-08-11. Decision deliberately left open — both sides below. No code change has
been made anywhere for this; it is a pure policy question for claudewheel's hook surface.

## Context

claudewheel centrally manages the shared settings inherited by every profile, including
the `hooks` configuration. A `PreToolUse` hook on the Bash tool sees every shell command
an agent is about to run — across all profiles, all sessions, all programs — and can deny
it with a message fed back to the agent.

Elsewhere in the fleet, a CLI framework's preview-mode allowlist (argv-prefix matching
that decides which subprocesses may really execute during a dry run) was found to accept
a hazardous prefix shape: a prefix whose second token is a flag rather than a subcommand
(e.g. `["tool", "--dry-run"]`). Because the framework accepts its reserved flags anywhere
in argv, such a prefix certifies nothing about which command actually runs — `tool
--dry-run <any-subcommand>` all match it — while failing to match the semantically
identical `tool <subcommand> --dry-run`. The structural fix (registration-time hard error
on flag-leading prefixes, plus an extended breadth check) is planned in that framework and
covers every tool built on it.

The question raised afterwards: should claudewheel's shared PreToolUse hook ADDITIONALLY
ban the argv shape itself — any invocation where flags precede the subcommand — so that a
version of the protection applies to ALL programs agents invoke, not only tools built on
that framework?

## The case FOR the hook ban

- Coverage: applies to every binary, regardless of what framework (if any) it is built
  on, with no cooperation needed from the invoked tool.
- Centralized: one hook in shared settings covers every profile and every session at once.
- Normalization: forces agents into one canonical argv order. That makes OTHER
  prefix-based matchers harder to game by flag reordering — including Claude Code's own
  permission rules, which also match Bash commands by prefix.
- Cheap to attempt: one hook script; no changes to any tool.

## The case AGAINST

- The shape is legitimate and often mandatory. `git -C /path status`, `git --no-pager
  log`, `docker --context prod ps` — and some fleet tools document app-global flags
  BEFORE the subcommand as their own convention. A ban blocks correct daily work.
- Meaningless for most of Unix. `grep -q pattern file`, `ls -la` — no subcommands exist;
  flags-first is the only shape. The hook would need a maintained list of
  "subcommand-style" programs: a new taxonomy that must be kept correct forever.
- Weak enforcement. The hook sees one shell STRING, not an argv. Pipes, `&&`, quoting,
  `env VAR=1 cmd`, `bash -c '...'`, `xargs`, command substitution, and
  variables-as-commands all evade naive tokenization. Hooks also never see subprocesses
  spawned by an invoked tool — only the agent's own top-level command. A
  trivially-bypassable guardrail reads as protection while providing little.
- The actual vulnerability is already addressed structurally at the only place that
  attached safety semantics to token position (the framework's allowlist validation).
  Banning the shape everywhere to protect a matcher that no longer accepts it is
  guardrail accumulation against a class already made impossible at the source.
- Agent friction: agents hitting shape denials on legitimate invocations will route
  around them (`bash -c`, subshells), training exactly the evasive behavior hooks are
  meant to discourage.

## A narrower alternative

Keep the hook layer for SEMANTIC denials, where coarse string matching is an acceptable
tripwire rather than a promise — e.g. the existing `todo/ban-non-git-remote-write-paths.md`
(remote-write enforcement), and possibly a top-level `git push` denial (aligned with the
fleet's no-manual-push rule; cannot break releases, which push from inside the release
tool's own process where hooks do not reach).

## Decision needed — pick one

1. Adopt the shape ban (accept the friction and bypassability for the normalization
   benefit; requires the program taxonomy and shell tokenization).
2. Reject it; keep the hook layer semantic-only (record the ruling and close this todo).
3. Adopt a scoped variant: shape ban only for a small named list of fleet tools whose
   canonical argv order is known and owned here.

## Affected files (if option 1 or 3)

- The shared-settings hook configuration managed by claudewheel (settings
  reconciliation), plus a new hook script shipped and managed by claudewheel.
- Tests covering the hook's tokenization, denial output, and bypass surface.

## Effort

- Option 1: moderate — hook script, shell-string tokenization, program taxonomy, tests,
  plus permanent maintenance of the taxonomy.
- Option 3: small-to-moderate — same machinery, smaller list, less friction.
- Option 2: zero — record the ruling, move this file per triage.
