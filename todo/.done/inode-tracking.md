# Proactive inode tracking for directory rename detection

## Problem

When a user renames a project directory, sessions stored under the old encoded path become orphaned. The current fix (v0.9.0) intercepts `--resume` and `--cont` at the point of failure and offers to move sessions. But this is reactive -- it only triggers when the user tries to access old sessions. Sessions can silently rot if the user just starts fresh sessions after a rename.

## Proposed solution

Record the inode of each project directory at launch time. On the next launch, if the current directory has a different path but the same inode as a previously-recorded entry, detect the rename proactively and offer to move sessions before the user even notices.

## Key findings from investigation (2026-06-09)

- Building an inode index of 3,268 directories under ~/Projects takes 12ms
- 100% inode uniqueness -- zero collisions, no hardlinks or bind mounts
- ~/Projects and ~/.claudewheel are on the same btrfs filesystem (device 44), so inodes are comparable
- Inodes survive renames within the same filesystem

## Design sketch

- Store `{path: str, inode: int, last_seen: str}` entries in a JSON file (e.g., `~/.claudewheel/shared/inodes.json`)
- On each launch, record the current directory's inode
- Before launch, check if any recorded inode matches the current directory but with a different path
- If match found: the directory was renamed. Offer to run `mv` automatically.
- Lightweight: 12ms overhead per launch for the inode lookup

## Scope

- Only covers same-filesystem renames (cross-filesystem moves change inodes)
- Does not help retroactively with already-orphaned sessions
- Complements the --resume/--cont interception (which handles the reactive case)
