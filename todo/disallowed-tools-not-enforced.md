# disallowedTools is not enforced

## Problem

`disallowedTools` is not a valid key in Claude Code's `settings.json`. The correct settings.json equivalent is `permissions.deny`. Claudewheel writes `settings["disallowedTools"]` in the wizard and checks it in health.py, but Claude Code silently ignores unknown top-level keys. Result: no tool has ever actually been banned in any profile.

Evidence from `~/.claude-lisa/settings.json` (lines 534-554): the `disallowedTools` array sits at the top level alongside `permissions`, `hooks`, etc. Claude Code reads `permissions.deny` (line 482) but has no awareness of the `disallowedTools` key.

## Affected Code

- **`claudewheel/defaults.py` lines 3-23**: Defines `DISALLOWED_TOOLS` -- a list of 20 tool names (CronCreate, EnterWorktree, Skill, TaskCreate, etc.). The list itself is correct; the problem is how it gets written.

- **`claudewheel/wizard.py` line 293**: `settings["disallowedTools"] = DISALLOWED_TOOLS[:]` -- writes the list as a top-level key in settings.json. Claude Code does not recognize this key and silently ignores it.

- **`claudewheel/health.py` lines 233-236**: Reads back `s.get("disallowedTools", [])` and checks for missing tools. This health check always passes because it reads the same invalid key that the wizard wrote -- it validates its own mistake, never detecting that the tools are not actually blocked.

- **`claudewheel/launch.py` lines 89-95, 116**: Constructs the CLI argv for launching Claude Code. Currently handles `--model`, `--dangerously-skip-permissions`, and `--permission-mode` flags but does not pass `--disallowedTools`.

- **`~/.claude-lisa/settings.json` lines 482-493 vs 534-554**: Shows the real `permissions.deny` array (git safety rules) at line 482 and the inert `disallowedTools` array at line 534. Both exist side by side; only `permissions.deny` has any effect.

## Solution

### Option A: CLI flags (recommended)

Since claudewheel already constructs the CLI command in `launch.py`, pass `--disallowedTools tool1 tool2 ...` as CLI flags when launching. This is semantically better because `--disallowedTools` removes tools from the model's context entirely (the model never sees them), while `permissions.deny` only blocks tools after the model tries to use them (the model wastes tokens attempting to call blocked tools, then gets an error).

Changes required:

1. **`launch.py`**: After line 95 (permission flags), read `DISALLOWED_TOOLS` and build `disallowed_flags = ["--disallowedTools"] + DISALLOWED_TOOLS`. Append to argv at line 116.
2. **`wizard.py` line 293**: Change the key from `settings["disallowedTools"]` to `settings["claudewheel.disallowedTools"]` (or a nested `settings.setdefault("claudewheel", {})["disallowedTools"]`) to make clear it is a claudewheel-specific persistence key, not a Claude Code native key.
3. **`health.py` lines 233-234**: Update the `get()` call to read from the new claudewheel-specific key instead of the top-level `disallowedTools`.

### Option B: permissions.deny

Change the wizard to merge tool names into `settings["permissions"]["deny"]` instead of `settings["disallowedTools"]`. This works but the model still sees the tools in its context and may waste tokens trying to call them before being blocked.

Option A is recommended.

## Migration

Existing profiles need migration: remove the top-level `disallowedTools` key from each profile's settings.json. If going with Option A, the key can be moved to the claudewheel-specific namespace. If going with Option B, merge its contents into `permissions.deny`.

## Effort

Small (under 1 hour). Three files to change, one migration pass over existing profiles.
