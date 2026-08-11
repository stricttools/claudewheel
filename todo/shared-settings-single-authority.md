# shared-settings.json as the single authority, with launch-time drift resolution

## Context

Today the canonical settings model lives in code (`defaults.build_canonical_shared_settings`, `guardrail.py`), and `shared-settings.json` is a derived artifact: reconcile rewrites it to match code on every launch, so hand edits to its guardrail keys are silently reverted. The per-launch reconcile (`preflight.py:311-329` → `reconcile_workspace`) covers only guardrail keys (`hooks`, `claudewheel.disallowedTools`, `permissions.deny/ask`, conflicting `allow` entries), runs silently inside `except Exception: pass`, and discards the full per-target change report it computes (`preflight.py:325`). `profileDefaults` seeds newly created profiles once (`wizard.py:266-269`) and is never re-applied.

Empirical state of the three live profiles: 16 of 20 top-level keys are byte-identical across all of them (`emergency` and `work` are byte-identical files); the differences are keys Claude Code itself writes at runtime (`model`, `enabledMcpjsonServers`, `remoteControlAtStartup`, and `permissions.allow` — 461/461/0 entries). Claude Code holds the profile settings.json open all session and appends to `permissions.allow` on every "don't ask again".

Motivating incident: the owner wanted `CLAUDE_CODE_DISABLE_ATTACHMENTS=1` in every profile and discovered there is no mechanism that shares a settings key with *existing* profiles — `profileDefaults` only reaches new ones. See `todo/disable-attachments-env-var.md`; this design supersedes its options 1 and 2 (once this lands, that env var is a one-line hand edit to shared-settings.json).

## Decided design (owner decisions)

All decisions below were made explicitly by the owner in a design session; the one marked [rec] was an accepted recommendation and is more weakly held than the rest.

1. **shared-settings.json becomes the single authority for ALL settings keys**, guardrails included (`hooks`, `disallowedTools`, `permissions.deny/ask`). The code-resident canonical model becomes bootstrap-only: used to materialize the file when it does not exist, never to overwrite it.
2. **Every key is shared. There are no profile-owned keys and no exclusion list.** Profiles are credentials plus nothing — the same user uses all of them and wants one setup. Keys CC writes mid-session (including `permissions.allow`) surface as drift at the next launch and get resolved through the prompt.
3. **Every launch checks ALL profiles against shared** (same fleet-wide scope as today's reconcile).
4. **Interactive drift resolution** [rec]: for each differing key, a prompt offers three actions — apply shared → profile, promote profile → shared, leave for this launch (drift persists, re-prompts next launch).
5. **Headless launches (`--print-prompt`) abort** with a hard error naming the drifted keys. No silent proceed, no silent heal.
6. **The reconcile report becomes visible.** The currently-discarded diff is what the prompt renders; the `except Exception: pass` swallow goes away.
7. **No tamper protection** beyond a warning at the top of the file. JSON has no comments; use a first key such as `"//": "DO NOT EDIT unless the user explicitly allowed it"`, preserved by all read-modify-write paths (claudewheel owns this file's serialization; Claude Code never reads it). Note the existing approved-hooks preflight step still hash-checks hooks at launch, so hook tampering retains a check through machinery that already exists.
8. **`profileDefaults` and the settings-seeding path are superseded.** A new profile gets credentials only; its settings.json materializes from shared. Delete the old path — pre-stable, no compat shims.
9. **Set-semantics comparison for order-meaningless lists** (`deny`, `ask`, `allow`): reordering is never drift. Keep today's `_reconcile_list` behavior (order preserved on apply, additions appended).
10. **`permissions.allow` churn policy**: start with prompting, with batch-promote (all of a profile's new allow entries in one keystroke). Revisit automatic set-union promotion only if prompting proves noisy in practice. (Open refinement, not a blocker.)

## Mechanism sketch

- Invert the authority direction in `reconcile.py`: shared file is read as truth; `build_canonical_shared_settings` is called only when the file is absent (bootstrap in `config.py:340-344` stays, its trigger condition unchanged).
- Repoint every reader of code-canonical to the file: `health.check_hooks_wired`, `check_settings_defaults`, `check_canonical_permissions_drift`, `check_shared_settings_drift` (this also collapses the current split where health compares against the file but reconcile compares against code — two reference points for the same data), `preflight.ensure_vanilla_guardrails`, hook-script deployment in `reconcile._referenced_scripts`, `wizard.py`, `docs/_directives/guardrail_table.py`.
- Replace the `reconcile-guardrails` preflight step with the drift-resolution step (`renders_ui=True`). Prompt construction follows the established `_prompt_*` pattern: `preflight._make_terminal`, `ui.show_page` / `ui.run_selection`, theme resolution, `FakeTerminal` in tests.
- Headless behavior follows the `_approved_hooks_run` precedent: abort with actionable text.
- Writes stay on `effects.write_json_atomic`. Re-read each profile file immediately before writing it (rather than reusing the copy read at check time) to shrink the clobber window against a live CC session — the fleet-wide scope decision keeps that race in principle, this makes it small.
- Keep the absolute-hook-path repathing (`merge_hooks` handling for a relocated workspace) — hooks contain workspace-rooted paths, and a naive equality check would flag every profile after a workspace move.
- Continue excluding the `~/.claude` pseudo-profile (`"default"`) from all of this, as today.
- Migration (one-time, part of implementation): materialize the authoritative shared file from the current de-facto state — `emergency`/`work` settings as baseline (byte-identical today); `hn`'s divergences resolve through the normal first-launch prompts. Then the motivating `"env": {"CLAUDE_CODE_DISABLE_ATTACHMENTS": "1"}` is an ordinary hand edit.
- Interaction with `todo/adopt-strictspec-for-owned-configs.md`: shared-settings.json is cw-owned and becomes more authoritative under this design — a natural candidate for that todo's document machinery. Per-profile settings.json stays out of scope there (CC's schema, CC's schedule), which this design is consistent with: cw never versions profile files, it only diffs them against shared.

## Open items

- **Rename `profile` → `account`** (or another name — needs owner approval, never assume). Rationale: under decision 2, a profile is credentials plus nothing, and the name should say so. Spans this project (CLI flags, `~/.claudewheel/profiles/` layout, wizard, docs) plus the two downstream consumers of the profile API (the streaming wrapper library and its terminal frontend, which pass `--profile`/`SessionConfig.profile` through to `resolve_profile`). Pre-stable everywhere: clean rename with a one-time directory migration, no dual recognition. Separable from this todo's core work.
- Allow-churn refinement (decision 10).
- Where per-profile reconcile metadata lives, if any is needed (the `claudewheel` namespace key inside profile settings.json vs `state.json`'s `OUT_OF_BAND_STATE_KEYS` pattern) — implementation choice.

## Affected files

`claudewheel/reconcile.py` (direction inversion, diff/report reuse), `claudewheel/preflight.py` (new step, step registration), `claudewheel/defaults.py` + `claudewheel/guardrail.py` (demoted to bootstrap), `claudewheel/health.py` (repoint four checks, unify reference points), `claudewheel/wizard.py` (seeding path removal), `claudewheel/config.py` (bootstrap trigger unchanged, comment key), `claudewheel/permission.py` (save path reuse), `claudewheel/cli.py` (`patch-profiles`/`reconcile-permissions` become the on-demand fleet sweep of the same mechanism), `claudewheel/ui.py` (reuse only), `docs/_directives/guardrail_table.py`. Tests: `test_reconcile.py`, `test_reconcile_preflight.py`, `test_shared_settings.py`, `test_preflight.py`, `test_health.py`, fixtures in `tests/wheelhelpers.py`.

## Effort

Medium-large. The inversion itself is moderate (the diff/report machinery exists and is currently discarded); the bulk is the prompt UX, the migration step, and reworking the tests that assert the current code-canonical direction. Roughly a full working session including tests and docs.
