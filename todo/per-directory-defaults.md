# Per-directory default config

## Problem

Every time you launch claudewheel from a project directory, you have to manually select the same profile, model, permissions, etc. There's no way to say "when I'm in this repo, default to these settings."

## Proposal

A local config file (e.g., `.claudewheel/defaults.json` or `.claudewheel.json`) in a project directory that pre-fills segment values when `cwd` matches.

## Open design questions

- **File name and format** -- `.claudewheel/defaults.json`, `.claudewheel.json`, `.claudewheel.toml`, or something in an existing config file?
- **Scope** -- should it walk up parent directories (like `.gitignore` does), or only check `cwd` exactly?
- **What it controls** -- just segment defaults, or also things like enabled/disabled segments?
- **Precedence** -- how does it interact with `last_config` from state.json and CLI args? Options: per-directory defaults < last_config < CLI args, or per-directory overrides last_config?
- **Gitignore-ability** -- should this file be committed to repos (shared team defaults) or kept local?

## Affected files

- `claudewheel/segment.py` -- `build_segment_bar()` would need to load and apply per-directory defaults
- `claudewheel/config.py` -- possibly a new loader for the local config
- `claudewheel/constants.py` -- new path constant(s)
- `claudewheel/defaults.py` -- default schema for the local config

## Effort

Small-medium. Core logic is straightforward (read file, merge into defaults), but the design questions above need answering first.
