+++
title = "README.md"
+++
<p align="center">
  <img src="assets/banner.png" alt="claudewheel" width="700">
</p>

A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc.

It is for developers who keep several Claude Code profiles on one machine -- separate accounts, plans, or permission setups -- and want to choose between them without editing config files or exporting environment variables by hand. Each profile is its own `CLAUDE_CONFIG_DIR` holding its own OAuth token, settings and permission rules, and `claudewheel patch-profiles` reconciles every managed profile back to one canonical guardrail model.

## Installation

```bash
go install github.com/stricttools/claudewheel/cmd/claudewheel@v0
```

Each GitHub Release also carries `claudewheel` archives for Linux and macOS (amd64 and arm64).

### Upgrading from the Python package

Versions up to 0.33.0 were a Python package, published to PyPI and npm; those releases stay there, and nothing newer is published to either. To move a workspace the Python wrote to the Go binary, install the Go binary as above, then:

```bash
uv tool uninstall claudewheel             # or pipx uninstall claudewheel; npm uninstall -g claudewheel for the old Node shim
claudewheel upgrade-workspace --dry-run   # preview the conversion
claudewheel upgrade-workspace             # remove the retired keys, add the missing ones
claudewheel deploy-hooks claudewheel-probe-runner.service --force-overwrite
```

Every other command refuses a workspace that still needs converting and names `upgrade-workspace`. The last line points the probe runner's user service at the Go binary (`claudewheel probe run-service`) and restarts it.

## Quick start

```bash
claudewheel         # launch the TUI
claudewheel --help  # show all flags
```

The first run creates `~/.claudewheel/` populated with defaults (config, segments, options, themes).

## The segment bar

The TUI is a single horizontal "segment bar" rendered at the vertical centre of the terminal. Each segment is a labelled cell whose value can be cycled, searched, or freely edited. Above and below the focused segment, a vertical "fan-out" shows the other available options dimmed in the segment's accent colour. Pressing Enter on any segment launches Claude Code with the current selections.

Keys:

- Left / Right -- move focus between segments (also exits freeform edit mode)
- Up / Down -- cycle the focused segment's value (blank state `---` is part of the ring)
- Type characters -- start fuzzy search (on `searchable` segments) or freeform edit (on `freeform` segments)
- Tab -- accept the current fuzzy match and advance to the next segment
- Backspace -- delete a search/edit character (on a non-empty selected value, starts edit mode)
- Esc -- cancel the in-progress search or edit
- `S` (uppercase) -- open the machine-wide sessions overview (see below)
- Enter -- launch
- q or Ctrl-C -- quit without launching

Search shows the matched characters in the search-match colour. The search buffer turns red when no option matches.

An uppercase `S` typed with nothing in the search buffer does not seed a fuzzy search -- it opens the sessions overview. Lowercase `s` still searches, and once a search is in progress `S` is an ordinary character again.

## The sessions overview

Press uppercase `S` from anywhere on the bar to see **every Claude Code session on this machine** as a framed table -- one row per session, gathered across every profile claudewheel discovers (the vanilla `default` profile included) plus every session recorded in the lifecycle store under `~/.claudewheel/shared/lifecycle/`. Nothing on the bar decides what it shows. The columns are the session's name, its state, its kind, its working directory, the Claude Code version, the model, how long ago it started, its resident memory in MiB, and how many of its probe reports (reports of OOM kills, see the probes guide) are not yet confirmed delivered; the session you are sitting in is marked with a `*`.

The state is the registry's own status wherever a process is still running (`working`, `idle`, `shell`, `waiting`, or `unverified` when the process identity could not be checked) and what the lifecycle store recorded otherwise: `starting`, `crashed`, `exited`, or the mark you gave it (`on-hold`, `blocked`, `done`). A running process always beats a recorded mark. Finished sessions -- `done` and `exited` -- are hidden until you ask for them.

It is a snapshot, not a live monitor: both stores are read when the screen opens and again only when you ask, so nothing renumbers under the cursor.

- Up / Down, Page Up / Page Down, Home / End -- move the focus (clamped, never wrapping)
- Left / Right -- scroll the columns sideways, for a terminal narrower than the table
- Enter -- expand the focused row into its session id, pid, profile, config directory and transcript path
- `a` -- show the finished sessions too, and hide them again
- `m`, then `h` / `b` / `d` / `c` -- mark the focused session on hold, blocked or done, or clear its mark
- `p` -- prune: delete the registry files of the sessions that crashed. Liveness and file identity are both re-checked at that moment, so a session that started while the screen was open keeps its file
- `r` -- read both stores again
- `q` or Esc -- close and return to the segment bar

Opening the screen also writes two things into the lifecycle store, both idempotent: an end for every session that died without recording one, and the display name of each live session, which exists nowhere else once its process is gone.

## OOM kill reports and probes

When the kernel's OOM killer kills a command a session started -- a `heavy` job over its `--mem` cap, or a Bash command when the session's Bash commands reach the memory cap they share -- claudewheel tells that session, even when it sits idle: the report wakes its main conversation, a Bash call that died with status 137 is labeled for the conversation that made it, and a session that ended gets the report when it resumes. A probe watches another session's kills, or every session's, until a deadline it must state:

```bash
claudewheel probe create oom-kill --all-sessions --deadline 2h
claudewheel probe list
```

The kills are read from the user journal by one user service, `claudewheel-probe-runner.service`, which `claudewheel deploy-hooks --all` installs, enables, and starts (`systemctl --user stop claudewheel-probe-runner.service` stops it). Nothing is dropped: `probe list` shows every undelivered or expired report and every kill no session took. See the probes guide in the documentation for the details.

## Client selection

Before the segment bar, the interactive launcher shows a **Client** step: choose which client to launch --- `claude` (the official Claude Code CLI) or `miniclaude` (the miniclaude REPL). The cursor starts on the `default_client` configured in `config.json` (default: `claude`). A client whose binary is not installed is shown with a `(not installed)` suffix rather than hidden; selecting it still launches and fails with a clear message.

Pass `--client <name>` to skip the step and choose explicitly; non-interactive launches (e.g. `--print-prompt`) use `default_client` without prompting. When the selected client is not `claude`, the version step is skipped --- the version selects a claudewheel-managed *claude* binary, which does not apply to other clients.

## Narrow terminals

When the segment bar is wider than the terminal, the renderer switches to a scrolling viewport:

- The focused segment is centered horizontally
- Edge arrows (`<2`, `3>`) show how many segments are off-screen in each direction
- A minimap in the top-right corner shows all segments as small colored squares; the focused one has an opaque background highlight
- Partially visible segments at the viewport edges are clipped rather than wrapped

The viewport activates automatically and deactivates when the terminal is resized wider. All rendering is identical to the non-scrolling case when the bar fits.

## Segment types

| Key           | Label   | Controls                                                                       |
|---------------|---------|--------------------------------------------------------------------------------|
| `profile`     | Profile | Maps to `CLAUDE_CONFIG_DIR` (e.g. `~/.claude-personal`)                        |
| `github`      | GH      | Selects the GitHub account; `gh auth token --user <acct>` exported as `GH_TOKEN` |
| `version`     | Ver     | Picks the Claude Code binary in `~/.local/share/claude/versions/`              |
| `model`       | Model   | Passes the model id as `--model`; an Opus/Sonnet `[1m]` suffix selects 1M-context |
| `directory`   | Dir     | Working directory to `cd` into before launch                                   |
| `mcp`         | MCP     | MCP profile mode (`default`, `strict`)                                         |
| `permissions` | Perms   | Permission mode passed to Claude Code. Offered: `bypass`, `default`, `auto`. `plan` is never offered, pinned, or accepted from `--set`: accepting a plan wipes the session's history |

Profile, GitHub, and Model are *creatable*: their option lists end with a `+` sentinel that prompts for a new value and persists it to `options.json`. Directory is *freeform*: you can type any path. Version pulls a live npm listing merged with the locally installed binaries.

Model discovers itself: claudewheel asks the Anthropic API which models your account may use, appends any it has not seen to `options.json`, and orders the picker by release date, newest first. The list only ever grows -- a model that stops being served stays selectable, and an offline launch offers everything a previous one discovered.

## Commands

:-: table-commands

### Segment presets

`-s KEY=VALUE` (`--set`) presets one segment, once per segment. Presets pre-fill the TUI:

```bash
claudewheel -s profile=myprofile -s github=myhandle
claudewheel -s directory=~/Projects/foo -s model=claude-opus-4-7
```

If the presets cover every *required* segment, the TUI is skipped entirely and Claude Code launches directly. A value a fixed-choice segment does not offer is refused, naming the values it offers; a freeform segment such as `directory` takes any value.

### Session passthrough

Which session a launch starts in is one selection with five alternatives, exactly one of which is elected per launch. Four of them forward to Claude Code; the fifth is the plain launch a bare `claudewheel` performs:

```bash
claudewheel --cont                                        # --continue: resume the most recent session
claudewheel --resume 0123abcd-0123-4567-89ab-0123456789ab # --resume <id>: jump to a specific session
claudewheel --resume ""                                   # --resume: open Claude Code's own session picker
claudewheel --picker                                      # pick the session from Claude Code's session picker
claudewheel --print-prompt "summarize this repo"          # --print: non-interactive print mode
claudewheel --new-session                                 # start a new session -- what a bare `claudewheel` does
```

Three of them carry a short form: `-c`, `-r <session>`, and `-p <prompt>` are `--cont`, `--resume`, and `--print-prompt`. A short takes its value as the next argument, so `-r 0123abcd-0123-4567-89ab-0123456789ab` is the spelling and `-r=0123abcd-0123-4567-89ab-0123456789ab` is not one.

Naming two of them is refused: `--cont --picker` is `--cont and --picker are mutually exclusive`, from the parser rather than from claudewheel.

These compose with segment presets: `claudewheel -s profile=personal --picker` opens the picker against the personal profile.

Print mode (`--print-prompt`) skips the TUI and launches Claude Code non-interactively; a required segment the presets leave unset is an error. Extra arguments after `--` are passed to the client:

```bash
claudewheel --print-prompt "explain auth.py" -- --output-format json --allowedTools "Read,Bash"
```

## Config directory

`~/.claudewheel/` layout:

| Path             | Purpose                                                     | Auto-written?     |
|------------------|-------------------------------------------------------------|-------------------|
| `config.json`    | Theme, enabled segments, default flags, health-check switch, minimap mode | No (user-edited)  |
| `segments.json`  | Segment definitions (label, width, wrap, searchable, etc.) | No                |
| `options.json`   | Values, metadata, and discovery configs per segment         | Only via `+` UX   |
| `state.json`     | `last_config`, `recent_dirs`, `launch_count`, npm cache    | Yes, every launch |
| `themes/*.json`  | Colour schemes (`dark.json`, `light.json` ship by default)  | No                |
| `hooks/*`        | Executable scripts -- see below                             | No                |

A launch writes the defaults for any file that is missing; read-only commands such as `show` and `health` write nothing. `claudewheel upgrade-workspace` adds the keys the current defaults declare that a file lacks, without changing a value already present, and removes retired keys; every other command refuses a workspace holding a retired key and names it.

## Hooks

Drop an executable script into `~/.claudewheel/hooks/` whose name starts with `pre-launch` (e.g. `pre-launch-token-refresh`). It runs immediately before `exec`, with the chosen segment values exported as `CL_<KEY>` environment variables:

```bash
#!/usr/bin/env bash
# ~/.claudewheel/hooks/pre-launch-warn-work
if [[ "$CL_PROFILE" == "work" && "$CL_DIRECTORY" == "$HOME/Projects/personal-thing" ]]; then
    echo "Refusing to use the work profile on a personal project." >&2
    exit 1
fi
```

A nonzero exit aborts the launch (and prevents `launch_count` from being incremented). Hooks have a 10-second timeout.

## Adding new options

- **Profile / GitHub / Model**: cycle the segment to its `+` sentinel, press Enter, type the new value. It is appended to `options.json` under the segment's `pinned` list and selected.
- **Direct edit**: open `~/.claudewheel/options.json` and add to the relevant segment's `values` array. For profiles you also need a `metadata.<name>.config_dir` entry.
- **Install a Claude Code version**: run `claudewheel install <version>` or pick a not-yet-installed version in the TUI and confirm the install prompt. Binaries land in `~/.local/share/claude/versions/<version>`.

## Themes

Two themes ship with the launcher: `dark.json` and `light.json` in `~/.claudewheel/themes/`. Switch by setting `theme` in `config.json` to the file's basename. Themes define per-segment foreground / focus / option / unavailable colours and the search highlight palette. Add a new theme by writing another `themes/<name>.json` and pointing `config.json` at it.

Themes also include an `overflow` section for viewport chrome:

| Key                | Controls                                                     |
|--------------------|--------------------------------------------------------------|
| `arrow_fg`         | Colour of the `<N` / `N>` edge scroll indicators             |
| `minimap_fg`       | Colour of unselected minimap squares                          |
| `minimap_focused_bg` | Background highlight on the focused minimap square          |
| `minimap_char`     | Character used for minimap squares (default `▪`)              |

## Tests

```bash
heavy -- go test ./...
```
