# profile delete leaves daemon husks (empty dirs resurrect; health flags orphans)

## Context

Two profiles deliberately deleted via `claudewheel profile delete` (2026-08-07) keep
resurrecting as EMPTY directories under ~/.claudewheel/profiles/, because live background
processes launched under those profiles still hold CLAUDE_CONFIG_DIR pointing there and
recreate the path. `claudewheel health` then flags orphan-profiles forever; re-deleting the
husks is proven futile while the processes live.

## Problem

`profile delete` removes state but does not account for the profile's running processes:
the deletion is immediately half-undone, and the health signal becomes permanent noise —
exactly the alarm-fatigue pattern the guardrails are designed against.

## Solutions

- (a) delete enumerates the profile's live processes (session registry / CLAUDE_CONFIG_DIR
  in /proc environs) and refuses with a list until they exit — explicit, no killing.
- (b) delete records a tombstone; health distinguishes "tombstoned husk with live holdout
  processes" (info, names the PIDs) from a genuine orphan (warning).
- (a)+(b) compose: refuse-by-default with the PID list, tombstone for the race window.

## Effort

Small-medium; the process-enumeration seam is the only new mechanics.
