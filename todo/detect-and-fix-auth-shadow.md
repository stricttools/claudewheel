# Detect and fix auth shadow (claudeAiOauth shadowing tokens.json)

## Problem

When a profile has both a `tokens.json` entry (long-lived OAuth, ~1 year) and a `claudeAiOauth` key in `.credentials.json` (short-lived session auth, ~1 day), Claude Code prefers the `.credentials.json` auth. This silently degrades the user experience -- the long-lived token is ignored and the user has to re-login constantly.

This happens naturally: claudewheel sets up auth via `claude setup-token` (populates `tokens.json`), but if the user ever runs `claude login` or Claude Code auto-creates session auth, `.credentials.json` gets a `claudeAiOauth` entry that shadows the env var.

## Solution

Two parts: detection (health check) and repair (new CLI command).

### Part 1: Health check -- `check_auth_shadow()`

Add a new health check to `health.py` that:

1. For each discovered profile, checks whether both conditions are true:
   - Profile has an entry in `tokens.json`
   - Profile's `.credentials.json` contains a `claudeAiOauth` key
2. If both exist, emit a warning naming the affected profile(s) and suggesting `claudewheel fix-auth <profile>`.
3. Flag whenever both exist, regardless of whether either token is expired. The fix command handles validation.

This check runs at launch (with other health checks) and via `claudewheel health`. It is read-only -- no mutation.

### Part 2: CLI command -- `claudewheel fix-auth`

New subcommand: `claudewheel fix-auth [profile]` (optional `--all` for all profiles).

Behavior:

1. Parse the profile's `.credentials.json`.
2. If `claudeAiOauth` key exists, remove it. Preserve all other keys (`mcpOAuth`, etc.).
3. Write the file back. If the file is now empty (`{}`), still keep it (discovery relies on its existence).
4. Report what was removed.

If `claudeAiOauth` doesn't exist, report "no shadow found" and exit cleanly.

## Design decisions (already made)

- Parse `.credentials.json` contents (accept coupling to Claude Code's credential schema).
- Flag whenever both `tokens.json` entry and `claudeAiOauth` exist, regardless of token validity.
- Health check is non-blocking (warning, not hard error) -- consistent with existing health check UX.
- Fix is surgical: remove only `claudeAiOauth`, preserve `mcpOAuth` and other entries.
- No wizard changes -- detection is sufficient.
- Health checks remain read-only; the fix lives in a separate command.

## Affected files

- `health.py` -- new `check_auth_shadow()` function, add to `run_health_check()` list
- `cli.py` -- new `fix-auth` subcommand registration
- `constants.py` -- may need `.credentials.json` filename constant if not already defined

## Effort

Small. The health check is ~30 lines following existing patterns (`check_tokens`, `check_token_expiry`). The CLI command is ~40 lines of JSON read-modify-write. No new dependencies.
