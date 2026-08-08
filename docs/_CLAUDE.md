---
title: CLAUDE.md
---
# claudewheel

A TUI launcher for Claude Code: pick a profile, model, directory, MCP mode, and permissions from a horizontal segment bar, then launch.

## Release workflow

This project uses [rlsbl](https://github.com/smm-h/rlsbl) for release orchestration.

- Cover every commit with an entry in `.rlsbl/changes/unreleased.jsonl`, added via `rlsbl changelog add`
- `CHANGELOG.md` is generated from those entries -- never edit it by hand
- Scaffold `.rlsbl/releases/unreleased.toml` with `rlsbl release init`, then set the bump type and description in it
- Release with `rlsbl release run --no-allow-dirty --watch --approve-consequential`
- CI publishes to both npm and PyPI via OIDC Trusted Publishing (no tokens needed); never run `npm publish` or `uv publish` by hand
- Use the framework-owned `--dry-run` flag to preview a release without making changes

## Architecture

:-: list-modules path="claudewheel/"

## Commands

:-: table-commands

### Confirmation and preview

- Ordinary commands need no approval flag. `c launch`, `c deploy-hooks --all`, `c stats` and `c permission add` are the bare, correct invocations from a script, hook or agent -- including the bare `claudewheel` that starts a session, which prompts for nothing.
- The CLI framework prompts only for commands that declare themselves `consequential`, and in claudewheel that is exactly three: `profile delete`, `reconcile-permissions` and `patch-profiles`. Each refuses with `error: stdin is not interactive; pass --approve-consequential to confirm` when there is no terminal, so a script that means to run one passes `--approve-consequential`.
- `profile delete` is there because it removes the profile directory with its `.credentials.json` and `settings.json`, drops the `tokens.json` entry, and de-registers the profile, none of which can be walked back. `reconcile-permissions` and `patch-profiles` -- two names for one operation -- are there because the reconciliation is EXACT: a single bare run rewrites every managed profile plus `shared-settings.json`, pruning hand-authored permission rules, hook entries and `disallowedTools` drift, with nothing backed up and nothing that reconstructs a pruned entry. Run them with `--dry-run` first; the per-target diff is the informative preview a blind `Proceed?` is not.
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
