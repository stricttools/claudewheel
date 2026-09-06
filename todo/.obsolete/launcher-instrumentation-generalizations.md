# Launcher-side instrumentation and enforcement considerations

## Context

claudewheel owns the three control points around every Claude Code session: the
binary install pipeline, the launch environment/argv, and per-profile
settings/hooks. Claude Code itself ships several capabilities in-band — a
session can flip its own debug logging, introspect the live settings schema,
register cron jobs in the harness's own timer store, and receive curated
reference payloads. Each of these has a stronger launcher-side counterpart:
observe and enforce from above, rather than trusting the harness to police
itself. The items below are independent considerations, in no particular
order; each stands alone. None is urgent.

## 1. Version audit on install

Problem: installing a new Claude Code version silently changes the surface
claudewheel guards. New tools appear (candidates for `disallowedTools`),
previously stripped tools vanish (making strip-list entries inert), settings
keys are added or renamed, and `CLAUDE_CODE_DISABLE_*` env switches come and
go. Discovering any of this today takes manual probing.

Direction: on `claudewheel install`, mine the downloaded binary for its tool
registry, settings JSON schema, slash-command list, and disable-switch env
vars (string/offset extraction; `scripts/tool-strip-report` demonstrates the
techniques against the live binary). Store a per-version extract, diff against
the previous version, print an upgrade report. Follow-ons: a `health` check
validating each profile's `settings.json` keys against the installed version's
schema, and flagging `disallowedTools` entries the installed version no longer
offers.

Affected: `claudewheel/install.py`, `claudewheel/health.py`, a new extraction
module, `scripts/tool-strip-report` (shared techniques).
Effort: medium-large — extraction robustness across binary layouts is the
hard part.

## 2. PTY-capture observability

Problem: the record of what a session actually did lives only in Claude Code's
own transcript formats, which shift across versions and omit whatever the
harness chooses not to log.

Direction: `claudewheel/pty_runner.py` already proxies a child under a PTY and
captures output. A per-profile launch option routing sessions through it gives
claudewheel an owned, complete, timed record of every session, independent of
harness internals. Forensics tooling then reads claudewheel's capture rather
than Claude Code's files.

Trade-offs to settle before default-on: disk growth, and captures contain
everything echoed to the terminal (including secrets) — needs a retention
policy and scrubbing story.
Effort: medium.

## 3. Launcher-owned scheduling via systemd timers

Problem: cron jobs registered by sessions live in the harness's own store —
per-profile invisible state, tied to harness internals, with no launcher-side
audit surface.

Direction: a `claudewheel schedule` subsystem creating systemd user timers
that launch profile-scoped sessions. Scheduling becomes OS-native: auditable
with `systemctl list-timers`, logged by journald, surviving harness changes.
Once at parity, set `CLAUDE_CODE_DISABLE_CRON=1` in the launch environment and
retire the harness timer store.

Trade-off to record explicitly: sessions lose in-session self-scheduling — a
running session can no longer extend its own loop. Decide whether that loss of
session autonomy is intended (it may be exactly the point).
Effort: medium.

## 4. Compile mechanically checkable session rules into guardrail hooks

Problem: some session-governance rules (banned vocabulary in output, no dates
in commit messages, never writing `--yes` into a command line) are pure string
checks, yet they currently rely on model discipline and decay under context
pressure. The guardrail model already generates and deploys blocker/advise
hook scripts for command-safety rules — the pipeline exists.

Direction: extend `claudewheel/guardrail.py` with a declared set of
string-checkable rules, compiled into generated advise-tier (or, where
justified, blocker) hooks by the existing generation path. Only the
mechanically crisp subset qualifies; rules requiring judgment stay prose.
Noise risk is real — advise hooks fire on every matching event — so start
narrow and grow deliberately.

Affected: `claudewheel/guardrail.py`, `claudewheel/hook_scripts.py`, deploy
pipeline, `health` drift checks.
Effort: medium.

## 5. Dependency references injected at launch

Problem: sessions working in a project tend to read sibling checkouts of that
project's dependencies, seeing half-finished working-tree state instead of the
released, documented surface.

Direction: at launch, point the session at the generated documentation for the
project's declared dependencies on the unified docs site (per-project
`llms-full.txt` builds already exist). Candidate mechanisms: a SessionStart
hook emitting the links, a generated context file, or an env var convention —
choosing the mechanism is the main design decision, along with how the
launcher discovers a project's dependency list.

Effort: small-medium once the mechanism is chosen.
