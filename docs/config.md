---
title: Configuration
description: "How the claudewheel configuration system works: file layout, segments and options, the migration framework, schema versioning, and non-interactive overrides."
nav_group: "Concepts"
order: 4
---

# Configuration

claudewheel's configuration lives in four JSON files under `~/.claudewheel/`.
On first run, each file is created from built-in defaults. On subsequent runs, a
migration system adds new keys and applies one-time schema fixes without
overwriting existing user values.

## File layout

All configuration lives under `~/.claudewheel/` (overridable via the
`CLAUDEWHEEL_CONFIG_DIR` environment variable).

| File | Purpose |
| --- | --- |
| `config.json` | Global settings: enabled segments, theme, default flags, default client, minimap mode, health check toggle, and the internal `_schema_version` counter |
| `segments.json` | Segment definitions: one entry per segment with its key, label, layout constraints (min/max width, wrap, searchable, creatable, freeform), and behavior flags (required, tab_advances, show_options) |
| `options.json` | Per-segment option data: static values, pinned values, discovery configuration, and segment metadata |
| `state.json` | Runtime state: last-selected values (`last_config`), recent directories, launch count, auth browser preference, per-project hook approvals, and the npm version cache |
| `themes/dark.json` | Dark theme color definitions |
| `themes/light.json` | Light theme color definitions |
| `shared-settings.json` | Canonical shared settings applied to all profiles: hooks, disallowedTools, and profileDefaults (permissions deny/ask arrays) |
| `tokens.json` | Centralized OAuth tokens keyed by profile name |

Additionally, `profiles/<name>/` directories hold per-profile settings and
credentials, and `shared/` holds session data symlinked from each profile.

## config.json

The main configuration file controls global behavior.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `theme` | string | `"auto"` | Theme selection: `"dark"`, `"light"`, or `"auto"` (detects terminal background) |
| `enabled_segments` | array | `["profile", "github", "version", "model", "directory", "mcp", "permissions"]` | Which segments appear in the TUI bar, in order |
| `default_flags` | array | `["--dangerously-skip-permissions"]` | Flags passed to every Claude Code launch |
| `health_check_on_launch` | bool | `true` | Run diagnostic health checks before each launch |
| `minimap` | string | `"auto"` | Minimap visibility: `"auto"` (only when scrolling) or `"always"` |
| `default_client` | string | `"claude"` | Pre-selected client for the interactive launcher and fallback for non-interactive launches |
| `_schema_version` | int | `0` | Internal migration counter (do not edit manually) |

Remove a segment key from `enabled_segments` to hide it from the bar entirely.

## Segments and options

The TUI renders a horizontal bar of **segments**. Each segment has a key (e.g.
`profile`, `model`, `directory`), a display label, and a list of selectable
options. The option list is assembled from multiple sources with a configurable
merge order.

### Option collections

Each segment maintains four option collections:

- **pinned** -- values explicitly added by the user (via the wizard, the `+`
  creation UI, or manual edits to `options.json`). Pinned values survive across
  restarts and are never overwritten by discovery.
- **discovered** -- values found at runtime by the segment's discovery function
  (scanning directories, querying npm, enumerating profiles, etc.).
- **defaults** -- static fallback values from the built-in `DEFAULT_OPTIONS` in
  `defaults.py`. These are the baseline options that ship with claudewheel.
- **ephemeral** -- values added during the current session only (e.g. a
  freeform-typed directory path). Not persisted.

The final option list is built by concatenating collections in a configurable
order (the **collection order**), deduplicating (first occurrence wins), and
optionally sorting. Different segments use different merge strategies:

| Segment | Collection order | Sort |
| --- | --- | --- |
| `version` | pinned, discovered, defaults | semver descending |
| `profile` | pinned, discovered | -- |
| `model` | pinned, defaults | -- |
| `mcp` | pinned, defaults | -- |
| `permissions` | pinned, defaults | -- |
| `github` | pinned, discovered | -- |
| `directory` | pinned, discovered, defaults | -- |

### Discovery types

Segments with dynamic content use a discovery function registered in the
`DISCOVERY_REGISTRY`. Discovery runs at startup; slow discoveries (those
that make network calls) run in a background thread and merge results into the
bar when ready.

| Discovery type | Segments | What it does | Slow? |
| --- | --- | --- | --- |
| `claude_config_scan` | profile | Enumerates profiles from the ProfileStore | no |
| `npm_and_local` | version | Fetches recent versions from npm, merges with locally installed binaries | yes |
| `directory_scan` | directory | Scans parent directories (`~/Projects`, `~/repos`, etc.) and validates recent dirs from state | no |
| `gh_auth` | github | Queries `gh auth status` for logged-in GitHub accounts | yes |
| `state_field` | -- | Merges state-tracked values with static defaults | no |

Slow discovery results are cached. The npm version cache has a 1-hour TTL
stored in `state.json` under `npm_versions_cache`.

### Staleness verification

When a slow discovery completes and returns new results, previously discovered
values that disappeared from the new list are not automatically dropped.
Instead, if the discovery type has a `verify` callback, each removed value is
checked (e.g. "does the binary still exist on disk?"). Values that pass
verification are kept; values that fail are dropped.

### Cross-segment constraints

Options can declare `requires` constraints that reference other segments. For
example, a model option might require a minimum Claude Code version:

```json
{
  "value": "claude-opus-5",
  "requires": {"version": ">=2.1.219"}
}
```

The `evaluate_requires` function runs every render cycle, computing the
`unavailable` set for each segment. Unavailable options are dimmed in the UI
and cannot be selected.

### options.json structure

Each segment key maps to an object with:

```json
{
  "profile": {
    "values": [],
    "pinned": ["work", "personal"],
    "discovery": {
      "type": "claude_config_scan",
      "base_dir": "~"
    }
  },
  "model": {
    "values": ["claude-opus-5", "claude-opus-4-8", "..."],
    "pinned": []
  },
  "directory": {
    "values": [],
    "pinned": [],
    "discovery": {
      "type": "directory_scan",
      "parents": ["~/Projects", "~/repos", "~/src"],
      "state_field": "recent_dirs"
    }
  }
}
```

- `values` -- legacy list, now largely superseded by the pinned/discovered/defaults
  split. Kept for backward compatibility; migration 3 classifies existing values
  into the appropriate collection.
- `pinned` -- user-added values that persist across restarts.
- `discovery` -- configuration for the segment's discovery function (type plus
  type-specific parameters like `path`, `parents`, `count`, `state_field`).
- `metadata` -- per-value metadata dict (e.g. auth status for profiles).

## segments.json

Each entry in the segments array defines a segment's visual and behavioral
properties:

| Property | Type | Default | Description |
| --- | --- | --- | --- |
| `key` | string | -- | Unique identifier, matches the options.json key |
| `label` | string | -- | Display label in the TUI bar |
| `show_options` | bool | `true` | Whether to render the fan-out option list below this segment |
| `wrap` | bool | `true` | Whether cycling past the last option wraps to blank |
| `min_width` | int | `6` | Minimum character width for the segment |
| `max_width` | int | `20` | Maximum character width for the segment |
| `required` | bool | `false` | Whether a value must be selected before launching |
| `searchable` | bool | `false` | Whether typing filters options via fuzzy matching |
| `tab_advances` | bool | `true` | Whether Tab moves focus to the next segment |
| `creatable` | bool | `false` | Whether the segment shows a `+` option for inline creation |
| `freeform` | bool | `false` | Whether typed text can be submitted as a new value directly |

## state.json

Runtime state persisted between sessions:

| Key | Description |
| --- | --- |
| `last_config` | Dict of segment key to last-selected value. Restored on next launch to pre-select previous choices. |
| `recent_dirs` | List of recently used directories (capped at 20, most recent first). Used as hints by directory discovery. |
| `launch_count` | Total number of successful launches. |
| `npm_versions_cache` | Cached npm version list with a `fetched_at` timestamp for TTL. |
| `auth_browser` | Browser path chosen in the auth wizard (written out-of-band). |
| `project_hook_approvals` | Per-project hook approval decisions, keyed by canonical project path. |
| `vanilla_guardrails_opt_in` | Machine-global opt-in state for vanilla profile guardrails. |
| `scratchpad_snooze_until` | ISO-8601 deadline for snoozing the scratchpad cleanup prompt. |

Some state keys are "out-of-band": they are written directly to disk by
subsystems (the auth wizard, preflight steps) while the TUI holds its own
in-memory copy. When the TUI saves state, it re-reads these keys from disk
and lets the disk values win, preventing stale in-memory state from clobbering
concurrent writes.

## Schema migration

claudewheel uses two migration mechanisms that run at startup.

### Key-additive migration (`_migrate`)

The first pass adds missing keys to existing config files without overwriting
user values. It runs every startup and is idempotent:

- **config.json** -- missing top-level keys are added from `DEFAULT_CONFIG`.
- **segments.json** -- for each segment matched by `key`, missing attributes
  are added from `DEFAULT_SEGMENTS`.
- **Theme files** -- missing keys are deep-merged from the default theme dicts.
- **options.json** -- new model values from `DEFAULT_OPTIONS` are appended to
  the user's model list.

Files are written only when something actually changed.

### Versioned migrations (`_run_versioned_migrations`)

The second pass runs one-time schema fixes keyed by `_schema_version` in
`config.json`. Each migration has a version number and runs exactly once (when
the config's `_schema_version` is less than the migration's version). After all
applicable migrations run, `_schema_version` is bumped to the highest applied
version.

Current migrations:

| Version | Description |
| --- | --- |
| 1 | Make the `github` segment optional (was incorrectly `required: true`) |
| 2 | Rewrite profile metadata paths from `~/.claude-<name>` to `~/.claudewheel/profiles/<name>` |
| 3 | Classify existing option `values` into `pinned` vs defaults (the pinned/discovered/defaults split) |
| 4 | Drop the legacy `metadata` block from the `profile` segment (locations are now derived from the profile directory) |

### Adding a new migration

1. Write a function with the signature
   `(config, segments_def, theme, options_def) -> None` that mutates in place.
2. Append an entry to the `_MIGRATIONS` list in `config.py` with the next
   version number.
3. The migration runs against all theme files uniformly (not just the
   terminal-resolved one), so schema fixes are deterministic regardless of
   which theme the user renders.

## Non-interactive overrides (the `--set` flag)

The `launch` command supports setting segment values from the command line,
bypassing the TUI entirely when all required segments are covered.

### Per-segment flags

Each segment has a dedicated flag:

```
c --profile work --model claude-opus-4-8 --directory ~/Projects/myapp
```

### The `-s` / `--set` flag

The generic `--set` (short: `-s`) flag sets any segment as `KEY=VALUE`:

```
c -s profile=work -s model=claude-opus-4-8
```

It is repeatable but each segment can only be set once. Setting the same
segment via both a dedicated flag and `-s` is an error:

```
c --profile work -s profile=personal   # error: duplicate
```

### TUI bypass

When all required segments have values (from flags, `-s`, or defaults), the TUI
is skipped entirely and the launch proceeds non-interactively. The `directory`
segment defaults to the current working directory when not explicitly set.

Values from `last_config` in `state.json` fill in any segments not covered by
flags, so a user who always uses the same profile and model can launch with no
flags at all after the first interactive session.

### Interaction with `--print-prompt`

The `--print-prompt` / `-p` flag activates non-interactive print mode. In this
mode, only segments marked `print_mode: true` in segments.json are used.
Missing required print-mode segments trigger a warning but do not block the
launch.

## Resetting configuration

The `reset-options` command deletes `options.json` so it regenerates from
defaults on the next run:

```
c reset-options
```

The `config` command opens `~/.claudewheel/` in your `$EDITOR` for manual
editing:

```
c config
```
