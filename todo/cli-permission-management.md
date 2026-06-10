# CLI commands for managing profile permissions

## Context

Profile permissions live in `~/.claudewheel/profiles/<name>/settings.json` under the `permissions` object with `allow`, `deny`, and `ask` arrays. Currently the only way to modify them is to hand-edit the JSON file.

## Problem

AI agents that need to add a permission (e.g. adding `safegit push` or `saferm purge` to the `ask` list) have to open the JSON file and edit it in-place. This is error-prone — agents can corrupt the JSON structure, lose formatting, accidentally remove entries, or create duplicate entries. There is no validation, no conflict detection, and no feedback about what changed.

## Proposed solution

Add CLI subcommands for managing permissions, e.g.:

```
claudewheel permission add ask "safegit push" --profile work
claudewheel permission add allow "Bash(grep:*)" --profile work
claudewheel permission remove ask "safegit push" --profile work
claudewheel permission list --profile work
```

Benefits:
- Validated writes — the tool can reject malformed permission strings
- Idempotent — adding an already-present entry is a no-op
- Atomic — no risk of half-written JSON from interrupted edits
- Auditable — could log what changed and when
