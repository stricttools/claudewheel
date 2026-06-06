# Rename config directory from ~/.claudelauncher/ to ~/.claudewheel/

## Context

The project was renamed from "claudelauncher" to "claudewheel" but the config directory still lives at `~/.claudelauncher/`. This is confusing for users and inconsistent with the package name.

## Problem

A naive `mv ~/.claudelauncher ~/.claudewheel` at startup will break any Claude Code session that is currently running with a profile from that directory. Open sessions reference paths like `~/.claudelauncher/options.json`, `~/.claudelauncher/tokens.json`, etc. — moving the directory out from under them mid-session could corrupt state or crash sessions.

## Solutions

### A. Symlink migration (recommended)

1. On startup, if `~/.claudelauncher/` exists and `~/.claudewheel/` does not:
   - `mv ~/.claudelauncher ~/.claudewheel`
   - `ln -s ~/.claudewheel ~/.claudelauncher`
2. If both exist, warn and do nothing (user resolves manually).
3. All new code uses `~/.claudewheel/`. The symlink keeps old sessions alive.
4. After N releases (or a major version bump), remove the symlink creation and just warn if the old dir exists.

| Pros | Cons |
|------|------|
| Zero breakage for running sessions | Leaves a symlink on disk until cleanup phase |
| Atomic from the user's perspective | Two-phase rollout (symlink now, remove later) |
| No user intervention required | |

### B. Copy + deprecation warning

1. On startup, if only `~/.claudelauncher/` exists:
   - Copy it to `~/.claudewheel/`
   - Print a deprecation warning: "Config has moved to ~/.claudewheel/. You can delete ~/.claudelauncher/ once all sessions are closed."
2. New writes go to `~/.claudewheel/` only.

| Pros | Cons |
|------|------|
| No symlink clutter | Data diverges: old sessions write to old dir, new sessions write to new dir |
| User controls when to delete | User must manually clean up |
| | Config changes made in old sessions are lost |

### C. Support both paths with fallback

1. Change `LAUNCHER_DIR` resolution: use `~/.claudewheel/` if it exists, else `~/.claudelauncher/`, else create `~/.claudewheel/`.
2. Never move or copy anything automatically.
3. Add a `--migrate-config` CLI command that does the move when the user is ready.

| Pros | Cons |
|------|------|
| Zero risk of breaking anything | Indefinite dual-path complexity in code |
| User migrates on their own schedule | Easy to forget and stay on old dir forever |

## Files that need changes

### Source code (use `LAUNCHER_DIR` constant everywhere)
- `claudewheel/constants.py:8` -- the canonical definition, rename to `.claudewheel`
- `claudewheel/cli.py:156` -- help text
- `claudewheel/wizard.py:273` -- hardcoded path, should use `LAUNCHER_DIR`
- `claudewheel/health.py:238,275,276,367` -- hardcoded paths, should use `LAUNCHER_DIR`
- `claudewheel/hooks.py:15` -- docstring

### Tests
- `tests/test_health.py` -- 8 occurrences in mock setup
- `tests/test_migration.py` -- 2 occurrences
- `tests/test_profile_ops.py` -- 2 occurrences
- `tests/test_wizard.py` -- 1 occurrence

### Documentation
- `README.md` -- 7 references
- `CLAUDE.md` -- 1 reference
- `CHANGELOG.md` -- 1 reference (historical, arguably leave as-is)

## Bonus fix

`wizard.py` and `health.py` hardcode `Path.home() / ".claudelauncher"` instead of using the `LAUNCHER_DIR` constant from `constants.py`. This should be fixed regardless of the rename -- it's a latent bug.

## Effort

Small-medium. The rename itself is mechanical (31 occurrences, 10 files). The migration logic (solution A) is ~20 lines in `constants.py` or a new `migration.py`. Testing the symlink path needs a few new test cases.
