+++
title = "Probes"
description = "How claudewheel reports OOM kills to Claude Code sessions: every session is told of its own commands' kills, probes watch other sessions or all of them until a deadline, the probe runner service that reads the journal, how a report reaches an idle main conversation, a subagent, or a session that resumes, the label on a tool call that ended OOM-killed, and where nothing is dropped."
nav_group = "Concepts"
nav_order = 7
+++

# Probes

A command a session starts can be killed by the kernel's OOM killer: a
`heavy` job that went over its `--mem` cap, or a Bash command that ran outside
`heavy` when the session's Bash commands reached the memory cap they share. The session that started it
should hear about it at once, even when it is sitting idle, and the fix is
never to run it again as it was. claudewheel reports every such kill to the
Claude Code session concerned, and a probe lets a session watch other
sessions' kills too.

## Every session is told of its own kills

An OOM kill of a command a session started, in its own session scope, in the
tool scope of one of its Bash commands, or in a `heavy` scope it launched, is
always reported to that session. Nothing has to
be set up for it, and it has no deadline: when the session has ended, the
report is kept until the session resumes. The report says what was killed,
where, and when, and ends with the same advice `heavy` prints when it kills a
command: fix the memory at its source, do not rerun it.

The report reaches the session's main conversation. A subagent learns of a
kill of its own command from its own failed tool call (see "The label on an
OOM-killed tool call" below).

## Probes watch other sessions

A probe watches for one kind of event and reports each one to the sessions
subscribed to it. The only kind is `oom-kill`, systemd's result term for a
unit whose process the kernel's OOM killer killed. A probe watches one other
session (`--session <uuid>`) or every session (`--all-sessions`, spelled out),
and every probe states a deadline:

```bash
claudewheel probe create oom-kill --all-sessions --deadline 2h
claudewheel probe create oom-kill --session 4d97ca01-9d56-4f49-8047-77f5160febde \
    --deadline 1d --until-watched-ends
```

- `--deadline` (required) is how long the probe lives at the latest, from
  now: a whole number with an `s`, `m`, `h`, or `d` suffix.
- `--count N` ends it once it has seen N kills.
- `--until-watched-ends` ends it when the watched session ends (it needs
  `--session`).
- `--until-file PATH` ends it once the absolute PATH exists.
- `claudewheel probe stop <probe-id>` ends it at once; only the session that
  created it can.

A probe runs no command. `probe create` refuses anything that is not a kind
that exists, and names the kinds; a command after `--` is refused the same
way.

`probe create` subscribes the session it runs in. Another session subscribes
with `claudewheel probe subscribe <probe-id>` and leaves with
`claudewheel probe unsubscribe <subscription-id>`. `claudewheel probe list`
shows every probe with its stops and subscriptions, and everything the store
holds that someone may need to act on (see "Nothing is dropped silently").

### Which session a probe command acts for

`probe create`, `subscribe`, `unsubscribe`, and `stop` learn their session from
their own cgroup: claudewheel starts each session in its own systemd scope,
`claudewheel-session-<pid>-<time>.scope`, and each command the session's Bash
tool runs in a tool scope, `claudewheel-tool-<pid>-<time>-<n>.scope`, that
names it (see "A memory cap each session's commands share" in
[Guardrails](guardrails.md)). The session scope names the Claude Code process,
and the lifecycle store maps that process to its session. A probe command run
outside a session, from a terminal or through `heavy` (which runs its command
in a scope of its own), refuses and says so. `probe list` runs anywhere.

### Which conversation a subscription belongs to

`probe create` and `probe subscribe` print a line naming the subscription.
When the tool call that ran them returns, the `hook-deliver-probe-reports`
hook reads that line from the call's output and binds the subscription to the
conversation that made the call: the payload's `agent_id` for a subagent, the
main conversation otherwise. The command itself is never rewritten. Until the
binding, the subscription's reports wait, listed as waiting for it.

## The probe runner

One persistent user service hosts every probe:
`claudewheel-probe-runner.service`. It follows the user journal for systemd's
"The kernel OOM killer killed some processes in this unit" entries and, for
each one:

- attributes the unit to a session: a session scope through the lifecycle
  store; a Bash command's tool scope through the session scope its name
  carries; a `heavy` scope through the session scope `heavy` wrote into the
  scope's description, which the journal keeps after the scope is gone;
- queues the session's own report, and one report to each subscription of
  every live probe that watches that session or all sessions;
- records the kill, then saves its journal cursor, so a restart resumes after
  the last entry it handled, with no repeat and no gap.

Between entries it ends probes whose deadline passed, whose file appeared, or
whose watched session ended; confirms each report handed to a session by
finding the report's id in the session's transcript, and queues it again when
the session went away without it; and expires the undelivered reports of an
ended probe once their session has ended too.

`claudewheel deploy-hooks claudewheel-probe-runner.service` (or `--all`)
writes the unit to `~/.config/systemd/user/`, enables it so it starts with the
user's systemd manager after every reboot, and starts it; it restarts after a
failure. `systemctl --user stop claudewheel-probe-runner.service` stops it
gracefully: it saves its cursor and exits. `claudewheel health` checks that
the unit is the one claudewheel deploys and that it is enabled and running
(`probe-runner`), and names the redeployment that fixes it.

## How a report reaches a conversation

Two hooks hand reports over; both are part of the canonical wiring every
profile gets (see [Health Checks](health.md)).

- **The main conversation** gets its reports from
  `hook-wait-for-probe-reports`, an `asyncRewake` hook on `SessionStart` and
  `Stop`. It waits in the background, one waiter per session, and when
  reports are queued it prints them and exits 2, which wakes the session even
  when it sits idle. The reminder the model sees starts with `claudewheel
  probe report:` and the notification is summarized as `claudewheel probe
  report` (the hook's `rewakeMessage` and `rewakeSummary`). Reports queued
  together arrive in one delivery. On a resumed session (`SessionStart`
  source `resume`), the reports kept for it arrive at once.
- **A subagent** gets the reports for it from `hook-deliver-probe-reports`,
  on the `PostToolUse` or `PostToolUseFailure` of its next tool call, as
  `additionalContext`. A report for a subagent that has finished goes to the
  main conversation instead, labeled with the subagent and its task.

The waiter is the one written exception to the rule that a claudewheel hook
never blocks a session (every other hook exits 1 when it fails, never 2): on
an `asyncRewake` hook, exit 2 blocks nothing, and it is the only way a
background hook can wake an idle session. It exits at once in a session that
is not interactive (under `claude -p`, an `asyncRewake` hook would hold the
run for its whole timeout), and when its Claude Code process is gone.

## The label on an OOM-killed tool call

When a Bash call ends with status 137 and the runner recorded an OOM kill in
that session during the call, `hook-deliver-probe-reports` labels the failure
for the conversation that made the call: `this command was OOM-killed: fix
the memory at its source, do not rerun`, with what was killed, where, and
when. When other Bash calls of the session were running at the kill, the label
names them and says the killed process may have been theirs; each of them
that ended OOM-killed is labeled the same way. The call's own output already
ends with the line `claudewheel-tool-scope` prints for a kill in the command's
scope, naming the memory cap the session's Bash commands share (see "A memory
cap each session's commands share" in [Guardrails](guardrails.md)).

## Nothing is dropped silently

- A kill no session and no subscription took is recorded as unrouted, with
  why it could not be attributed, and `probe list` lists it.
- A report whose session has not received it is listed as undelivered by
  `probe list`, and counted in the sessions overview's `Undelivered` column.
- A report of an ended probe whose session ended without it is expired, and
  `probe list` lists it.

## Where the state is

Everything lives under `~/.claudewheel/shared/probes/`, each file shape a
strictspec schema under `.strictspec/`:

| Path | What it holds |
|---|---|
| `probes/<probe-id>.jsonl` | one probe's log: created, subscribed, bound, unsubscribed, ended |
| `kills.jsonl` | every OOM kill the runner read, its session, its reports, and its label |
| `sessions/<session>.jsonl` | a session's Bash calls and subagents, as the deliver hook recorded them |
| `reports/<state>/<session>/<report-id>.<recipient>.json` | one report; the state is `pending`, `handed`, `delivered`, or `expired`, and the recipient is `main`, `agent-<id>`, or `unbound-<subscription>` |
| `waiters/<session>.lock`, `waiters/<session>.fifo` | the lock that keeps one waiter per session, and the FIFO the runner wakes it through |
| `journal-cursor` | where the runner resumes |

The delivery mechanics are verified against each Claude Code version listed
in `claudewheel.probe.VERIFIED_CLIENT_VERSIONS`: the test suite runs a real
interactive session of each one and checks the idle wake with its custom
texts, print mode, subagent injection, the 137 label, and delivery on resume.
