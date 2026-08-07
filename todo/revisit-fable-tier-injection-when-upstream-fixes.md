# Revisit plan-tier env injection when Claude Code fixes it upstream

## Context

claudewheel injects `CLAUDE_CODE_SUBSCRIPTION_TYPE` (and possibly
`CLAUDE_CODE_RATE_LIMIT_TIER`) into the launch environment for profiles whose
launch carries a setup token. This exists to work around a Claude Code client
bug, not because claudewheel has any business asserting a user's plan tier.

## The upstream bug being worked around

When Claude Code authenticates from `CLAUDE_CODE_OAUTH_TOKEN` (a long-lived
setup token, which is claudewheel's normal auth path), its credential resolver
builds `subscriptionType` as `env.CLAUDE_CODE_SUBSCRIPTION_TYPE || null`. There
is no server-side recovery: the tier would come from `GET /api/oauth/profile`,
but setup tokens are not granted the required scope. Verified response:

```
HTTP 403
"OAuth token does not meet scope requirement any_of(user:profile, user:office)"
```

With the tier null, the client's self-service entitlement check fails closed and
walls Fable 5 behind a usage-credits dialog whose only offered action is
"Request usage credits from your admin" — a dead end for personal plans. The
same account and token serve Fable fine in headless mode, which is what proves
the entitlement is intact and the wall is purely client-side.

Tracked upstream as anthropics/claude-code issues #79597 (the setup-token case,
with root-cause analysis), #79378, #79337, #79441.

## What to check, later

1. Whether the client still hardcodes `subscriptionType` to null on the
   env-token path, or now recovers the tier some other way.
2. Whether setup tokens have gained `user:profile` scope. Re-run the probe:
   `GET https://api.anthropic.com/api/oauth/profile` with `Authorization:
   Bearer <setup-token>`. A 200 means the client can self-configure and the
   whole injection becomes unnecessary.
3. Whether a profile's `.claude.json` has since acquired a real `oauthAccount`
   or `subscriptionType` written by the client itself. If so, claudewheel's
   declared value is now shadowing a legitimate one.

## What to do if it is fixed

Delete the injection and the declared fields outright — no compat shim, no
dual recognition, no version gate. Per project policy for pre-stable code:
remove the old surface, update all callers, and document the one-time manual
cleanup (dropping the declared tier fields from `tokens.json` entries).

## Why there is deliberately no detection machinery

A build-time or run-time check that notices the upstream fix and warns the user
was considered and explicitly rejected. It is a warning shim for a transient
state, it adds a code path whose only purpose is to describe another code path,
and nobody would act on the warning anyway. This file is the mechanism.

## Solutions considered for the workaround itself

| Approach | Pros | Cons |
|---|---|---|
| Inject unconditionally (chosen) | One code path, no version logic, no detection | A stale declared tier silently overrides a correct server value once upstream is fixed |
| Gate on client version | Self-disabling | Dual recognition of old and new client behavior; banned pre-stable; no known-good fix version exists |
| Per-profile opt-out toggle | User keeps control | Requires the user to notice upstream shipped a fix, which will not happen |
| Detect redundancy and report | Catches the stale-override case | Warning shim for a transient state; rejected |

## Affected files

- `claudewheel/profile_store.py` — `ProfileStore.env()`, which assembles the
  launch env vars and is where the injection lands
- `claudewheel/launch.py` — `resolve_launch_config()`, which carries them into
  the exec environment
- `claudewheel/tokens.py` — the entry schema holding the declared fields
- `claudewheel/preflight.py` — the gate that prompts when the tier is undeclared
- `claudewheel/profile_info.py` — reporting the declared tier
- `tests/` — whatever covers the above

## Effort

Small. Removal is a subtraction across the files above plus their tests. The
expensive part is noticing that it became possible, which is what this file is
for.
