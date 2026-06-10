# Store rateLimitTier in tokens.json

## Context

howmuchleft (Claude Code statusline tool) determines the subscription tier label ("Max 5x", "Max 20x", "Pro", etc.) from the `rateLimitTier` field in `<claudeDir>/.credentials.json`. When claudewheel launches a session with `CLAUDE_CONFIG_DIR` pointing to a profile directory (e.g. `~/.claudewheel/profiles/work/`), that directory may not have a `.credentials.json` — Claude Code authenticates via `CLAUDE_CODE_OAUTH_TOKEN` from `tokens.json` instead.

This causes howmuchleft to show "API" instead of the actual subscription tier, because it has no way to determine the tier from the token alone. The usage API (`platform.claude.com/api/oauth/usage`) does not return tier info either.

## Problem

`tokens.json` currently stores only `token`, `created`, and `expires_at`. No subscription metadata.

## Proposed solution

When claudewheel acquires or refreshes an OAuth token, also store `rateLimitTier` (and optionally `subscriptionType`, `memberOfActiveTeam`) in `tokens.json` alongside the token. This data is available during the OAuth flow or can be queried from the credentials endpoint.

Then either:
- Write a minimal `.credentials.json` in the profile directory at launch time (so howmuchleft can read it as-is), or
- Set an additional env var (e.g. `CLAUDE_RATE_LIMIT_TIER`) that howmuchleft can check

## Affected files

- `tokens.json` schema
- Token acquisition/refresh code
- Possibly `launch.py` if writing `.credentials.json` or setting env vars
