# reconcile-guardrails silently prunes user-added hook entries from shared-settings.json

## Context

Every launch runs the `reconcile-guardrails` preflight step unconditionally
(`preflight.py`, `PREFLIGHT_STEPS` registration around line 597; it is not
gated by `health_check_on_launch`). The step calls
`reconcile_workspace(ws, dry_run=False, profile=None)`, which reconciles
`shared-settings.json`. In `reconcile.py`, `_reconcile_hooks()` (around line
188) performs whole-subtree replacement — `container["hooks"] =
deepcopy(canonical_hooks)` — and its docstring states outright that
"user-added hook entries are pruned."

## Problem

Any hook entry a user (or an agent acting for the user) adds by hand to
`shared-settings.json` is silently destroyed on the next launch — typically
within minutes on a machine with frequent launches. Three compounding issues:

1. **Zero output.** `_reconcile_guardrails_run()` (preflight.py around line
   308) prints nothing when it prunes. The edit simply evaporates; from the
   user's perspective the config was never added, and debugging the
   disappearance requires reading reconcile internals.
2. **Swallowed failures.** The step body is wrapped in a bare
   `except Exception: pass`, and `_process_settings_file()` captures write
   errors into a report object that nothing prints. A genuine write failure is
   indistinguishable from success.
3. **Doctrine violation.** The ecosystem rule is "hard errors, not warnings"
   and "no silent degradation." Silently discarding user configuration is the
   textbook opposite: same input, silently different outcome depending on
   whether a reconcile ran in between.

The pruning itself may well be the intended design (the file is a tool-owned
fleet template), but then hand edits should be *impossible or loud*, not
possible-and-futile.

## Solutions

1. **Report what was pruned.** On every reconcile that changes a settings
   file, print a diff-level summary ("pruned hook entry X from
   hooks.PreToolUse[Bash]"). Pros: minimal change, immediate observability.
   Cons: still allows the futile-edit workflow; the message appears in a
   launch that may scroll past.
2. **Refuse and error on drift.** If the on-disk file differs from canonical,
   fail the preflight with a message telling the user to run an explicit
   `reconcile --apply` (or to move their customization to its proper home).
   Pros: matches the hard-error doctrine exactly; nothing is ever silently
   lost. Cons: a stray edit blocks launches until resolved; needs an escape
   path for legitimate fleet-template evolution.
3. **Make the file mechanically unforgeable.** Since it is 100% tool-owned:
   write it `chmod 444` with a generated-file marker key, exactly like the
   ecosystem already does for selfdoc-generated root files and released JSONL
   changelogs. Hand edits then fail loudly at write time instead of
   succeeding and evaporating. Pros: prevents the whole class at the source;
   consistent with existing patterns. Cons: reconcile itself must chmod
   around its own writes; third-party tooling that legitimately edits the
   file (if any) breaks — arguably the point.
4. **Fix the exception handling regardless.** Replace the bare
   `except Exception: pass` with logging/reporting; surface
   `_process_settings_file` errors. This is worth doing under any of the
   options above.

Recommended combination: 3 + 1 + 4 (unforgeable file, report on prune during
the transition, loud failures). Option 2 is the strictest alternative if
launch-time friction is acceptable.

## Affected files

- `claudewheel/preflight.py` (`_reconcile_guardrails_run`, step registration)
- `claudewheel/reconcile.py` (`_reconcile_hooks`, `_process_settings_file`)
- `claudewheel/fsutil.py` (mode handling if option 3 is chosen)
- tests for reconcile/preflight

## Effort

Half a day including red-green tests (a test that hand-adds a hook entry,
runs reconcile, and asserts the outcome is loud — currently it would assert
silent deletion).
