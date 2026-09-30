+++
title = "Guardrails"
description = "How claudewheel guardrails work: the 4 enforcement tiers, subagents versus the main agent, command-string caveats, the heavy wrapper and its memory budget, the memory cap each session's commands share, upgrading profiles, and stripped tools."
nav_group = "Concepts"
nav_order = 5
+++

# Guardrails

claudewheel ships a canonical set of command guardrails that every profile
inherits. Each rule names one command form an agent should not reach for and
says what to do instead: use a safer tool such as `safegit` or `saferm`, leave
the decision to the user, or not run a command at all. The rules live in one
place and drive both the deployed hook scripts and each profile's permission
arrays, and the "Rule reference" table below lists every one of them.

## Enforcement tiers

Every rule belongs to exactly one of 4 tiers. The tier decides where the
rule is enforced (a `PreToolUse`/`PostToolUse` hook, the settings
`deny`/`ask` arrays, or both) and who it applies to. The hook is always the
authoritative enforcer when a rule has one; the settings arrays are
best-effort defense-in-depth for the plain command form.

- **HARD_DENY** -- denied for everyone, both the main agent and subagents, via
  a `PreToolUse` hook. A backing settings `deny` glob may exist as
  defense-in-depth, but it never reproduces the hook's full match surface
  (compound commands, `sudo`/`env`/`xargs`/`find -exec` wrappers, alternate
  remotes). Some HARD_DENY rules own no deny glob at all.
- **ESCALATE** -- denied only when a subagent attempts the command. The main
  agent falls through the hook silently so the settings `ask` rule prompts the
  user to approve it deliberately.
- **ADVISE** -- the command runs, then a `PostToolUse` hook nudges the agent
  with advice via `additionalContext`. There are no settings entries and
  nothing is blocked.
- **ASK** -- a pure settings `ask` rule with no hook involvement. The user is
  prompted before the command runs.

## Subagents versus the main agent

The blocker hook distinguishes a subagent from the main agent using the
`agent_id` field in the `PreToolUse` payload. Claude Code populates
`agent_id` only for subagent tool calls, so a non-empty `agent_id` marks a
subagent. HARD_DENY rules block regardless of `agent_id`, while ESCALATE rules
block only when `agent_id` is set and otherwise let the main agent through.

This is why an ESCALATE command like `git push` is refused outright for a
subagent (with a message telling it to report to its parent) but merely prompts
the user when the main agent runs it. The distinction keeps risky,
outward-facing actions in the hands of the human-supervised main agent.

## Command-string caveat

The hooks match against the raw command string with `grep -qE`, anchored to
the start of a shell segment. They do not parse the shell. A command that only
*mentions* a guarded token -- for example inside an `echo`, a `grep` pattern, a
comment, or a heredoc -- can still trip the matcher and be nudged or blocked
even though nothing dangerous would actually run.

This is a deliberate trade-off: false positives are safe (you rephrase or
split the command), whereas parsing the shell to eliminate them would be far
more fragile than a conservative string match. When a benign command is
blocked, move the guarded token out of the command line or run the pieces
separately.

## Heavy commands and the heavy wrapper

Test suites, large builds, and verification runs can each take several
gigabytes of memory, and several of them started at once from different
sessions can exhaust the machine until the whole terminal session is killed.
The `heavy-unwrapped` rule refuses such a command (`go test`, `pytest`,
`npm test`, `cargo build`, `make`, Go's own toolchain builds `./make.bash`,
`./all.bash`, and `./run.bash`, a project's `scripts/*test*.sh`, and the rest
of the forms in the rule reference below) wherever it stands at a command
position, and tells the agent to run it through `heavy` instead:

```bash
cd project && heavy -- go test ./...
cd go/src && heavy -- ./all.bash
heavy --mem 3G -- scripts/full-suite.sh   # 3G set from a measured peak
```

Every heavy command declares its memory cap (`--mem`, 5G unless given), and
`heavy` starts it only when that cap fits in a machine-wide memory budget, so
heavy commands from every session run side by side as long as the memory is
there and wait only when it is not. A cap comes from a measurement, never a
guess: `heavy` prints the peak memory use of the command's scope when the
command returns, and `--mem` is set from that peak, so a job that needs less,
such as a single test or a small build, starts sooner. A command whose need is
not known is measured once with the largest cap that fits now, which `heavy`
names when a cap does not fit or a command is killed at its cap, and a command
killed at its cap is a defect to fix at the source, not a reason for a bigger
cap. The rule's refusal says the same. The budget rule is checked when the
command would start:

- the budget is `MemAvailable` from `/proc/meminfo` less a 2G margin, which
  stays free for the desktop and the Claude sessions as they grow;
- each running heavy command reserves the part of its cap it has not used yet
  (its cap less what its scope holds now, page cache aside). Running commands
  are counted by the `heavy-*` scopes still running as well as by their
  slots, so a command whose `heavy` was killed, which runs on in its scope,
  keeps its cap reserved until the scope ends, and the waiting message names
  the `systemctl --user stop` command that ends it;
- the new command starts when its cap plus those reservations fits in the
  budget, and at most 8 heavy commands run at once.

Swap never counts toward the budget: zram swap lives in RAM too, so a swapped
page still costs memory, and swap on disk is too slow to lean on. A cap larger than the
machine's whole memory less the margin could never start and is refused as a
usage error.

Each running command holds one of the 8 slots, a file under
`$XDG_RUNTIME_DIR/heavy.slots/` that `heavy` keeps a lock on for as long as it
runs, with a note naming its PID, start time, cap, directory, scope, and
command. A slot is taken while, and only while, a live `heavy` holds its lock,
so a `heavy` that dies in any way, `SIGKILL` included, gives back its slot and
its share of the budget at once, except what its command, if still running,
reserves through its scope. The admission itself happens under a short
lock, `$XDG_RUNTIME_DIR/heavy.lock`, held only for that moment. The command
runs with neither descriptor open, and when it returns `heavy` stops its scope,
printing `heavy: stopped what the command left running in <scope>` when there
was anything, so nothing the command started holds memory or budget after
`heavy` exits. A note is believed only while its process is alive with
the recorded start time; a slot held by a process `heavy` cannot identify is
reported as `unknown`, with the `fuser -v` command that finds it, and counts as
reserving all the memory.

While a command waits, `heavy` checks again every second and prints, every 30
seconds, how long it has waited, the memory figures, and every running heavy
command with its cap and use. After 60 minutes (`--max-wait` sets another
limit, such as `90s`, `30m`, or `2h`) it gives up with exit status 75, naming
the running commands. It says to wait longer with a longer `--max-wait`, to
lower `--mem` only to a measured peak of the command, never as a guess, and to
stop a running job only when it is stuck, never to make room; for each job it
names the `systemctl --user stop` of its scope, which ends the job and all it
started, where killing the job's `heavy` would leave its command running.

When the cap does not fit and no heavy command is running, none can end to
make room, so `heavy` exits with status 75 at once instead of waiting. It
names the largest cap that fits now, to be used only when a measured peak of
the command fits under it; otherwise the command needs more memory than is
free, and the message says to make it need less at the source, or to wait for
the memory outside `heavy` to be given back, and not to stop other programs to
make room.

The command runs in its own systemd user scope, named `heavy-<pid>-<time>.scope`,
described as `heavy job of claudewheel-session-<pid>-<time>.scope: <command>`
(the session `heavy` was started from, read from its own cgroup: the Bash
command's tool scope names the session scope; `heavy job outside any
claudewheel session: <command>` otherwise), which the journal
keeps after the scope is gone, so an OOM kill of the command is reported to
the session that started it (see [Probes](probes.md)). The scope is
capped at 5G of memory with no swap (`--mem` sets another cap, a whole number
with a `K`, `M`, `G`, or `T` suffix) and with `CPUWeight=20` (the default is
100), so interactive work stays responsive; its arguments reach it unchanged,
`$` included. `heavy` prints the cap on every
run (`heavy: capped at 5G`), and when the command returns, the peak memory use
of its scope, page cache included, rounded up
(`heavy: the command's scope peaked at 1.3G of its 5G cap (page cache included)`).

When the command is killed for going over the cap, `heavy` reads that from the
scope's result and prints `heavy: killed at the 5G memory cap`, with the
scope's peak before the kill when systemd recorded one. The same line says not
to rerun the command with a bigger `--mem`: a command that outgrows its cap is
a defect to fix at the source, by stopping that line of work at a clean
committed point, finding where the memory goes (a heap profile, what is held
at once, what is loaded that need not be), and cutting it. It names one
exception: when the cap was a guess rather than a measurement, measure once
with the largest cap that fits now, which it names, counting the running heavy
commands as an admission would, and set `--mem` from the peak that run
reports. When no cap larger than the one the command was killed at fits now,
it says so, and that the memory is to be cut at the source first. The scope is
then cleared, so none accumulate.

`heavy` adds `-p=2` to `GOFLAGS` unless `GOFLAGS` already sets `-p`, and prints a line when it does;
and it exits with the command's exit status. A command behind `heavy` is an
argument of `heavy`, never a command position of its own, so the wrapped form
passes the hook, and none of the rule's `deny` globs starts with `heavy`.

claudewheel ships the wrapper: `claudewheel deploy-hooks heavy` (or `--all`)
writes it to `~/.claudewheel/scripts/heavy` and makes `~/.local/bin/heavy` a
symlink to that copy. A redeployment swaps in a new file rather than rewriting
the old one, so a `heavy` that is running keeps the copy it started from. When
anything else already stands at
`~/.local/bin/heavy`, the deployment refuses and leaves it alone;
`--force-overwrite` replaces it with the link.

## A memory cap each session's commands share

The `heavy-unwrapped` rule matches command names, so a heavy command it cannot
see, such as a test suite started inside `bash -c`, still runs unwrapped. So
that such a command is killed while the session goes on, claudewheel caps
what a session runs, and not the session itself:

- **Claude Code runs uncapped** in the session scope,
  `claudewheel-session-<pid>-<time>.scope`, inside the session slice
  `claudewheel-<pid>_<time>.slice` (the Claude Code process ID and the launch
  second).
- **Every Bash command** the session runs, and everything that command starts,
  runs in a scope of its own, `claudewheel-tool-<pid>-<time>-<n>.scope`, inside
  the session's tools slice, `claudewheel-<pid>_<time>-tools.slice`. The tools
  slice caps all of them together: 6G of memory and 1G of swap unless set.

A slice's dashes name its parents, so the tools slice sits inside the session
slice, and every session slice inside `claudewheel.slice`. claudewheel starts
the session with

```bash
systemd-run --user --scope --quiet --collect --expand-environment=no \
    --slice=claudewheel-<pid>_<time>.slice \
    --unit=claudewheel-session-<pid>-<time>.scope \
    -p OOMPolicy=continue -- <the client command>
```

`systemd-run` puts the client in the scope and then becomes it, so the session
keeps claudewheel's process ID, terminal, and environment, and
`--expand-environment=no` passes the client's arguments through with any `$`
in them untouched. `OOMPolicy=continue` keeps the session running when the
kernel kills one of its processes because the machine ran out of memory.
claudewheel prints the layout and the cap at every launch:

```text
claudewheel: this session runs in claudewheel-session-<pid>-<time>.scope, not memory-capped; its Bash commands run in claudewheel-<pid>_<time>-tools.slice, capped together at 6G of memory and 1G of swap (tool_memory_max and tool_memory_swap_max in config.json)
```

The Bash commands reach their scopes through Claude Code's
`CLAUDE_CODE_SHELL_PREFIX`: the launch points it at
`~/.claudewheel/scripts/claudewheel-tool-scope`, and Claude Code runs every
shell command it spawns as `claudewheel-tool-scope '<command line>'`. The
wrapper tells a Bash tool call by the way Claude Code assembles one, a chain
that ends by writing the shell's working directory to a file, and:

- runs the call in its own scope in the tools slice. The session's first Bash
  call creates the tools slice with its cap; when the slice cannot carry the
  cap, the call is refused with `claudewheel: refused to run this command:`
  and the reason, rather than run uncapped.
- runs everything else Claude Code sends through the prefix directly, outside
  the cap: hooks, the status line, and stdio MCP servers. A guardrail hook
  killed at the cap would let its tool call through unchecked.
- reports a kill. When the kernel's OOM killer kills a process of the command,
  the wrapper prints one line on stderr once the command returns, and the
  command keeps its own exit status. Nothing is retried:

```text
claudewheel: this command was OOM-killed: the kernel's OOM killer killed 1 of its processes when this session's Bash commands reached the 6G memory cap they share (claudewheel-<pid>_<time>-tools.slice, MemoryMax=6G, MemorySwapMax=1G). The command ran in claudewheel-tool-<pid>-<time>-<n>.scope, which peaked at 5.9G (page cache included). Claude Code itself is not capped and keeps running. Fix the memory at its source, do not rerun it: ...
```

The count is of the command's processes the kernel killed, read from its
scope's `memory.events` (`oom_kill`). The tools slice's own count of the times
it reached its limit (`oom` in its `memory.events`) tells a kill at the cap
from one when the whole machine ran out of memory, which the line names
instead (`when this machine ran out of memory, with this session's Bash
commands under the 6G memory cap they share`). The cap is shared, so the
process killed is the largest one of any of the session's Bash commands, not
necessarily one of the command that went over.

- **The cap** is `tool_memory_max` in `config.json`, 6G unless set.
- **Swap** is `tool_memory_swap_max`, 1G unless set, or `0` for none. A little
  swap lets cold memory be compressed into zram instead of holding RAM; more
  would let a runaway push gigabytes into swap, slowing the whole machine,
  before the cap stopped it.

A `heavy` command started from a Bash command is not counted against the
session's cap: the user manager places a scope that names no slice in its
default slice, not inside the scope or slice of the process that asked, so the
`heavy` scope is outside the tools slice and only its own `--mem` cap limits
it.

Both keys take a whole number with a `K`, `M`, `G`, or `T` suffix; any other
value fails the launch with `Launch failed:` and the key's name. `systemd-run`
must be on the launch `PATH`, and the launch fails if it is not. A launch
deploys `claudewheel-tool-scope` when it is missing (`claudewheel deploy-hooks
claudewheel-tool-scope --force-overwrite` replaces a stale copy), and fails
when the deployed copy cannot be run. systemd keeps an empty slice until it is
stopped, so each launch stops the session slices of sessions that ended with
nothing left running in them, launched more than a minute ago. A session
already running keeps the units it was launched with; the layout applies from
its next launch.

## Upgrading existing profiles

The guardrail model evolves between releases. Existing profiles keep whatever
rules were current when they were created, so after upgrading claudewheel you
should re-apply the canonical model to bring older profiles up to date:

- Run `claudewheel reconcile-permissions` to rewrite each profile's
  `deny`/`ask`/`allow` permission arrays to match the current model.
- Run `claudewheel patch-profiles` to sync the deployed hook scripts and
  `disallowedTools` defaults into every profile and `shared-settings.json`.

Both commands support `--dry-run` so you can preview the changes before writing
anything to disk, and you should: the reconciliation is exact, so it prunes any
permission rule, hook entry or `disallowedTools` entry you added by hand, and
nothing is backed up.

Because of that pruning both commands are declared *consequential*: the CLI
framework asks `Proceed? [y/N]` before writing, and when there is no terminal
to answer at it refuses with `error: stdin is not interactive; pass
--approve-consequential to confirm`. A script or hook that means to reconcile
passes `--approve-consequential`. `--dry-run` is never gated.

## Rule reference

The table below is generated directly from the canonical rule set, so it always
reflects the guardrails shipped in this version. "Settings coverage" reports how
completely a rule's `deny`/`ask` glob(s) track its hook surface as 1 of 3
levels (FULL, PARTIAL, or NONE), or `n/a` for tiers with no settings backstop.

:-: table-guardrails

## Stripped tools

Beyond hooks and permission rules, claudewheel removes a set of Claude Code's
tools from every session it launches, via the `--disallowedTools` launch argv.
The operating principle is that less is more: every exposed tool is an
invitation for the agent to stray into it unnoticed during a long unattended
run, and the fewer tools the harness exposes, the more intelligently the model
calls the ones that remain.

The table below is generated directly from the canonical model, so it always
reflects the strip list shipped in this version.

:-: table-disallowed-tools

The list is a declaration, not a measurement: a name stays banned even while
the installed Claude Code version happens not to offer that tool (such an
entry is dormant insurance, not an error). For live numbers against the
installed binary -- baseline versus stripped tool counts, and which entries
are currently inert -- run `scripts/tool-strip-report`.
