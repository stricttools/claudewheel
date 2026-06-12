# Global orphan index for session directory rename detection

## Problem

The --cont interception only detects orphaned session dirs whose original cwd shares the same parent directory as the current dir. This is an artificial limitation — cross-parent moves (e.g., Projects/foo -> Work/foo) are missed entirely. The --resume interception does a full glob search so it catches everything, but --cont has no session ID to search for.

## Proposed solution

Maintain a persistent index (hashmap) of all orphaned project dirs in the shared store. On every launch or on demand, scan all project dirs, extract their cwds, check which no longer exist on disk, and cache the result. When --cont or --resume can't find sessions under the current dir, consult the index instead of doing a limited same-parent scan.

The index would be a JSON file (e.g., `~/.claudewheel/shared/orphan-index.json`) mapping encoded_cwd to real_cwd for all orphaned project dirs. Rebuilt on demand (e.g., during health check or on first miss).

## Supersedes

- `todo/inode-tracking.md` — inode tracking is complementary (proactive detection vs reactive lookup), but the orphan index solves the immediate problem of --cont missing cross-parent moves. Inode tracking could still be added later for proactive rename detection before sessions become orphaned.

## Key insight from session

The same-parent heuristic was a premature optimization. A full scan of the shared store takes ~50ms (291 dirs, extract cwd from first JSONL line). That's fast enough to do on every miss without caching. But caching makes it instant for repeated misses.
