---
description: "claudewheel is a TUI launcher for Claude Code: pick a profile, model, directory, and permissions from a visual segment bar, then launch a session."
---

# claudewheel

A TUI launcher for Claude Code that presents a horizontal segment bar for selecting a profile, model, directory, MCP mode, and permissions before launching a session. Selections persist across launches, and the bar adapts to narrow terminals with viewport scrolling and a minimap.

## Documentation

- [CLI Reference](cli-index.html) -- all commands, flags, and arguments
- [API Reference](gen-index.html) -- auto-generated module and function docs

## Overview

claudewheel manages multiple Claude Code profiles, each with isolated settings and permissions stored in `~/.claude-<name>/settings.json`. The TUI renders a segment bar where each segment fans out into selectable options. Before launching, optional health checks verify API tokens, hook scripts, and file permissions.

Configuration lives in `~/.claudewheel/` (config.json, segments.json, options.json, state.json, and a themes/ directory). Themes support hex color definitions with dark and light variants.

## API Reference

The claudewheel API covers configuration, profile discovery, TUI rendering, terminal I/O, segment bar layout, theme parsing, and session launching. Modules are organized by concern: `config` and `defaults` handle persistent settings, `renderer` and `terminal` drive the display, `segment` defines the bar data model, `launch` builds the exec command, and `profile` resolves Claude Code profile directories.

:-: ref path="claudewheel"
