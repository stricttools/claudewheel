# resolve_profile() constructs a full ConfigManager (breaks read-only / headless environments)

## Context

`claudewheel.profile.resolve_profile(name)` resolves a profile name to a dict of env vars
(`CLAUDE_CONFIG_DIR` + `CLAUDE_CODE_OAUTH_TOKEN`). A consumer (a Python library wrapping
the claude CLI as a subprocess for server-side one-shot completions) calls it from within
an asyncio web server's background job to authenticate headless LLM calls. The claudewheel
profile dir is bind-mounted **read-only** into a Docker container (the security-correct
posture: the server should read a token, never modify profiles).

## Problem

`resolve_profile()` (`profile.py:14`) instantiates `ConfigManager()`, whose
`__post_init__()` (`config.py:170`) calls `_ensure_dir()` (`config.py:232`), which
unconditionally creates directories and writes default files:

```python
CONFIG_DIR.mkdir(exist_ok=True)         # line 234 — succeeds (mount root exists)
THEMES_DIR.mkdir(exist_ok=True)         # line 235 — OSError: Read-only file system
HOOKS_DIR.mkdir(exist_ok=True)          # line 236 — would also fail
# then writes config.json, segments.json, options.json, state.json,
# themes/dark.json, themes/light.json, shared-settings.json — all fail
```

On a read-only filesystem the first mkdir that targets a nonexistent subdir crashes with
`OSError`. In practice `themes/` is the first to fail because it's alphabetically and
codepath-first. But even if `themes/` existed, `hooks/`, six JSON files, and the
`_ensure_shared_settings` + `_migrate` paths would all fail for the same reason.

Every artifact `_ensure_dir` creates is **TUI infrastructure** (color themes, hooks, UI
segments, state persistence). `resolve_profile()` needs **none** of it — it only reads
`tokens.json` (token) and `options.json` (profile metadata).

## Proposed solutions

1. **Make `resolve_profile()` do lightweight read-only profile lookup (recommended).**
   It needs exactly: `_discover_profiles()` (scan `profiles/*/` for `.credentials.json`
   or `settings.json`), read `tokens.json` for the token, and optionally read
   `options.json` for the active-profile setting. All of those are pure reads. Factor
   them into a standalone function (or a slim read-only config reader) that never
   constructs `ConfigManager`. `resolve_profile` becomes ~20 lines of direct file I/O
   with no side effects.
   - Pros: eliminates the entire class of problems — any future dir/file that
     `ConfigManager.__post_init__` adds can never break headless consumers. Clean
     separation between "TUI environment init" and "profile/token resolution."
   - Cons: mild duplication if `ConfigManager` and the lightweight reader both parse
     `tokens.json` / `options.json` — extract a shared reader if needed.

2. **Guard `_ensure_dir()` against read-only filesystems** (defense in depth, not
   standalone fix). Wrap each `mkdir` / `open(..., 'w')` in `try/except OSError`. The TUI
   features degrade gracefully (default themes, no hooks, no state persistence) — which
   is already the correct behavior for a headless subprocess that never renders a TUI.
   - Pros: `ConfigManager` itself becomes tolerant of read-only mounts; benefits any
     future consumer that constructs it in a constrained environment.
   - Cons: doesn't fix the architectural issue that `resolve_profile` pulls in a full
     TUI init for a profile lookup; `ConfigManager` still does unnecessary work.

3. **Both (1 + 2):** lightweight `resolve_profile` for consumers + a resilient
   `_ensure_dir` for the TUI launcher itself. Most correct.

## Affected files

- `claudewheel/profile.py` — `resolve_profile()` (the public API; refactor to not use
  `ConfigManager`).
- `claudewheel/config.py` — `ConfigManager.__post_init__` / `_ensure_dir` (guard against
  OSError for option 2).
- `claudewheel/constants.py` — `CONFIG_DIR`, `THEMES_DIR`, `HOOKS_DIR` definitions
  (reference only; unchanged).

## Workaround for consumers blocked now

Pre-create every directory and default file `_ensure_dir` expects on the host before the
read-only mount:
```
themes/  hooks/  config.json  segments.json  options.json  state.json
shared-settings.json  themes/dark.json  themes/light.json
```
Then `mkdir(exist_ok=True)` is a no-op and `_ensure_dir` skips writing existing files.
This is fragile (must track `_ensure_dir` across claudewheel releases) and should be
retired once the real fix ships.

## Effort

Small. Option 1 is ~30 lines of new code + deleting the `ConfigManager()` call from
`resolve_profile`. Option 2 is wrapping 8-10 lines in try/except. Tests: a red-green
test that calls `resolve_profile` with `CONFIG_DIR` pointing at a read-only tmpdir (e.g.
a SquashFS mount or `os.chmod(dir, 0o555)`) and asserts it returns the correct env dict
without raising. Roughly 1-2 hours including tests.
