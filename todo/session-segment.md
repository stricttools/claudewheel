# Session segment

## Context

Claude Code stores conversation sessions per-profile in its `CLAUDE_CONFIG_DIR`. Two flags control session restore on launch:

- `--continue` / `-c` : resume the most recent conversation
- `--resume` / `-r` : open Claude Code's interactive session picker

claudewheel already supports both as passthrough flags (`./c -c`, `./c -r`). This TODO is about a more integrated approach: a TUI segment that lists recent sessions for the currently selected profile, with metadata, so the user can pick a specific session from the launcher itself before Claude Code starts.

## Problem

Today the user has three options for session management:
- Launch a fresh session (default)
- Continue the most recent session (one keystroke, no choice)
- Hand off to Claude Code's resume picker (extra navigation step inside Claude after launch)

What's missing: previewing and selecting a specific older session **before** committing to a launch, in the same TUI as the other config segments. Useful when:
- You want to resume a session from yesterday but not the very latest one
- You want to see the first prompt or timestamp of recent sessions to remember which is which
- You want session selection to be reactive to the profile choice (different profiles have different session histories)

## Solutions

### A. Simple session segment with auto-discovery

Add a `session` segment with values `[new, continue, <session-id-1>, <session-id-2>, ...]`. The launcher scans the active profile's session storage and populates the list. Selecting `new` is the default (no flag). Selecting `continue` adds `--continue`. Selecting a specific session ID adds `--resume <id>` (or `--session <id>` if such a flag exists).

- **Pros**: Unified with other segments, profile-aware, no need to memorize session IDs
- **Cons**: Requires reverse-engineering Claude Code's session storage format (path, file structure, metadata), which is undocumented and may change. Session IDs are usually opaque hashes, so display values need metadata enrichment (timestamp, first user message, model, length)

### B. Session segment with metadata-rich display

Same as A, but each session is displayed as `2h ago - "Fix the segment renderer"` instead of a raw ID. The launcher reads the session file's first user message and timestamp.

- **Pros**: Much more usable, the segment becomes self-explanatory
- **Cons**: More parsing work, session files might be large (~MBs) so we'd need to read just the head, and the format could change between Claude Code versions

### C. Defer to Claude Code's picker but pre-launch

Don't list sessions in the TUI. Just have a `session` segment with `[new, continue, picker]` where `picker` adds `--resume` (which triggers Claude Code's own picker). This is barely better than the current passthrough flags.

- **Pros**: No reverse engineering, version-resilient
- **Cons**: Almost the same as the current `-r` passthrough, doesn't add real value

## Recommendation

Option B if pursued. Option A is a stepping stone but has poor UX without metadata. Option C is not worth implementing over the existing `-c` / `-r` passthrough.

## Reactivity concerns

The session list depends on which profile is selected. The cross-segment `requires` system already handles this kind of reactivity for option availability (e.g. `auto` permission gated on version). Sessions are different: not just availability but the actual list of options changes. Two ways:

- Re-discover sessions whenever the profile segment changes (recompute on every render -- might be slow if session dirs are large)
- Cache per-profile session lists in `state.json` and refresh on a TTL or on demand

The latter is more reliable. The npm version cache is a precedent (see `fetch_npm_versions` in `segment.py`).

## Where Claude Code stores sessions

Investigation needed. Likely candidates inside `CLAUDE_CONFIG_DIR`:
- `sessions/` directory with one file per session
- A SQLite database
- JSON files keyed by session ID

`strings` on a Claude Code binary should reveal the path. Check `~/.claude-personal/` or whichever profile has been used most.

## Files likely affected

- `claude_launcher/defaults.py` -- add `session` segment definition and options entry
- `claude_launcher/segment.py` -- new discovery type, e.g. `claude_sessions`, with profile-aware logic
- `claude_launcher/launch.py` -- map `session` value to `--continue` / `--resume <id>` flags
- `claude_launcher/app.py` -- trigger session re-discovery when the profile segment changes (cross-segment dependency)
- `claude_launcher/themes/*.json` -- new color block for the session segment

## Relative effort

Medium. The session storage reverse-engineering and the cross-segment reactivity (re-discover when profile changes) are the unknowns. If sessions live in a simple format (JSON files), it's a half-day. If we need to parse a SQLite database or a binary format, more.

The cross-segment reactivity is a small-but-real generalization of what we have now: option lists were treated as static after the first `discover_options()` call. We'd need a "rediscover on event" mechanism, scoped to which segment changed.
