# Residual small decisions and config-tree housekeeping

Small items with no natural home in the larger files. Each is independent; most need
one ruling and minutes of work.

## Code decisions left open by shipped fixes

- **Atomic-write durability.** The 2026-09-06 rewrite fixed concurrency (unique staging
  file + atomic replace; the race was reproduced publishing spliced invalid JSON at
  real config sizes with a 0-2 ms window) but deliberately added NO fsync. Crash
  durability would add fsync of the file before the replace and of the parent directory
  after (measured ~8x per-write cost on this filesystem; the frequently-written state
  file is the hot spot). Options: leave as-is (concurrency-only), full durability
  everywhere, or durability only for tokens and settings. Ordering note if adopted:
  write, chmod, fsync file, replace, fsync directory — chmod before the file fsync so
  the mode is covered by the barrier. Filesystem notes: staging must stay in the
  target's own directory (cross-device replace fails); rename failure on NFS is
  ambiguous (re-stat, never assume).
- **The list-level malformed-value guard.** The dict-level guard now refuses loudly
  (per owner ruling), but a non-list INSIDE permissions (`"deny": null`) still crashes
  the apply step with a raw TypeError. Same defect class one level down; needs the list
  counterpart in the permission add/remove helpers plus red-green tests. (Also tracked
  as a rider in the settings-authority file.)
- **Mock-path guard placement.** A runtime refusal of mock objects at the effects
  boundary shipped 2026-09-06 (it is what catches ad-hoc probes — the class that
  actually produced a fake-path directory tree in the repo root on 2026-08-17, deleted
  since). If the owner prefers the alternative (a test-side ban on unspecced mock
  workspaces) the runtime guard can be reverted; otherwise this is closed.

## Config-tree housekeeping (~/.claudewheel — operator territory, tool proposes)

- **A live Google Maps API key sits inside an allow-rule string** in the two large
  profiles' settings. Rotate the key; strip the rule (it also should not survive any
  allow-policy cleanup).
- **Six foreign executables plus a bytecode cache in the deployed scripts directory**,
  none referenced by any hooks block anywhere: a superseded standalone guard script
  (predecessor of the generated blocker, with its compiled cache beside it), an unwired
  notification-sound set, and two unrelated utilities. One of them is wired by another
  project's local settings elsewhere on the machine, so deletion needs a quick check
  there first. Rulings: delete via saferm / keep; and should health report unknown
  executables in the scripts dir at all (today the drift check iterates only known
  names, so foreign content is invisible)?
- **Five stale hand-made backup files** (`*.bak-*` beside shared-settings and three
  profile settings, from pre-restructure hand edits of 2026-07/08). Nothing reads or
  prunes them; their guardrail content matches canonical. Keep as history or delete.
- **The operator's global rules file describes a retired layout**: a centralized
  tokens file that no longer exists (storage is per-profile now) and profile discovery
  keyed on a credentials file (discovery actually accepts settings or the data dir).
  Correcting that file is the operator's edit; the accurate description lives in the
  profile-store module docstrings.

## Standing operational reminder

After the next release the already-committed guardrail model changes need the operator
step to reach live profiles: `claudewheel deploy-hooks --all --force-overwrite`, then
`claudewheel reconcile-permissions --dry-run` and the real run. Until then the deployed
blocker still guards the retired safegit spelling and the live ask arrays still carry
its dead entry.

## Effort

Minutes per item once ruled.
