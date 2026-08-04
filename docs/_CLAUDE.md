---
title: CLAUDE.md
---
# claudewheel

A TUI launcher for Claude Code: pick a profile, model, directory, MCP mode, and permissions from a horizontal segment bar, then launch.

## Release workflow

This project uses [rlsbl](https://github.com/smm-h/rlsbl) for release orchestration.

- Update CHANGELOG.md with a `## X.Y.Z` entry describing changes
- Run `rlsbl release [patch|minor|major]` to bump version and create a GitHub Release
- CI handles `npm publish` automatically via OIDC Trusted Publishing (no tokens needed)
- First publish must be done locally: `npm login && npm publish --access public`
- After first publish, configure Trusted Publishing on npmjs.com (package settings)
- Never run `npm publish` manually after Trusted Publishing is configured
- Use `rlsbl release --dry-run` to preview a release without making changes

## Architecture

:-: list-modules path="claudewheel/"

## Commands

:-: table-commands

### Confirmation and preview

- Ordinary commands need no approval flag. `c launch`, `c deploy-hooks --all`, `c reconcile-permissions` and `c patch-profiles` are the bare, correct invocations from a script, hook or agent -- including the bare `claudewheel` that starts a session, which prompts for nothing.
- The CLI framework prompts only for commands that declare themselves `consequential`, and in claudewheel that is exactly `profile delete`: it removes the profile directory with its `.credentials.json` and `settings.json`, drops the `tokens.json` entry, and de-registers the profile, none of which can be walked back. It refuses with `error: stdin is not interactive; pass --approve-consequential to confirm` when there is no terminal, so a script that means to run it passes `--approve-consequential`.
- `--quiet`, `--verbose`, `--dry-run` and `--approve-consequential` are framework-owned: no short forms, recognized anywhere in the command line, and never valid as claudewheel's own flag names.
- `--dry-run` previews instead of writing: every subprocess launch, filesystem mutation and network call is recorded in a would-do log and nothing under `~/.claudewheel/` is touched. It also suppresses the confirmation, so a preview never has to be consented to.

## Config system

- Config files live in `~/.claudewheel/` (config.json, segments.json, options.json, state.json, themes/)
- On startup, `_migrate()` adds missing keys from DEFAULT_* without overwriting user values
- `_run_versioned_migrations()` applies one-time value fixes keyed by `_schema_version` in config.json
- New migrations go in the `_MIGRATIONS` list in config.py with an incremented version number

## Viewport scrolling

When the segment bar overflows the terminal width, the renderer activates a scrolling viewport:
- `_compute_bar_layout()` pre-computes logical column positions for all segments
- `_compute_viewport()` centers the focused segment with ARROW_MARGIN (4 chars) reserved on each side
- Segments outside the viewport are skipped; partially visible ones are clipped at the margins
- Edge arrows show off-screen segment counts; minimap shows colored squares in the top-right
- Config key `"minimap"` controls visibility: `"auto"` (only when scrolling) or `"always"`
- Theme section `"overflow"` controls arrow/minimap colors and the minimap character
