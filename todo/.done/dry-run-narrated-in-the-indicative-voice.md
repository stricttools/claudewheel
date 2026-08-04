# Several handlers narrate a dry run as though it happened

Found 2026-08-05 while verifying `--dry-run` behaviour during the confirmation
protocol re-sweep. `c --dry-run install` had this exact bug and was fixed; the
fix was not carried to its siblings.

## Symptom

The framework's would-do log is correct and nothing is written, but the
handler's own output above it is in the indicative past tense, so the first
lines a user reads claim the mutation happened:

```
$ c --dry-run profile delete throwaway --no-force-delete --no-force-delete-data
Deleting profile 'throwaway'...
  Removed dir: 0 symlinks unlinked, 2 real entries removed
  Not found in options.json (already clean)
  Not found in tokens.json (already clean)
Profile 'throwaway' deleted.          <-- it was not
DRY RUN — no changes were made. Would do:
  1. remove: .../profiles/throwaway/settings.json
  ...
```

```
$ c --dry-run profile rename alpha beta
Renamed profile 'alpha' -> 'beta'.    <-- it was not
DRY RUN — no changes were made. Would do:
  ...
```

```
$ c --dry-run deploy-hooks --all
created: .../scripts/hook-advise-commands     <-- nothing was created
created: .../scripts/hook-block-unsafe-commands
...
DRY RUN — no changes were made. Would do:
  ...
```

## Confirmed affected

- `profile delete` (`cli._handle_delete_profile`)
- `profile rename` (`cli._handle_rename_profile`)
- `deploy-hooks` (`cli._handle_deploy_hooks`)

## Not affected (the correct shape)

- `stats` (`stats.run_stats`) takes `dry_run` and switches verb: `"would
  remove"` / `"removed"`, and opens with `[stats] DRY RUN -- no changes will be
  made`.
- `install` — fixed in commit `9f0cb6f`.
- `reconcile-permissions` / `patch-profiles` — `reconcile._print_report` takes
  `dry_run` and prints `would reconcile` / `reconciled`.

## Unverified, same shape, worth checking in the same pass

`mv`, `migrate`, `import`, `uninstall`, `reset-options`, `permission add`,
`permission remove`, `profile fix-auth`.

## Why it matters

`--dry-run` exists so a caller can find out what a command would do without
doing it. A preview whose own narration says the thing happened defeats that
for the reader who does not scroll to the would-do log — and an agent reading
the first line of stdout will report success.

It is also a correctness asymmetry that will keep recurring: the handlers that
got it right are the ones that were passed `dry_run` explicitly, and the ones
that got it wrong are the ones that consult `effects.previewing()` (or nothing
at all) only where they mutate, not where they narrate.

## Solutions

### A. Fix each handler individually (matches the `install` precedent)

Thread `effects.previewing()` into the narration and switch verb, as
`stats.run_stats` and `reconcile._print_report` already do.

Pros: small, obvious, testable per command; no new machinery.
Cons: it is the same edit N times, and the next handler added will get it
wrong again for exactly the reason the current ones did. Effort: ~2 hours for
the three confirmed plus the eight unverified.

### B. Make the narration structurally unable to lie

Route handler-facing success messages through a small helper that takes the
past-tense and conditional forms and picks by `effects.previewing()` — e.g.
`effects.narrate(did="Removed dir: ...", would="Would remove dir: ...")`.
A test can then walk the handlers and fail on any bare `print` of a
mutation-describing string.

Pros: closes the class rather than the instances, which is what the repeated
recurrence argues for; the guard is mechanical instead of a review habit.
Cons: touches every handler's output path; the "mutation-describing string"
predicate for the guard test is a heuristic and will need an exemption list.
Effort: a day.

## Affected files

`claudewheel/cli.py` (the handlers), `claudewheel/effects.py` (option B),
`tests/test_dry_run_records.py` (the natural home for the assertions).
