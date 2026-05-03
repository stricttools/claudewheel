# Claude Launcher Core

## Context

Managing multiple Claude Code profiles, GitHub accounts, versions, and flags via shell aliases (`c`, `c3`, `c3p`, `cw`, `cwp`, `c_with_mcp`) is unsustainable. The aliases are opaque, the GH token is evaluated at shell init (stale by mid-session), version pinning is impossible because CC auto-updates the symlink, and there's no visibility into usage or system health before launching.

This tool replaces all `c*` aliases with a single interactive TUI launcher.

## Design

### Single command: `c`

- `c` -- open the TUI, pick options, launch
- `c <preset>` -- launch a named preset instantly (e.g. `c pe`, `c work`)
- `c --last` -- relaunch with last-used config, no TUI
- `c --pick` -- always show TUI

### TUI layout

```
Claude Launcher                          Usage: 47% weekly (resets Apr 24)

Profile    [personal]  the-third  work          GH: J4+ao  pp0M
Version    [2.1.117]   2.1.104    2.1.116       Pin: auto
Directory  ~/Projects/ProductEngine              [recent dirs...]
MCP        [strict]    allow-all
Perms      [bypass]    normal

[Launch]   [Config]   [Cleanup /tmp]   [Health]
```

Arrow keys to navigate between option groups. Enter to toggle/select. `l` or Enter on Launch to go.

### Config file: `~/.claude-launcher.jsonc`

```jsonc
{
  // Profiles map to CLAUDE_CONFIG_DIR
  "profiles": {
    "personal": { "config_dir": "~/.claude-personal" },
    "the-third": { "config_dir": "~/.claude-the-third" },
    "work": { "config_dir": "~/.claude-work" }
  },

  // GitHub accounts -- token fetched LIVE via `gh auth token --user` at launch
  "github_accounts": ["]=L(;", "jov""],

  // Default flags applied to every launch
  "default_flags": ["--allow-dangerously-skip-permissions"],

  // MCP presets
  "mcp_modes": {
    "strict": "--strict-mcp-config",
    "allow-all": ""
  },

  // Recent directories (auto-populated on each launch)
  "recent_dirs": [],
  "max_recent_dirs": 20,

  // Named presets for instant launch
  "presets": {
    "pe": {
      "profile": "personal",
      "github": ""|1D&",
      "dir": "~/Projects/ProductEngine",
      "mcp": "strict"
    },
    "work": {
      "profile": "work",
      "github": "@._F",
      "dir": "~/Work/super",
      "mcp": "strict"
    }
  },

  // Last-used config (auto-saved)
  "last_config": {},

  // Version management
  "auto_discover_versions": true,
  "versions_dir": "~/.local/share/claude/versions",
  "pinned_versions": {},

  // Health check before launch
  "health_check_on_launch": true,
  "tmp_warn_mb": 500,

  // Usage display
  "show_usage": true
}
```

### What happens on launch

1. Read `~/.claude-launcher.jsonc`
2. If preset name given (`c pe`), load that preset's config
3. If `--last`, load `last_config` from the config file
4. Otherwise, show TUI and let user pick
5. Run pre-launch health check (if enabled):
   - Check tmpfs quota usage
   - Check `/tmp/claude-$UID/` size
   - Check for ghost files via `lsof +L1`
   - If unhealthy, show warning with fix options (clean /tmp, kill offending PIDs)
6. Fetch GH token live: `gh auth token --user <selected_account>`
7. Build the command:
   ```
   GH_TOKEN=<token> CLAUDE_CONFIG_DIR=<profile_config_dir> <version_binary> <flags>
   ```
8. `cd` to selected directory
9. Save current config as `last_config` in the config file
10. Update `recent_dirs`
11. `exec` the built command (launcher process is replaced by CC)

### Version management

- Auto-discover installed versions by listing `~/.local/share/claude/versions/`
- Show which version the symlink currently points to (marked as "auto")
- When user picks a specific version, exec that binary path DIRECTLY -- not through the symlink -- so CC's auto-update revert has no effect on the running process
- TUI shows version + model info if parseable (e.g. "2.1.117 (Opus 4.7)" vs "2.1.104 (Opus 4.6)")
- Option to delete old versions to free disk space

### Usage stats

Parse CC's cached data from the profile's `.claude.json`:
- `lastCost`, `lastTotalInputTokens`, `lastTotalOutputTokens`
- `lastModelUsage` breakdown per model
- Weekly limit info (if available from CC's API response cache)

Display as a one-line summary in the TUI header.

### Health check integration

Quick pre-launch checks (reuses logic from the planned HealthMonitor project):
- tmpfs quota: warn if >80%
- `/tmp/claude-$UID/` size: warn if >threshold
- Ghost files: warn if any >200MB
- If critical, show fix options before launching

## Architecture

Single Python file: `claude-launcher.py`. Installed to `~/.local/bin/c`.

```
claude-launcher.py
  |
  |-- config.py logic (inline)
  |   |-- load_config()        -- read JSONC, expand ~ paths
  |   |-- save_config()        -- write back (last_config, recent_dirs)
  |
  |-- tui.py logic (inline)
  |   |-- render_dashboard()   -- draw the TUI layout
  |   |-- handle_input()       -- keyboard navigation
  |   |-- option_groups[]      -- profile, version, directory, mcp, perms
  |
  |-- discovery.py logic (inline)
  |   |-- discover_versions()  -- list ~/.local/share/claude/versions/
  |   |-- get_symlink_target() -- readlink ~/.local/bin/claude
  |   |-- get_usage_stats()    -- parse .claude.json for usage data
  |
  |-- health.py logic (inline)
  |   |-- quick_health_check() -- tmpfs quota, ghost files, /tmp size
  |   |-- show_health_warning() -- interactive fix options
  |
  |-- launch.py logic (inline)
  |   |-- fetch_gh_token()     -- subprocess: gh auth token --user
  |   |-- build_command()      -- assemble env + binary + flags
  |   |-- launch()             -- cd + exec
```

All in one file. No external dependencies beyond Python stdlib (`curses`, `json`, `subprocess`, `os`, `pathlib`).

## CLI interface

```bash
# Interactive TUI
c

# Launch preset instantly
c pe
c work

# Relaunch last config
c --last

# Force TUI even if last config exists
c --pick

# Just show health check, don't launch
c --health

# Just show usage stats
c --usage

# Edit config in $EDITOR
c --config

# List available versions
c --versions
```

## Open questions

- **TUI framework**: `curses` (stdlib, ugly but no deps) vs `textual` (beautiful but pip dependency). A middle ground: raw ANSI escape sequences for a simple grid layout without curses. Fast startup, no deps, looks decent.
- **JSONC parsing**: Python stdlib doesn't parse JSONC (JSON with comments). Options: strip comments with regex before `json.loads()`, or use plain JSON. Stripping comments is ~5 lines of code.
- **Config file location**: `~/.claude-launcher.jsonc` (flat in home) vs `~/.config/claude-launcher/config.jsonc` (XDG-compliant). The former is simpler for a personal tool.
- **Relationship to HealthMonitor**: The health check here is a lightweight subset. Could import from HealthMonitor if it exists, or inline a minimal version. Keep them independent for now; unify later if both mature.
- **Preset auto-detection**: Should the launcher detect which project you're in (by cwd or git remote) and suggest a preset? e.g. if you're in `~/Projects/ProductEngine`, auto-highlight the `pe` preset.

## Files

- `claude-launcher.py` -- the tool (single file)
- `~/.claude-launcher.jsonc` -- user config (created on first run with defaults)
- `~/.local/bin/c` -- symlink to `claude-launcher.py`

## Relative effort

- Config loading + JSONC stripping: Low
- Version discovery + symlink reading: Low
- GH token fetching: Low
- Health check (minimal): Low-Medium
- Usage stats parsing: Low
- TUI rendering (ANSI-based, no curses): Medium
- Keyboard input handling: Medium
- Preset system: Low
- Overall: Medium -- single-session project
