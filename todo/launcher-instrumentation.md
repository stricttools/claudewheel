# Launcher-side instrumentation and enforcement

## Context

claudewheel owns the three control points around every session: the binary install
pipeline, the launch environment/argv, and per-profile settings/hooks. The items below
are independent directions, none urgent; facts were verified 2026-09-06 against the
installed client (2.1.236) and this repo.

## 1. Version audit on install

New client versions silently change the guarded surface (tools appear/vanish, settings
keys move, disable switches come and go). Direction: on `claudewheel install`, extract
the downloaded binary's tool registry, settings schema, and env-switch list; store a
per-version extract; diff against the previous version; print an upgrade report.
Corrected premise: `scripts/tool-strip-report` is NOT a binary-mining example — it is a
runtime interception harness (launches the real client against a local capture server
answering 401, reads the request's tools array; strong for the tool registry, blind to
schema/env-vars). Binary text extraction IS proven separately: bounded streaming greps
over the bundle reliably yielded the env-var registry, the settings schema, and the
tier lists (never load the whole ~320 MB file; bounded `grep -a -o` patterns).
The natural attach point is between checksum verification and the final rename in
`claudewheel/install.py` — and the dry-run preview branch must mirror any new step or
previews under-report. Follow-ons: a health check validating profile settings keys
against the installed version's schema; flagging strip-list entries the installed
version no longer offers; asserting a minimum client version where guardrail
correctness depends on it (the client's own rule-matching had compound-command bypasses
fixed across 2.1.243-2.1.259).

## 2. PTY-capture observability

`claudewheel/pty_runner.py` already proxies a child under a PTY and captures output —
but no launch uses it: launches exec-replace the launcher process, so routing sessions
through the runner turns claudewheel into a long-lived supervising parent (signals,
resize, exit codes) with an unbounded in-memory capture. Before any default-on:
retention policy, size bounds, and secret scrubbing (captures contain everything echoed
to the terminal). Its one production consumer today is token capture during profile
creation.

## 3. Launcher-owned scheduling via systemd timers

Session-created cron jobs live in the harness's own store — per-profile invisible
state. Direction: a schedule subsystem creating systemd user timers that launch
profile-scoped sessions; then set `CLAUDE_CODE_DISABLE_CRON=1` (verified present in the
client; removes the cron tools from sessions) and retire the harness store. Explicit
trade to rule on: sessions lose in-session self-scheduling — decide whether that loss
is intended. Interim smaller item regardless: a read-only scheduled-jobs inventory
(health section or screen: what fires, when, with what prompt), with the read path
behind one module since the store location may shift across client versions.

## 4. Compile mechanically checkable session rules into hooks

Some operator rules are pure string checks (banned vocabulary in output, no dates in
commit messages, never writing a blanket consent flag into a command line). The
guardrail generation pipeline already exists; extend it with a declared set of
string-checkable rules compiled into advise-tier (or justified blocker) hooks. Only the
mechanically crisp subset; noise is real — start narrow.

## 5. Dependency references injected at launch

Sessions read sibling checkouts of dependencies instead of released docs. Direction:
point sessions at the generated docs for the project's declared dependencies (the
unified docs site publishes per-project full-text builds). This is a natural
conditional-context pair (see attachments-and-conditional-context.md) rather than its
own mechanism — decide there.

## 6. Session forensics

`claudewheel inspect-session <id>`: read a session's JSONL from the shared store and
summarize errors, token burn, tool-call anomalies, stuck/retry patterns. Complements
the sessions overview (list vs analyze-one). Keeps working regardless of harness debug
tooling changes.

## 7. Subagent-model enforcement levers (define and stage; adoption needs testing)

The operator rule "spawn subagents on the designated heavy model" is prompt-discipline
today. Mechanical levers verified available: `CLAUDE_CODE_SUBAGENT_MODEL` +
`CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (forces every subagent's model, ignoring per-spawn
overrides — note it would also flatten deliberate exceptions); agent definitions with a
model field in frontmatter (a standard worker/auditor/implementor set was proposed —
DEFINE and stage only; the owner wants comparison testing against the current
prompt-driven spawning before any adoption, and a deployment mechanism decision for
syncing definitions into profiles); `PreModelSwitch`/`PostModelSwitch` hook events
(added v2.1.251: can block or confirm a model switch; a non-answering hook blocks;
30-second timeout — much tighter than other hooks; adopting sets a fresh version floor).

## 8. Small verified gaps to fold into whichever item ships first

- The wizard's auth subprocesses build their env from scratch and omit the
  marketplace-autoinstall suppression, so a new profile's first-ever client invocation
  runs unsuppressed — likely why plugin trees appear on fresh profiles (`purge-plugins`
  exists to clean them). Fix alongside any launch-env work.
- No health check compares the client symlink target against the installed versions
  (a newer version can sit installed while the symlink pins an older one), and the
  model-version guard's remedy text tells the user to install a version that is already
  on disk in that state — a false remedy.

## Supersedes

launcher-instrumentation-generalizations.md, plus the scheduled-jobs and forensics
items formerly in the session-governance filing and the Opus-agent-definitions item
from the same.

## Effort

Each direction is independent; 1 and 2 are medium-large, 3 medium, 4 medium, 6 medium,
7 small-to-define. Rule which are wanted at all before any design.
