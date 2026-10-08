+++
description = "A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc."
+++

# claudewheel

A TUI Claude Code Launcher that lets you have more than one profile, manage sessions lifecycle, pick the exact CC version, model to use (even older unlisted ones), pick which GitHub account to use, etc.

Selections persist across launches, and the bar adapts to narrow terminals with viewport scrolling and a minimap.

## Documentation

- [CLI Reference](cli-index/) -- all commands, flags, and arguments
- [Guardrails](guardrails/) -- enforcement tiers, subagent handling, command-string caveats, and upgrading profiles
- [Probes](probes/) -- OOM kills reported to the sessions they concern, probes over other sessions, and the probe runner service

## Overview

claudewheel manages multiple Claude Code profiles, each with isolated settings and permissions stored in `~/.claudewheel/profiles/<name>/settings.json`. The TUI renders a segment bar where each segment fans out into selectable options. Before launching, optional health checks verify API tokens, hook scripts, and file permissions.

Configuration lives in `~/.claudewheel/` (config.json, segments.json, options.json, state.json, and a themes/ directory). Themes support hex color definitions with dark and light variants.
