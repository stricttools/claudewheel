<p align="center">
  <img src="assets/banner.png" alt="claudewheel" width="700">
</p>

TUI launcher for Claude Code with profile, GitHub account, version, model, directory, MCP, and permission switching.

## Quick start

```bash
./c            # launch the TUI
./c --help     # show all flags
```

The first run creates `~/.claudelauncher/` populated with defaults (config, segments, options, themes).

Requirements: Python 3.14+ on the `PATH`. No third-party packages.

## The segment bar

The TUI is a single horizontal "segment bar" rendered at the vertical centre of the terminal. Each segment is a labelled cell whose value can be cycled, searched, or freely edited. Above and below the focused segment, a vertical "fan-out" shows the other available options dimmed in the segment's accent colour. Pressing Enter on any segment launches Claude Code with the current selections.

Keys:

- Left / Right -- move focus between segments
- Up / Down -- cycle the focused segment's value (blank state `---` is part of the ring)
- Type characters -- start fuzzy search (on `searchable` segments) or freeform edit (on `freeform` segments)
- Tab -- accept the current fuzzy match and advance to the next segment
- Backspace -- delete a search/edit character (on a non-empty selected value, starts edit mode)
- Esc -- cancel the in-progress search or edit
- Enter -- launch
- q or Ctrl-C -- quit without launching

Search shows the matched characters in the search-match colour. The search buffer turns red when no option matches.

## Segment types

| Key           | Label   | Controls                                                                       |
|---------------|---------|--------------------------------------------------------------------------------|
| `profile`     | Profile | Maps to `CLAUDE_CONFIG_DIR` (e.g. `~/.claude-personal`)                        |
| `github`      | GH      | Selects the GitHub account; `gh auth token --user <acct>` exported as `GH_TOKEN` |
| `version`     | Ver     | Picks the Claude Code binary in `~/.local/share/claude/versions/`              |
| `model`       | Model   | Sets the model ID (`ANTHROPIC_MODEL`); `[1m]` suffix enables 1M-context        |
| `directory`   | Dir     | Working directory to `cd` into before launch                                   |
| `mcp`         | MCP     | MCP profile mode (`default`, `strict`)                                         |
| `permissions` | Perms   | Permission mode passed to Claude Code (`bypass`, `default`, `plan`, `auto`)    |

Profile, GitHub, and Model are *creatable*: their option lists end with a `+` sentinel that prompts for a new value and persists it to `options.json`. Directory is *freeform*: you can type any path. Version pulls a live npm listing merged with the locally installed binaries.

## CLI flags

### One-shot commands

```bash
c --versions               # list installed versions, mark which one `claude` points to
c --install 2.1.119        # download and install a Claude Code binary from GCS
c --uninstall 2.1.104      # remove an installed binary (refuses if it is the current symlink target)
c --reset-options          # delete options.json so defaults regenerate next run
c --show                   # print last_config, theme, default flags, recent dirs
c --config                 # open ~/.claudelauncher/ in $EDITOR
c --health                 # run pre-launch health checks and exit
c --new-profile            # interactive wizard to create a new Claude Code profile
c --migrate SRC DST        # migrate session artifacts between profiles
c --migrate --dry-run S D  # preview migration without changes
c --redir OLD NEW          # redirect session data after a project directory rename
c --redir --dry-run O N    # preview redirect without changes
```

### Segment overrides

Every enabled segment gets its own `--<key>` flag. These pre-fill the TUI:

```bash
c --profile work --github mhxv
c --directory ~/Projects/foo --model claude-opus-4-7
```

If the override set covers every *required* segment, the TUI is skipped entirely and Claude Code launches directly.

### Session passthrough

Mutually exclusive flags forwarded to Claude Code:

```bash
c -c                       # --continue: resume the most recent session
c -r                       # --resume: open Claude Code's session picker
c -r 0123abcd              # --resume <id>: jump to a specific session
c -p "summarize this repo" # --print: non-interactive print mode
```

These compose with segment overrides: `c --profile personal -r` opens the picker against the personal profile.

Print mode (`-p`) skips the TUI and launches Claude Code non-interactively. Extra flags after `--` are passed through:

```bash
c -p "explain auth.py" -- --output-format json --allowedTools "Read,Bash"
```

## Config directory

`~/.claudelauncher/` layout:

| Path             | Purpose                                                     | Auto-written?     |
|------------------|-------------------------------------------------------------|-------------------|
| `config.json`    | Theme, enabled segments, default flags, health-check switch | No (user-edited)  |
| `segments.json`  | Segment definitions (label, width, wrap, searchable, etc.) | No                |
| `options.json`   | Values, metadata, and discovery configs per segment         | Only via `+` UX   |
| `state.json`     | `last_config`, `recent_dirs`, `launch_count`, npm cache    | Yes, every launch |
| `themes/*.json`  | Colour schemes (`dark.json`, `light.json` ship by default)  | No                |
| `hooks/*`        | Executable scripts -- see below                             | No                |

Defaults are regenerated on first run if any file is missing.

## Hooks

Drop an executable script into `~/.claudelauncher/hooks/` whose name starts with `pre-launch` (e.g. `pre-launch-token-refresh`). It runs immediately before `exec`, with the chosen segment values exported as `CL_<KEY>` environment variables:

```bash
#!/usr/bin/env bash
# ~/.claudelauncher/hooks/pre-launch-warn-work
if [[ "$CL_PROFILE" == "work" && "$CL_DIRECTORY" == "$HOME/Projects/personal-thing" ]]; then
    echo "Refusing to use the work profile on a personal project." >&2
    exit 1
fi
```

A nonzero exit aborts the launch (and prevents `launch_count` from being incremented). Hooks have a 10-second timeout.

## Adding new options

- **Profile / GitHub / Model**: cycle the segment to its `+` sentinel, press Enter, type the new value. It is appended to `options.json` under the segment's `values` list and selected.
- **Direct edit**: open `~/.claudelauncher/options.json` and add to the relevant segment's `values` array. For profiles you also need a `metadata.<name>.config_dir` entry.
- **Install a Claude Code version**: run `c --install <version>` or pick a not-yet-installed version in the TUI and confirm the install prompt. Binaries land in `~/.local/share/claude/versions/<version>`.

## Themes

Two themes ship with the launcher: `dark.json` and `light.json` in `~/.claudelauncher/themes/`. Switch by setting `theme` in `config.json` to the file's basename. Themes define per-segment foreground / focus / option / unavailable colours and the search highlight palette. Add a new theme by writing another `themes/<name>.json` and pointing `config.json` at it.

## Tests

```bash
cd /home/m/Projects/claudewheel
python3 -m unittest discover tests/
```

Sixty-plus stdlib `unittest` tests covering segment cycling, fuzzy matching, requires-evaluation, install/manifest parsing, and discovery merge logic. Runs in well under 100 ms.
