# claudewheel Changelog

## 0.1.11
- Viewport scrolling for narrow terminals: bar scrolls horizontally with focused segment centered
- Edge arrows (`<2`, `3>`) show off-screen segment count at viewport edges
- Minimap: colored block indicators in top-right corner showing all segments at a glance
- Configurable minimap visibility (`"minimap": "auto"` or `"always"` in config.json)
- Overflow theme colors: `arrow_fg`, `minimap_fg`, `minimap_focused_bg` in the `overflow` theme section
- Clip partially visible segments at viewport edges instead of rendering past margins
- Clip fan-out options at screen edges when scrolling
- Minimum width guard for degenerate terminals (< 9 columns)
- Config migration: missing default keys are merged into existing config/segments/theme files on startup
- Wide terminals see no visual change (viewport only activates when the bar overflows)

## 0.1.10
- Fix backspace in freeform segments trapping arrow keys (emptying the buffer now exits edit mode; LEFT/RIGHT work mid-edit)
- Make GitHub segment optional (launching without a GH profile is legitimate)
- Update session hook to use `rlsbl prs` instead of deleted script

## 0.1.9
- Add `--version` flag (prints app version)
- Add `-s`/`--set KEY=VALUE` flag for setting any segment value (e.g. `-s version=2.1.119`)
- `-s` is required for the `version` segment (its `--version` flag collides with the app version flag)
- `-s` takes precedence over individual `--<segment>` flags; validates segment name and format

## 0.1.8
- Add `print_mode` toggle to segment definitions; segments declare whether they participate in `-p` mode
- Exclude `github`, `mcp`, `permissions` segments from print mode by default (github is slow, mcp unreliable non-interactively, permissions can hang on interactive-only modes)
- Fix health check blocking `input()` in print mode; warnings now go to stderr without blocking
- Warn to stderr when print mode uses fallback defaults for missing required segments
- Add 7 unit tests for print mode

## 0.1.7
- Add `-p`/`--print` flag for non-interactive print mode
- Add `--` passthrough for raw Claude Code flags (e.g. `--output-format`, `--allowedTools`)
- Add `scripts/redir-history.sh` for rewriting paths in history.jsonl files
- Fix CI: add Python 3.14 setup and pytest install

## 0.1.6
- Default directory segment to current working directory
- Raise `/tmp/claude` health check threshold from 500 MB to 2 GB
- Add `npm test` script (runs pytest)

## 0.1.5
- Add `--redir OLD NEW` subcommand for redirecting session data after a project directory rename
- Fix `--redir` to find `.claude.json` project keys under `data["projects"]` (not top-level)
- Fix `--redir --dry-run` to report accurate JSONL file/line counts
- Rename Python package from `claude_launcher` to `claudewheel`
- Simplify README quick start, add `--redir` to CLI docs

## 0.1.4
- Rename all internal references from ClaudeLauncher to claudewheel (docstrings, CLI output, User-Agent, tests)
- Add branding assets (logo, banner) and banner to README

## 0.1.3
- Fix freeform backspace bug: can now delete to empty string without resetting
- Scaffold rlsbl scripts and hooks (check-prs.sh, pre-release.sh, record-gif.sh, pre-push hook)
- Add Claude Code SessionStart hook for PR awareness
- Merge security-sensitive patterns into .gitignore
- Update references to rlsbl

## 0.1.2
- Exclude `__pycache__` bytecode from npm tarball (package size: 75kB -> 26kB)

## 0.1.1
- Remove hardcoded personal data from defaults (profile names, GitHub usernames)
- Add runtime discovery for profile segment (`claude_config_scan` — scans `~/.claude-*` directories)
- Add runtime discovery for github segment (`gh_auth` — parses `gh auth status`)
- Expand directory scan parents to common project directories

## 0.1.0
- Initial npm release as `claudewheel`
- Node.js bin wrapper with Python 3.14+ version check
- CI and publish workflows scaffolded via rlsbl

## Architecture

- 16-module Python package (no external deps, stdlib only -- targets Python 3.14)
- Bash shim `c` as entry point, sets `PYTHONPATH` and runs `python3 -m claude_launcher`
- Tests via stdlib `unittest` (61+ tests, run in <10 ms)

## TUI

- Single-line segment bar at vertical-centre-left of the terminal
- Vertical fan-out shows other options above/below the selected value
- Per-segment focus background; per-option foreground colours from theme
- Fuzzy search with character-position highlighting (matched chars in `search_match_fg`)
- Search buffer turns red (`search_no_match_fg`) when zero matches
- "+" sentinel option for creatable segments (profile, github, model) lets users add new values inline
- Freeform editing on directory segment: backspace/typing on a selected value enters edit mode
- Cycle math includes -1 (blank) as a ring position; symmetric blank reachability for both `wrap=True` and `wrap=False`

## Discovery

- `npm_and_local` for versions: fetches `npm view @anthropic-ai/claude-code versions --json` (cached 1 h in `state.json`), merges with locally installed binaries
- `directory_scan`: lists subdirectories of configured parent dirs (`~/Projects`, `~/Work`)
- `state_field`: merges state-tracked recent values
- `directory_listing`: simple file listing (legacy, used by older configs)

## Cross-segment dependencies

- Options can declare `requires` constraints on other segments' values (e.g. `auto` permission previously required version >=2.1.110, since dropped)
- `evaluate_requires(bar)` recomputes per-segment `unavailable` sets on every keypress
- Renderer dims unavailable options with `unavailable_fg`

## Install mechanism

- Direct binary download from Anthropic's GCS bucket (the same mechanism Claude Code's own updater uses): `storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/{ver}/{platform}/claude`
- Fetches `manifest.json` first for SHA-256 checksum
- Streams ~234 MB binary with progress reporting
- Atomic write via `.downloading` -> rename
- Replaces the original npm-based approach which failed on user-prefix installs

## CLI

- Dynamic `--<segment_key>` flags generated from segment definitions
- TUI shown by default (pre-filled from `last_config` + arg overrides)
- TUI skipped only when args alone cover every required segment
- Passthrough flags `-c` / `--continue` and `-r` / `--resume [SESSION_ID]` for Claude Code's session restore
- One-shot flags: `--versions`, `--config`, `--health`, `--install VERSION`, `--uninstall VERSION`, `--reset-options`, `--show`

## Health checks

- tmpfs quota (warns >80%)
- `/tmp/claude-$UID/` size (warns >500 MB)
- Ghost files check removed -- was misidentifying memfd regions as leaks

## State persistence

- `last_config` (segment selections from last launch)
- `recent_dirs` (cap 20, dedup, recent-first)
- `launch_count`
- `npm_versions_cache` (1 h TTL)
- Atomic writes via tmp-file rename

## Hooks

- `~/.claudelauncher/hooks/pre-launch*` scripts run before `exec`
- Receive segment selections as `CL_*` env vars
- Nonzero exit aborts launch

## Notable upstream issue

- Filed and closed [#54026](https://github.com/anthropics/claude-code/issues/54026) as duplicate of [#53180](https://github.com/anthropics/claude-code/issues/53180): Claude Code 2.1.120 `--resume` crashes due to `RW4` hook returning fewer keys than the destructure call expects. Workaround: use 2.1.116 or 2.1.119 until upstream ships a fix.

## Open future work

- Session-picker-as-segment (filed in `todo/session-segment.md`)
