# One confirmation-key discipline: y/n/ESC everywhere, three families unified

## The decided spec (owner-approved 2026-08-20, still standing)

A single `confirm()` component in `claudewheel/ui.py` becomes the one authority for
confirmation keys: renders a centered fullscreen page, LOOPS on read_key (unrecognized
keys — including ENTER — do nothing), returns exactly accept (`y`/`Y`), decline
(`n`/`N`), or skip (ESC / Ctrl-C = "not answering now", re-ask where that applies).
The temp-dir-cleanup snooze machinery is dead either way (the 7-day re-ask was rejected
as nagging): ESC skips this launch, nothing persisted; what `n` persists is the one
open sub-decision — per-directory dismissal (feature stays useful for new stale dirs)
vs a single kill switch.

Surfaces to convert (line references verified 2026-09-06): the hook-approval prompt and
the temp-dir cleanup prompt in `claudewheel/preflight.py` (both currently one-shot
`show_page` + `key in ("y","Y")`); the health warning in `claudewheel/cli.py` (cooked
`input()`); the delete-profile dialog and the saferm-install offer in
`claudewheel/app.py` (run_selection, Enter submits — Enter-on-cancel is safe today and
becomes inert under the spec); the deletion checklist's final keypress
(`claudewheel/deletion_checklist.py`) via a shared exported key mapping. Out of scope by
design: the vanilla/guardrails mode choice and the profile-inspect page (multi-action
pages, not confirmations).

## What the 2026-09-06 investigation added

- There are THREE accept-test disciplines in the repo, not one: the raw-key pair above;
  a cooked-line family `answer.strip().lower().startswith("y")` at five `claudewheel/
  cli.py` sites (saferm-install offer, and the four prompts of the `--resume`/`--cont`
  migration interceptions); and a strict exact-equality + int() parser in the
  multi-candidate branch of the cont interception — one function contains both a
  lenient and a strict idiom. The unification should absorb all three.
- Severity of the cooked family: two of the five sites fire the REAL session-store
  migration on a false accept (the others lead to dry runs or a checksum-verified
  install). All five advertise `[y/N]` yet accept any y-prefixed word ("yolo"
  confirms); none documents its accept rule, while the raw-key family documents its
  strictness — evidence the leniency is accidental at all five.
- Test-harness fact: the fake terminal helper returns ESC when its scripted keys run
  out, so an under-fed test of the new `confirm()` reads as a decline, not a hang —
  new tests should assert key-exhaustion explicitly where it would mask a bug.
- The sessions-overview prune (one keypress deletes all dead session rows, no
  confirmation) is a candidate seventh surface — decide whether it joins.

## Open decisions

1. y-only accept (the spec) vs Enter-confirms (current selection dialogs): muscle
   memory for one is a destructive misfire under the other; the spec's choice stands
   unless re-ruled — re-confirm at build time since both conventions currently coexist.
2. The scratchpad-cleanup `n` persistence choice (above).
3. Whether the cooked-line and int-parser families convert in the same pass (recommended:
   yes — the count of disciplines goes from three to one).

## Effort

One focused session: the component, the conversions, snooze removal, tests (ENTER
inert, three-way returns, exhaustion behavior, each converted surface). User-facing
changelog entry.

## Supersedes

confirm-key-semantics.md.
