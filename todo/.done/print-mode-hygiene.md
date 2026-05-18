# Print mode hygiene for programmatic consumers

## Context

A downstream project (Dijkstra) uses claudewheel's print mode (`-p`) programmatically via claudestream to make hundreds of LLM calls. Any stdout contamination breaks output parsing. Any unnecessary work on every invocation adds latency.

## Problems

### 1. Health warnings go to stdout in print mode

When `-p` is used, stdout is the LLM response. Health warnings like:

```
Health warnings:
  [WARN] orphan-profiles: orphans: work
```

are printed to stdout, contaminating the response. Programmatic consumers cannot distinguish warnings from the actual response.

**Fix:** In print mode, health warnings must go to stderr, or be suppressed entirely.

### 2. No way to suppress warnings

Even if warnings go to stderr, they're noisy when running hundreds of calls. There should be a `--quiet` or `--no-warnings` flag that suppresses all non-error output. Print mode could imply `--quiet` by default.

### 3. Orphan profile check runs on every invocation

The orphan-profiles health check runs on every `claudewheel` invocation, including `-p` calls. This is a diagnostic check that belongs in a `claudewheel doctor` command, not a boot-time check. In print mode, the user is a program -- it doesn't care about orphan profiles.

**Fix:** Skip non-critical health checks in print mode. Or: only run health checks when the TUI is shown (interactive mode), never in print mode.

### 4. Profile resolution not available as a library function

claudestream needs to spawn `claude` with the right `CLAUDE_CONFIG_DIR` for a given profile. Currently, the profile → env var mapping only happens inside claudewheel's launch sequence. There's no way to resolve a profile name to environment variables without going through the TUI.

**Fix:** Expose a function like `resolve_profile(name) -> dict[str, str]` that returns the environment variables (CLAUDE_CONFIG_DIR, GH_TOKEN, etc.) for a given profile. claudestream and other programmatic consumers can call this to configure their subprocess environment.

## Effort

Small-medium. Items 1-3 are behavioral fixes in the print mode path. Item 4 is a new public API function.
