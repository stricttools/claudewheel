+++
title = "Guardrails"
description = "How claudewheel guardrails work: the 4 enforcement tiers, subagents versus the main agent, command-string caveats, the heavy wrapper and its memory budget, the memory ceiling of each session, upgrading profiles, and stripped tools."
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
heavy --mem 8G -- scripts/full-suite.sh
```

Every heavy command declares its memory cap (`--mem`, 5G unless given), and
`heavy` starts it only when that cap fits in a machine-wide memory budget, so
heavy commands from every session run side by side as long as the memory is
there and wait only when it is not. A job that needs less, such as a single
test or a small build, starts sooner with a smaller cap
(`heavy --mem 2G -- go test -run TestOne ./pkg`). The rule is checked when the command would
start:

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
the running commands and how to stop them, and suggesting a smaller `--mem` or
a longer `--max-wait`.

The command runs in its own systemd user scope, named `heavy-<pid>-<time>.scope`,
capped at 5G of memory with no swap (`--mem` sets another cap, a whole number
with a `K`, `M`, `G`, or `T` suffix) and with `CPUWeight=20` (the default is
100), so interactive work stays responsive; its arguments reach it unchanged,
`$` included. `heavy` prints the cap on every
run (`heavy: capped at 5G`). When the command is killed for going over the
cap, `heavy` reads that from the scope's result and prints
`heavy: killed at the 5G memory cap; rerun with 'heavy --mem 10G -- <command>'`;
the scope is then cleared, so none accumulate. `heavy` adds `-p=2` to
`GOFLAGS` unless `GOFLAGS` already sets `-p`, and prints a line when it does;
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

## A memory ceiling for each session

The `heavy-unwrapped` rule matches command names, so a heavy command it cannot
see, such as a test suite started inside `bash -c`, still runs unwrapped. So
that such a command can take down only its own session, claudewheel starts
every session it launches in its own systemd user scope with a memory ceiling:

```bash
systemd-run --user --scope --quiet --collect --expand-environment=no \
    --unit=claudewheel-session-<pid>-<time>.scope \
    -p MemoryMax=4G -p MemorySwapMax=1G -p OOMPolicy=continue -- <the client command>
```

`systemd-run` puts the client in the scope and then becomes it, so the session
keeps claudewheel's process ID, terminal, and environment, and
`--expand-environment=no` passes the client's arguments through with any `$`
in them untouched. claudewheel prints
the ceiling at every launch:

```text
claudewheel: this session runs in claudewheel-session-<pid>-<time>.scope, capped at 4G of memory and 1G of swap (session_memory_max and session_memory_swap_max in config.json)
```

- **The ceiling** is `session_memory_max` in `config.json`, 4G unless set. A
  session's Claude Code process has been measured at under 1G at its peak over
  days of work, so 4G leaves room for the commands its agents run outside
  `heavy` (searches, git, package resolution, small scripts).
- **Swap** is `session_memory_swap_max`, 1G unless set, or `0` for none. A
  little swap lets an idle session's cold memory be compressed into zram
  instead of holding RAM; more would let a runaway push gigabytes into swap,
  slowing the whole machine, before the ceiling stopped it.
- **`OOMPolicy=continue`**: when the scope reaches its ceiling, the kernel
  kills the largest process in it, which is the runaway command rather than
  Claude Code, and the session carries on. Without it, systemd would stop the
  whole scope on the first kill.

A `heavy` command started from inside a session is not counted against the
session's ceiling: the user manager places every scope it starts beside the
others in its own slice, not inside the scope of the process that asked, so the
`heavy` scope is a sibling of the session scope and only its own `--mem` cap
limits it.

Both keys take a whole number with a `K`, `M`, `G`, or `T` suffix; any other
value fails the launch with `Launch failed:` and the key's name. `systemd-run`
must be on the launch `PATH`, and the launch fails if it is not. A session
already running keeps the scope, or the absence of one, it was launched with;
the ceiling applies from its next launch.

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
