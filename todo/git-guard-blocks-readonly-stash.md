# The git guard blocks read-only stash subcommands

## Problem

The git-command guard blocked `git stash list` — a purely read-only inspection — with
the standard instruction to report the attempt. The matcher apparently triggers on the
`stash` subcommand as a whole rather than on its destructive forms (`push`, `pop`,
`apply`, `drop`, `clear`).

## Two readings, both defensible

- **Overreach — narrow the matcher.** `stash list` and `stash show` mutate nothing;
  blocking them costs a session an inspection tool and produces a false "destructive
  attempt" report. Allow the read-only subcommands, keep blocking the rest.
- **Deliberate — keep the blanket ban.** The fleet workflow bans stash as a mechanism
  outright (commit-on-a-branch instead), and a session with no ability to create
  stashes has little legitimate reason to list them; a blanket ban is simpler and the
  matcher cannot be bypassed by subcommand aliasing. If this is the intent, the block
  message could say "stash is banned as a workflow, including inspection" so the report
  instruction stops implying a destructive attempt was made.

## Affected

The guard hook's git-command matching (shared settings).

## Effort

Minutes either way; the decision is the work.
