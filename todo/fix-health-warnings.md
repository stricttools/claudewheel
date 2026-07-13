# Fix health check warnings

Observed on 2026-07-05. Four warnings surfaced by `claudewheel health`.

## 1. hooks-wired: missing PreToolUse hook-block-unsafe-commands

**Problem:** `shared-settings.json` has only the `Agent`/`hook-block-worktree` PreToolUse entry. The `Bash`/`hook-block-unsafe-commands` entry defined as canonical in `defaults.py` (`build_canonical_shared_settings()`) is missing. All profiles (emergency, lisa, personal, work, zap) inherit this gap.

**Fix:** Add the missing PreToolUse entry to `shared-settings.json`, then run `patch-profiles` to propagate to all profiles. The entry should be:
```json
{"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/m/.claudewheel/scripts/hook-block-unsafe-commands"}]}
```

**Severity:** Medium -- sessions launch without this safety hook.

## 2. settings-defaults: missing disallowedTools

**Problem:** `shared-settings.json` has 15 disallowed tools but `DISALLOWED_TOOLS` in `defaults.py` has 17. Missing: `Artifact`, `DesignSync`, `ReportFindings`. These were added to defaults after the last sync.

**Fix:** Add the three missing tools to `disallowedTools` in `shared-settings.json`, then run `patch-profiles` to propagate.

**Severity:** Low -- unlikely to be invoked but shouldn't be available.

## 3. orphan-profiles: profile "main"

**Problem:** `~/.claudewheel/profiles/main/` exists but has no `.credentials.json` or `settings.json`. Contains only `.claude.json` and a backup. Created 2026-07-03. Not listed in `options.json`.

**Fix:** Delete with `saferm delete -r --description "Orphan profile with no credentials or settings" ~/.claudewheel/profiles/main/`.

**Severity:** Low -- harmless clutter.

## 4. /tmp/claude: 3.5 GB

**Problem:** `/tmp/claude-1000/` has ~30 project scratchpad directories totaling 3.5 GB, exceeding the 2 GB threshold.

**Fix:** Remove stale project dirs that haven't been accessed recently. Could also consider lowering retention or adding auto-cleanup.

**Severity:** Low -- disk space only.
