# A global, per-hook disable switch

## Context

Every managed profile gets four canonical hook wirings, declared once in
`EXPECTED_HOOK_WIRINGS` (`claudewheel/guardrail.py:452-457`) and deployed as
generated bash from `claudewheel/hook_scripts.py:16-46`:

| Event | Matcher | Script | Kind |
|---|---|---|---|
| UserPromptSubmit | (none) | `hook-timestamp` | informational |
| PreToolUse | Agent | `hook-block-worktree` | blocking |
| PreToolUse | Bash | `hook-block-unsafe-commands` | blocking |
| PostToolUse | Bash | `hook-advise-commands` | advisory |

`_reconcile_hooks` (`claudewheel/reconcile.py:189-200`) replaces a profile's
entire `hooks` object with the canonical structure, so any hand-editing is
reverted on the next `patch-profiles` / `reconcile-permissions` run. That is
the intended contract — reconciliation is EXACT — and it means there is
currently no supported way to turn a hook off.

## Problem

There is no switch at any level. The only lever that exists belongs to Claude
Code, not to this project: a top-level `disableAllHooks: true` in a profile's
`settings.json` (a real setting, paired with `allowManagedHooksOnly`). It works
— reconciliation leaves it alone, because `reconcile_profile_dict`
(`reconcile.py:226-239`) touches only `hooks`, `claudewheel.disallowedTools`,
and `permissions`, leaving non-guardrail keys untouched — but it is the wrong
shape in three ways:

1. **Per-profile, not global.** Turning hooks off across the board means
   editing every profile's settings file by hand, and repeating that for every
   profile created afterwards.
2. **All-or-nothing.** There is no per-hook granularity. Silencing the advisory
   hook also removes both blocking hooks, which are the ones that stop
   worktree-isolated subagents and destructive bash patterns. The informational
   timestamp hook is the only one that is free to lose.
3. **Invisible to this project.** The flag lives outside the canonical model,
   so `health` and the reconciler have no idea a profile's hooks are inert.
   They will report the wiring as correct while nothing runs — a state the
   project's own diagnostics cannot describe.

What is wanted: one place that turns a *named* hook off for *all* profiles,
inside the canonical model so the reconciler and health checks agree about it.

## Solutions considered

### A. `disabled_hooks` list in `~/.claudewheel/config.json`

A list of script names (`["hook-advise-commands"]`). Canonical-hook
construction filters `EXPECTED_HOOK_WIRINGS` against it, so the wiring is
simply absent from every profile after reconciliation.

- Pros: global by construction; per-hook granularity; single source of truth;
  reconciliation and health stay authoritative because the disabled state is
  part of canonical, not a foreign key sitting beside it; new profiles inherit
  it automatically; `config.json` already carries comparable switches such as
  `health_check_on_launch`.
- Cons: makes the canonical model configurable, which softens the "EXACTLY
  canonical" contract that the reconcile rewrite was built to establish; an
  unqualified list lets a blocking hook be switched off as casually as an
  advisory one.

### B. Same as A, but only advisory and informational hooks may be disabled

Blocking hooks (`hook-block-worktree`, `hook-block-unsafe-commands`) are
refused with a hard error naming them.

- Pros: preserves the safety guarantees that justify the guardrail model while
  still allowing the noise reduction that motivates the request; the refusal
  documents which hooks are structural.
- Cons: someone will eventually want a blocking hook off, and the escape route
  is then a source edit; requires classifying each wiring as
  blocking/advisory/informational, which is not currently recorded next to
  `EXPECTED_HOOK_WIRINGS`.

### C. Disable by guardrail tier

Reuse the existing `Tier` enum (`guardrail.py:27`, values used at `:152`,
`:173`, `:187`, `:197` — HARD_DENY, ESCALATE, ADVISE, ASK) as the unit of
disabling.

- Pros: no new taxonomy; tiers are already the model's organising concept and
  already drive script generation (`:599`, `:612`, `:665`).
- Cons: tiers classify permission *rules*, not hook *wirings*, and the mapping
  is not one-to-one — the timestamp hook belongs to no tier at all. Disabling
  a tier would also affect generated permission rules, which is a wider blast
  radius than asked for.

### D. Write `disableAllHooks` into every profile from a claudewheel switch

A config key that causes reconciliation to set Claude Code's own flag in each
profile.

- Pros: no change to the canonical hook model; uses the client's own supported
  setting; trivially global.
- Cons: still all-or-nothing, so it does not deliver per-hook granularity; it
  also disables any non-claudewheel hooks a profile may have; and it makes this
  project write a foreign key whose semantics belong to the client and could
  change under it.

## Recommendation

B — A's mechanism with a refusal for blocking hooks — subject to confirming
whether a blocking hook should ever be disableable. If it should, A is the same
work minus the check.

## Affected files

- `claudewheel/guardrail.py` — `EXPECTED_HOOK_WIRINGS`, and a
  blocking/advisory/informational classification if B is chosen
- `claudewheel/defaults.py` — canonical hook construction, default config key
- `claudewheel/reconcile.py` — `_reconcile_hooks` filtering, and hook-script
  deployment so a disabled hook's script is not deployed
- `claudewheel/config.py` — new config key plus a versioned migration entry
- `claudewheel/health.py` — report disabled hooks rather than flagging the
  absent wiring as drift
- `claudewheel/cli.py` — surfacing the setting, if it gets a command
- `tests/` — the canonical-contract tests pin the exact hook structure and will
  need cases for the filtered form

## Effort

Small to medium. The filtering itself is a few lines in one place; most of the
work is the health-check behavior (so a disabled hook reads as intended rather
than as drift), the config migration, and updating the pinned canonical-contract
tests.
