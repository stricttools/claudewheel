# Guard bare mv in repos: the hook becomes safemv

## Context

The guardrail hooks already intercept Bash tool calls and pattern-match
command lines to steer sessions toward the sanctioned tools (git add ->
safegit commit, rm -> saferm). Bare `mv` is currently unguarded. safegit is
gaining a declared-move verb (`safegit mv 'old -> new'`) that performs the
filesystem move and commits it with a durable move record; once that ships,
a bare `mv` inside a repo silently produces exactly the unrecorded,
heuristic-dependent move the safegit feature exists to eliminate.

## Problem

Sessions will keep typing `mv a b` out of habit. Without a guard, tracked
files move with no record and no commit; with a naive regex guard, exotic
invocations (`find -exec mv`, `xargs mv`, `/bin/mv`, compound commands)
slip through, and legitimate non-repo moves get blocked.

## Design

Upgrade the Bash-intercepting hook (no new binary): the hook body parses
the command (shlex, not regex -- regex only stays as the cheap trigger),
recognizes plain `mv` invocations (including `-t` form and multiple
sources), resolves each source and the destination to its repository via
`git -C <dir> rev-parse --show-toplevel`, and applies three-way logic:

1. **Source tracked, source and destination in the same repo** -> block,
   print the exact replacement: `safegit mv '<old> -> <new>'` (or the
   subtree form for directories).
2. **Source tracked, destination outside that repo (or in a different
   repo)** -> block with the honest two-step explanation: this is a
   deletion here plus a creation there, not a move safegit can record;
   copy/move out, then commit the deletion through the sanctioned flow.
3. **Source untracked, or both sides outside any repo** -> allow bare mv
   unchanged.

**Fail closed on ambiguity:** a compound or unparseable command containing
an mv token is blocked with "rephrase as a plain mv or use safegit mv" --
an agent rephrases trivially, and the failure polarity matters: a
parse-based guard that fails closed catches what a regex-only guard fails
open on.

## Considered and rejected

- **A standalone safemv binary** that refuses in repos and redirects: it
  duplicates the hook's logic and only helps non-hooked shells (human
  terminals), which are not the consumer here. Trivial to add later if
  that changes.
- **Regex-only detection**: unreliable against shell reality in exactly
  the wrong direction (fails open).

## Sequencing

Blocked until safegit ships `safegit mv` (the campaign in flight as of
this filing). The hook text should match the shipped syntax exactly.

## Affected areas

- The Bash guardrail hook (guardrail.py or its successor in the shared
  hooks) -- add the mv interception with parse-based analysis
- Shared-settings hook registration if a new hook entry is needed
- Hook tests, if the guardrail has a test surface

## Effort estimate

Small-medium: the parsing and three-way resolution are the work; the
blocking mechanics already exist in the current guardrail pattern.
