# write_text_atomic uses a fixed temp path — concurrent writers race

## Context

`fsutil.py` (`write_text_atomic`, around lines 11-25, also used by
`write_json_atomic`) implements atomic writes as: write to
`path.with_suffix(".tmp")`, chmod to the original mode, `rename()` over the
target. The temp filename is therefore deterministic — e.g.
`shared-settings.json` is always staged through `shared-settings.tmp`.

This function runs on every launch via the `reconcile-guardrails` preflight
step, and `_reconcile_guardrails_run`'s docstring explicitly claims
"Concurrent launches racing on the same files are fine — the output is
idempotent."

## Problem

The claim is true of the *content* but not of the *temp file*. Two concurrent
launches (or a launch racing an explicit `reconcile --apply`) stage through
the same `.tmp` path:

- Writer A opens the tmp file and writes; writer B truncates and rewrites the
  same inode (or replaces it) mid-flight; A's `rename()` can then publish a
  file whose content was half-written by B — a torn write published
  atomically.
- Even in the benign interleavings, one writer renames a tmp file it did not
  fully author, so "atomic" no longer means "this writer's bytes."

Probability is low (requires near-simultaneous launches), but the failure
mode is corruption of settings files that every session on the machine
depends on, and the docstring actively asserts the race is safe.

## Solution

Use `tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")` for a unique
temp name, write, fsync, `os.replace()` onto the target, and propagate the
original file mode with `os.chmod` (or copy the mode before replacing).
Unique-per-writer temp names make concurrent writers fully independent; last
`os.replace` wins with each writer publishing only its own complete bytes.

Pros: eliminates the class; `os.replace` is the same atomicity guarantee.
Cons: none meaningful — leftover orphan temp files on crash are possible but
harmless (and can be prefix-matched and cleaned by the next writer).

Check for any code or tests that grep for the literal `.tmp` sibling name and
update them.

## Affected files

- `claudewheel/fsutil.py` (`write_text_atomic`, `write_json_atomic`)
- `claudewheel/preflight.py` (correct the "racing is fine" docstring or make
  it true)
- tests

## Effort

1-2 hours including a red-green regression test (two threads writing distinct
payloads through `write_text_atomic` to the same target in a loop; assert the
published file always equals exactly one of the two payloads — the current
implementation fails this).
