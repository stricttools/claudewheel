# Two release-preflight gates are red on `tests/`

Found 2026-08-04 during the strictcli effects-regime migration, while verifying
that the migration had not broken anything. Both failures predate that work and
are unrelated to it; production code (`claudewheel/`) passes both cleanly.

## Context

`.rlsbl/config.json` declares four `external_checks`, all tagged `preflight`, so
all four run during `rlsbl release run`:

| Check | Paths | Status |
|-------|-------|--------|
| `mypy-strict` | `claudewheel`, `tests` | **FAIL** — clean on `claudewheel`, ~55 errors on `tests` |
| `ruff-check` | `claudewheel`, `tests`, `scripts`, `docs` | PASS (three latent errors fixed in the same visit) |
| `ruff-format` | `claudewheel`, `tests`, `scripts`, `docs` | **FAIL** — 25 files would be reformatted |
| `autospec-tests` | `tests` | PASS (0 bare patch sites of 797) |

Neither red gate is reachable through the test suite, so a green
`uv run pytest` says nothing about them. They only bite at release time.

## Problem 1: `mypy --strict` on `tests/`

`uv run mypy tests` reports roughly 55 errors. They fall into a few shapes:

- `tests/test_wizard.py` (~22) — a `FakeTerminal` assigned to a name mypy has
  already inferred as `MagicMock`, repeated at every setup site.
- `tests/test_vanilla_default.py` (~10) — untyped local helpers
  (`_opt_in`) plus `Returning Any from function declared to return
  dict[str, Any]` where a `json.loads` result is returned directly.
- `tests/test_preflight.py`, `test_reconcile.py`, `test_reconcile_preflight.py`,
  `test_approved_hooks.py`, `test_scratchpad_cleanup.py`, `test_tokens.py` —
  a handful each: missing `var-annotated` hints, `Missing type arguments for
  generic type`, and `assertAlmostEqual` called with an optional float.

None is a behavior bug. All are annotation debt in test code that grew under a
gate nobody was running.

## Problem 2: `ruff format --check`

25 files across `claudewheel/` and `tests/` are unformatted, including files
untouched for a long time (`claudewheel/app.py`, `claudewheel/session.py`,
`tests/test_workspace.py`). The repo has evidently never been run through
`ruff format`, only `ruff check`.

## Solutions

### A. Fix both, in two mechanical passes (recommended)

1. `uv run ruff format claudewheel tests scripts docs` — one commit, no
   semantic change, verified by the suite staying green.
2. Work the mypy list file by file. The `FakeTerminal`/`MagicMock` shape is one
   annotation on the attribute declaration and fixes 22 at once.

Pros: both gates go green and stay green; the annotation debt stops
accumulating; a future release is not blocked at the last step.
Cons: the format pass is a large, wide diff that will conflict with any
concurrent session's in-flight edits. It must be run when the tree is quiet.

### B. Narrow the gates to `claudewheel/`

Drop `tests` from `mypy-strict`'s paths and from `ruff-format`'s.

Pros: one config edit; gates go green immediately.
Cons: gives up type checking and formatting on 60% of the codebase, which is
where the mocks live and where drift is hardest to see. This is deleting the
thermometer.

### C. Fix formatting only, narrow mypy only

Format everything (cheap, mechanical), and scope `mypy-strict` to
`claudewheel` until the test annotations are worked through, then widen it back.

Pros: banks the cheap win; keeps the expensive one visible as a smaller task.
Cons: "widen it back later" is exactly how this debt accumulated.

## Affected files

- `.rlsbl/config.json` — the `external_checks` block
- `tests/test_wizard.py`, `tests/test_vanilla_default.py`,
  `tests/test_preflight.py`, `tests/test_reconcile.py`,
  `tests/test_reconcile_preflight.py`, `tests/test_approved_hooks.py`,
  `tests/test_scratchpad_cleanup.py`, `tests/test_tokens.py`
- 25 files for the format pass (`uv run ruff format --check` lists them)

## Effort

Formatting pass: minutes, plus a suite run.
mypy pass: a few hours, front-loaded on `test_wizard.py`.
