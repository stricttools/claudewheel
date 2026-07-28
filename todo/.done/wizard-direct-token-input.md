# Wizard: direct token input without browser or CLI

## Problem

Creating a profile with a pre-existing long-lived token requires manual file surgery: creating the profile directory, writing settings.json, writing .claude.json with onboarding flag, editing tokens.json, and creating symlinks. None of this is exposed through the wizard.

The wizard's auth flow (`wizard.py`) only offers two paths:
1. "Session login" (`claude auth login`) -- opens a browser
2. "Long-lived token" (`claude setup-token`) -- runs a Claude Code subprocess

Both assume the user needs to go through an interactive auth ceremony. But if the user already has a token (e.g., from `claude setup-token` on another machine, or from the Anthropic console), there's no way to just paste it in.

## Proposed solution

Add a third auth option to the wizard: "Paste token directly". The flow:

1. User selects "Paste token directly" in the auth step
2. Wizard prompts for the token string (masked input, like a password field)
3. Wizard validates the token format (matches `sk-ant-*` pattern)
4. Optionally: wizard probes the API to verify the token works (the validation logic already exists in `auth.py` -- `validate_oauth_token()` sends a request to `api.anthropic.com/v1/models`)
5. Wizard writes the token to `tokens.json` with `created` set to today, no `expires_at`
6. Profile creation continues normally (settings.json, .claude.json, symlinks)

## Affected files

- `claudewheel/wizard.py` -- add "Paste token" option to the auth step, implement masked input and token write
- `claudewheel/auth.py` -- reuse `validate_oauth_token()` for optional validation
- `claudewheel/tokens.py` -- may need a helper to add a token entry programmatically (currently `write_tokens()` exists but check if a higher-level "add single token" function would be cleaner)

## Considerations

- The masked input should use the existing `ui.py` form infrastructure if possible, or raw terminal input with echo suppression
- Token validation should be optional (the API might be unreachable, or the token might be for a different environment) -- but defaulting to "validate" is safer
- The `expires_at` field: long-lived tokens from `setup-token` do have an expiration (typically 1 year), but the user may not know it. Could probe the API for token metadata, or just omit the field and let the user discover expiration naturally
- This is a pure convenience feature -- the profile creation ceremony already works manually, this just makes it a first-class wizard option

## Effort

Small. The wizard already has multi-step form logic and the token validation code exists. Main work is adding the option, the masked input field, and wiring the token write.
