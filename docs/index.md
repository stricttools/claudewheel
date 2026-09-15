+++
description = "A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc."
+++

# claudewheel

A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc.

Selections persist across launches, and the bar adapts to narrow terminals with viewport scrolling and a minimap.

## Documentation

- [CLI Reference](cli-index/) -- all commands, flags, and arguments
- [API Reference](gen-index/) -- auto-generated module and function docs
- [Guardrails](guardrails/) -- enforcement tiers, subagent handling, command-string caveats, and upgrading profiles

## Overview

claudewheel manages multiple Claude Code profiles, each with isolated settings and permissions stored in `~/.claude-<name>/settings.json`. The TUI renders a segment bar where each segment fans out into selectable options. Before launching, optional health checks verify API tokens, hook scripts, and file permissions.

Configuration lives in `~/.claudewheel/` (config.json, segments.json, options.json, state.json, and a themes/ directory). Themes support hex color definitions with dark and light variants.

## API Reference

The claudewheel API covers configuration, profile discovery, TUI rendering, terminal I/O, segment bar layout, theme parsing, and session launching. Modules are organized by concern: `config` and `defaults` handle persistent settings, `renderer` and `terminal` drive the display, `segment` defines the bar data model, `launch` builds the exec command, and `profile` resolves Claude Code profile directories.

:-: ref path="claudewheel"
