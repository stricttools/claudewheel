# `reconcile-permissions` and `patch-profiles` have no explicit-intent gate any more

Found 2026-08-05 while re-sweeping onto strictcli's redesigned confirmation
protocol. This is a behaviour regression that arrived in two steps, neither of
which was wrong on its own.

## What was lost

`reconcile-permissions` used to require **exactly one** of `--dry-run` or
`--apply`. A bare invocation was a parse error, so it could not write by
accident. That pair was hand-rolled: `--dry-run` was claudewheel's own flag,
and `--apply` existed purely so the write needed a second, deliberate token.

Step 1 (the effects-regime migration): `--dry-run` became the framework's, so
claudewheel could not keep its own. `--apply` was deleted at the same time and
the explicit-intent guarantee was handed to strictcli's confirm protocol, which
at the time prompted for every `mutating` command. The handler docstring and
the command help text both said so, and `tests/test_reconcile.py` pinned it
(`test_unconfirmed_non_tty_writes_nothing`).

Step 2 (this sweep): the blanket confirm protocol was replaced. The framework
now prompts only for commands that declare `consequential=True`, and
`reconcile-permissions` does not declare it — reconciling to canonical is this
tool's routine maintenance job, and 63% of fleet commands classified `mutating`,
so the inferred prompt was noise at a ~1:10 signal-to-noise ratio.

Net effect: a bare `c reconcile-permissions` now rewrites **every** managed
profile's `settings.json` plus `shared-settings.json` immediately, with no
prompt, no second token, and no undo. `c patch-profiles` is the same operation
through a second name (both delegate to `reconcile.run_reconcile`).

## Why it matters

The reconciliation is *exact*, not additive: it prunes user-added permission
rules, user-added hook entries and `disallowedTools` drift across every profile
at once. Nothing is backed up. `todo/reconcile-silently-prunes-user-hooks.md`
describes the sharpest edge of that already.

The tool is also frequently invoked from scripts and by agents, which is
precisely the population that would have benefited from a second token and is
now the population that can destroy a profile's customizations with one.

## Options

### A. Declare the two commands `consequential=True`

Pros: zero new machinery; the framework's non-TTY refusal (`pass
--approve-consequential to confirm`) is exactly the second token that was
lost, and it is the same mechanism `profile delete` already uses.
Cons: contradicts the fleet-level judgement that routine maintenance commands
should not prompt, and the prompt is blind — it says "about to run
consequential command 'reconcile-permissions'. Proceed? [y/N]" and nothing
about what will change, which is strictly less informative than the
`--dry-run` diff the user should have run first. Cost: one registration
keyword plus a test row.

### B. Reinstate a flag-granular seam in the handler (the safegit precedent)

`safegit` guards `doctor --uninstall` with its own in-handler confirmation
rather than declaring the whole command consequential, so the harmless
invocation stays quiet. The analogue here is a required
`--prune-user-entries` / `--no-prune-user-entries` bool: the reconciliation
runs either way, but the destructive half (removing entries that are not
canonical) needs the explicit token.

Pros: the token names the actual hazard instead of the command; a
non-destructive reconcile (add missing canonical entries only) becomes a
first-class, safe invocation; agents reading the flag learn what is at stake.
Cons: reintroduces a hand-rolled gate the effects regime deliberately
collapsed; splits the command's behaviour into two modes that both have to be
tested. Cost: a required bool flag, a branch in `reconcile.reconcile_workspace`,
and roughly a dozen test rows.

### C. Make the operation recoverable instead of gated

Snapshot each target `settings.json` into a timestamped backup before writing
and print the restore path. No prompt at all.

Pros: removes the need for a gate rather than restoring one; helps every
caller, including the ones who would have consented anyway.
Cons: new on-disk state to grow, prune and document; does not stop the
destruction, only makes it reversible; the user has to notice the printed path.
Cost: a backup helper, a retention policy, and a test that the backup round
trips.

### D. Accept it

The command's whole purpose is to make profiles canonical, `--dry-run` prints
an exact per-target diff, and anyone running it is asking for exactly this.
Pros: nothing to build.
Cons: the guarantee that used to exist is gone and nothing records that it was
a deliberate removal rather than an accident of two migrations.

## Affected files

- `claudewheel/cli.py` — `_handle_reconcile_permissions`, `_handle_patch_profiles`,
  and the two `app.command(...)` registrations
- `claudewheel/reconcile.py` — `run_reconcile`, `reconcile_workspace`
- `claudewheel/patch_profiles.py` — the delegate
- `tests/test_reconcile.py` — `ReconcileCliTests`
- `tests/test_effects_binding.py` — the `CONSEQUENTIAL` set, if option A
- `docs/_CLAUDE.md` — the "Confirmation and preview" section names the current set

## Effort

A: ~15 minutes. B: half a day. C: half a day plus a retention decision.
D: a decision, no code.
