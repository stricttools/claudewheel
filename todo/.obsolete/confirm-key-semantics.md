# Unified y/n/ESC semantics for all confirmation screens

## Context

A user launched a session and reflexively pressed Enter on the scratchpad-cleanup
preflight page without reading it. No harm was done (Enter took the decline
branch and only wrote the 7-day snooze), but the incident exposed two problems:

1. Confirmation pages read exactly ONE keypress and interpret whatever arrives
   (`ui.show_page` returns a single `terminal.read_key()`). Any key dismisses
   the page, so an accidental Enter silently takes some branch instead of doing
   nothing.
2. There is no shared confirmation component. The accept test
   `key in ("y", "Y")` is duplicated per call site, and several other surfaces
   confirm on Enter outright.

## Decided spec (user-approved)

One `confirm()` component in `ui.py` becomes the single authority for
confirmation key semantics:

- Renders the page (same centered fullscreen style as `show_page`).
- Loops on `read_key()` instead of one-shot: unrecognized keys (including
  ENTER) do nothing — redraw and keep waiting.
- Returns exactly one of three results:
  - **accept** — `y`/`Y`
  - **decline** — `n`/`N`
  - **skip** — `ESC` (and `CTRL_C`): "not answering now", ask again later
    where that concept applies.

All confirmation surfaces route through it:

| Surface | Today | Target behavior |
|---|---|---|
| Hook approval (`preflight.py:463-493`, decision at `:493`) | `y` accepts, any other key declines and aborts launch | accept = approve (persisted as today); decline and skip both abort the launch (decline stores nothing, so it re-asks next launch naturally) |
| Scratchpad cleanup (`preflight.py:558-591`, decision at `:591`; snooze write at `:659-660`) | `y` deletes, any other key = skip + 7-day snooze | accept = delete; decline/skip per the open decision below; **snooze machinery removed** |
| Health warning (`cli.py:167-181`) | cooked-mode `input()`, "Press Enter to continue or Ctrl-C to abort" | convert to raw-mode `confirm()`: accept = continue, decline/skip = abort ("ask later" does not apply — it is informational) |
| Delete-profile dialog (`app.py:1218-1225`) | `run_selection` with Cancel pre-focused, Enter submits | convert to `confirm()` |
| saferm-install offer (`app.py:1442-1451`) | same pattern | convert to `confirm()` |
| Deletion checklist (`deletion_checklist.py:249-267`, Enter confirms at `:264`) | Enter starts stopping processes | keeps its live-updating rendering (it is a progress screen, not a static page), but the final "proceed?" keypress uses the same exported key-decision mapping from `confirm()` so semantics cannot drift |

Out of scope, deliberately: the vanilla/guardrails page
(`preflight.py:236-269`, accepts `g`) is a choice between modes, not a
confirmation — leave it untouched. The profile-inspect page
(`app.py:1127-1148`) is likewise a multi-action page, not a yes/no
confirmation.

## Open decision: scratchpad `n` semantics (snooze is dead either way)

The user rejected the snooze concept ("re-ask every 7 days forever" is
nagging). `ESC` = skip this launch, re-ask next launch, nothing persisted.
What `n` persists is undecided; two candidate designs:

1. **Per-directory dismissal** (session recommendation, not yet chosen):
   record the dismissed paths in `state.json`; those directories never prompt
   again, a NEW directory going stale later still does. Keeps the feature
   alive without nagging about something already declined.
   - Pro: proportionate; feature stays useful.
   - Con: a new persisted list to maintain; dismissed-dir entries for deleted
     dirs need occasional pruning.
2. **Feature kill switch**: `n` sets one boolean, scratchpad cleanup never
   prompts again about anything.
   - Pro: simplest possible state.
   - Con: first `n` permanently disables a feature the user may want for
     future stale dirs, with no visible way back except editing `state.json`.

Either way: delete `scratchpad_snooze_until` handling and
`SCRATCHPAD_SNOOZE_DAYS` (`scratchpad.py:30`); this is a pre-stable project,
so no recognition of the old state key — remove it outright (a stale key left
in an existing `state.json` is harmless dead data).

Ask the user before implementing if still undecided.

## Affected files

- `claudewheel/ui.py` — new `confirm()` beside `show_page` (`ui.py:453-491`);
  export the key-decision mapping for the deletion checklist.
- `claudewheel/terminal.py` — no change expected; `read_key()`
  (`terminal.py:107-181`) already returns `"ENTER"`, `"ESC"`, `"CTRL_C"`,
  literal chars.
- `claudewheel/preflight.py` — `_prompt_hook_approval`,
  `_prompt_scratchpad_cleanup`, `_scratchpad_cleanup_run` (snooze removal).
- `claudewheel/scratchpad.py` — snooze constant removal; possibly the new
  dismissal record.
- `claudewheel/cli.py` — health-warning prompt conversion from cooked
  `input()` to raw `confirm()`.
- `claudewheel/app.py` — the two dialog conversions.
- `claudewheel/deletion_checklist.py` — key loop uses shared mapping.

## Tests

Existing coverage to update (all use `tests/wheelhelpers.py:65`
`FakeTerminal` key feeding):

- `tests/test_scratchpad_cleanup.py` — snooze assertions all die; new
  assertions: ENTER is ignored (page keeps waiting), `n` persists the chosen
  decline state, `ESC` persists nothing.
- `tests/test_approved_hooks.py` — add ENTER-is-ignored case.
- `tests/test_ui.py` — new `confirm()` unit tests: accept/decline/skip
  returns, unrecognized keys loop.
- `tests/test_deletion_checklist.py` — Enter no longer confirms.
- New coverage for the converted `cli.py` health prompt and the two `app.py`
  dialogs.

Red-green order: write the ENTER-is-ignored / three-way-result tests first,
watch them fail, then implement.

## Effort

Small-to-medium: one new UI component, six call-site conversions, one state
mechanism swap (snooze -> chosen decline state), test updates. Roughly one
focused session. Needs a `rlsbl changelog add` entry (user-facing: `--type
feature` or `fix` — confirmation keys change behavior users see).
