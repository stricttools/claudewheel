---
title: Getting Started
description: "Install claudewheel, create your first profile, understand the segment bar, and launch a Claude Code session with the right model and permissions."
nav_group: "Guides"
order: 2
---

# Getting Started

This tutorial walks through installing claudewheel, creating a profile, navigating the segment bar, and launching your first Claude Code session.

## Prerequisites

- Python 3.11 or later
- Claude Code installed (`npm install -g @anthropic-ai/claude-code`)
- A terminal that supports ANSI colors

## Install claudewheel

Install from PyPI using `pipx` (recommended) or `uv`:

```bash
pipx install claudewheel
```

```bash
uv tool install claudewheel
```

Verify the installation:

```bash
claudewheel --help
```

If you previously used the deprecated npm package, remove it first:

```bash
npm uninstall -g claudewheel
```

## First run

Launch the TUI:

```bash
claudewheel
```

On the first run, claudewheel creates `~/.claudewheel/` and populates it with default configuration files: `config.json`, `segments.json`, `options.json`, `state.json`, and a `themes/` directory with dark and light color schemes.

Before the segment bar appears, you are prompted to choose a **client** -- `claude` (the official Claude Code CLI) or `miniclaude` (an alternative REPL). Select `claude` to continue.

If no profiles exist yet, the launcher shows an empty Profile segment. You can create one from inside the TUI or ahead of time with the profile wizard.

## Create a profile

A profile is an isolated Claude Code configuration directory (`~/.claudewheel/profiles/<name>/`) with its own `settings.json`, OAuth credentials, and permission rules. Profiles let you maintain separate settings for different contexts -- personal projects, work, experimentation -- without them interfering with each other.

Run the profile wizard:

```bash
claudewheel profile create
```

The wizard walks through several steps:

1. **Name** -- a lowercase identifier (letters, digits, hyphens). This becomes both the directory name and the label in the segment bar.
2. **Clone from** -- optionally copy settings from an existing profile or start from the defaults template.
3. **Advanced options** -- toggle hook wiring, shared store symlinks, recap, auto-memory, cleanup period, and Co-Authored-By attribution.
4. **Authentication** -- after the profile directory is created, the wizard launches Claude Code under the new profile so you can complete OAuth login.

Once the wizard finishes, the new profile appears in the Profile segment the next time you launch the TUI.

You can also create a profile from inside the TUI itself: cycle the Profile segment to the `+` sentinel (the last entry in the list), press Enter, and type the new name.

## Understanding the segment bar

The segment bar is the core of the claudewheel interface -- a horizontal row of labeled cells rendered at the vertical center of your terminal. Each cell controls one aspect of the Claude Code session you are about to launch.

### The segments

| Segment | Label | What it controls |
|---------|-------|-----------------|
| Profile | Profile | Which `~/.claudewheel/profiles/<name>/` directory to use as `CLAUDE_CONFIG_DIR` |
| GitHub | GH | Which GitHub account to authenticate with (exports `GH_TOKEN`) |
| Version | Ver | Which Claude Code binary version to use |
| Model | Model | The model ID (e.g. `claude-sonnet-4-20250514`); a `[1m]` suffix enables the 1M-context window |
| Directory | Dir | The working directory Claude Code starts in |
| MCP | MCP | MCP profile mode (`default` or `strict`) |
| Permissions | Perms | Permission mode (`bypass`, `default`, `plan`, or `auto`) |

### Navigation

- **Left / Right** -- move focus between segments.
- **Up / Down** -- cycle the focused segment through its available values. A blank `---` entry is part of the cycle.
- **Type characters** -- on searchable segments (Profile, Model), start a fuzzy search. On freeform segments (Directory), type any value directly.
- **Tab** -- accept the current fuzzy match and advance to the next segment.
- **Backspace** -- delete a search or edit character. On a non-empty selected value, enters edit mode.
- **Esc** -- cancel the in-progress search or edit.
- **Enter** -- launch Claude Code with the current selections.
- **q / Ctrl-C** -- quit without launching.

### Fan-out display

Above and below the focused segment, a vertical "fan-out" shows other available options dimmed in the segment's accent color. This lets you see all choices at a glance without cycling through them one by one.

### Narrow terminals

When the bar is wider than your terminal, the renderer switches to a scrolling viewport. The focused segment stays centered, edge arrows (`<2`, `3>`) indicate how many segments are off-screen, and a minimap in the top-right corner shows all segments as colored squares.

## Launch a session

Once every segment has a value you are satisfied with, press **Enter**. claudewheel runs any pre-launch hooks, resolves your selections into the correct binary path, environment variables, and flags, then `exec`s Claude Code.

Your selections persist in `state.json`, so the next time you launch, the bar starts with your previous choices pre-filled.

### Skipping the TUI

If you already know what you want, pass segment values as flags to skip the TUI entirely:

```bash
claudewheel --profile work --model claude-opus-4-7 --directory ~/Projects/myapp
```

When every required segment is covered by flags, the TUI is skipped and Claude Code launches directly.

### Session passthrough

claudewheel forwards session management flags to Claude Code:

```bash
claudewheel -c                          # resume the most recent session
claudewheel -r                          # open the session picker
claudewheel -r 0123abcd                 # resume a specific session by ID
claudewheel -p "summarize this repo"    # non-interactive print mode
```

These compose with segment overrides:

```bash
claudewheel --profile personal -r      # session picker against the personal profile
```

## Common workflows

### Switching profiles

You have two main ways to switch between profiles:

- **In the TUI** -- focus the Profile segment (Left/Right keys) and cycle through options (Up/Down) or type to fuzzy-search.
- **Via flags** -- pass `--profile <name>` to pre-select or skip selection entirely.

Each profile carries its own `settings.json` with permissions, hooks, and preferences. Switching profiles is how you move between different permission setups (strict for production work, relaxed for experiments) or different GitHub accounts.

### Changing models

Focus the Model segment and cycle or search. Models are listed from `options.json` and include common options like `claude-sonnet-4-20250514` and `claude-opus-4-7`. The `[1m]` suffix on a model name enables the extended 1M-token context window.

To add a model not in the list, cycle to the `+` sentinel and type the full model ID. It is saved to `options.json` for future launches.

From the command line:

```bash
claudewheel --model claude-opus-4-7
```

### Running health checks

Verify that your profiles, tokens, hooks, and permissions are correctly configured:

```bash
claudewheel health
```

This checks OAuth token validity, hook script deployment, permission array consistency against the canonical guardrail model, and file permissions.

### Managing versions

List installed Claude Code versions:

```bash
claudewheel versions
```

Install a specific version:

```bash
claudewheel install 2.1.119
```

In the TUI, the Version segment shows both installed and available versions. Selecting a not-yet-installed version prompts you to install it.

### Keeping guardrails up to date

After upgrading claudewheel, reconcile your profiles with the latest guardrail rules:

```bash
claudewheel reconcile-permissions --dry-run   # preview changes
claudewheel reconcile-permissions --apply      # apply changes
claudewheel patch-profiles                     # sync hooks and disallowedTools
```

## Next steps

- [CLI Reference](cli-index.html) -- full documentation for every command and flag
- [Guardrails](guardrails.html) -- how the enforcement tiers, hooks, and permission arrays work
- [API Reference](gen-index.html) -- module-level documentation for contributors
