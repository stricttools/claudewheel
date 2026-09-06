# Revisit the plan-tier env injection when upstream fixes setup-token entitlements

## What claudewheel does and why it must stay (re-verified 2026-09-06)

claudewheel injects `CLAUDE_CODE_SUBSCRIPTION_TYPE` (and `CLAUDE_CODE_RATE_LIMIT_TIER`)
for profiles authenticating from a stored setup token, because the client resolves the
tier from the environment or not at all on that path. Re-verified against client
2.1.236's own code: the credential object built from an env token hardcodes the tier to
the env var or null, the internal override hooks are compiled dead, and a null tier
propagates into FEATURE ELIGIBILITY (not just billing display). Live probe: the profile
endpoint returns 403 for setup tokens — scope requirement is now
any_of(user:profile, user:office); setup tokens carry neither. The `user:office` scope
is new and unexplored — a login flow that grants it would be a second path to a
self-configuring token.

## Upstream state (checked 2026-09-06)

- A maintainer CONFIRMED the root cause on the issue to track — anthropics/claude-code
  **#79597** is the well-written but untriaged report; the acknowledged one is
  **#79360** (labeled bug, area:auth): setup tokens are inference-only and structurally
  cannot read entitlements; the 2.1.227 fix covered only the sibling expired-login
  case; two candidate fixes were named (grant these tokens plan-inclusion read, or stop
  deciding client-side) and NEITHER had shipped through 2.1.263. **#81350** is the most
  recently active duplicate. The four issue numbers in the original filing are mostly
  dead ends (self-closed duplicates, a closure the reporters contradicted).
- The env-var workaround appears NOWHERE in the upstream changelog or docs — it is
  undocumented and could regress silently in any release. The accepted tier strings are
  extractable from the installed binary by bounded grep; a repeatable extraction would
  turn claudewheel's hand-maintained tier lists into a checkable fact.
- One community report says the two env vars restored the plan header but made the top
  model tier disappear from the model picker on 2.1.224 — not reproduced here; check
  when touching this area.

## What to re-check, later

1. Does the client still hardcode the tier on the env-token path (grep the installed
   binary), or did either candidate fix ship (changelog)?
2. Do setup tokens now pass `GET /api/oauth/profile` (200 instead of the 403 scope
   error)? A control: the same token should still pass the models endpoint.
3. Does an interactive-login credential (the client's own stored OAuth blob) reach the
   profile endpoint where a setup token cannot? If yes, the workaround could narrow to
   the setup-token path only instead of applying to every stored-token profile.

## What to do when it is fixed

Delete the injection and the declared fields outright — no compat shim, no version
detection, per pre-stable policy: remove the env assembly for the two vars, the token
entry's plan fields and their validation, the pre-launch plan-declaration prompt/abort,
`profile set-plan`, and the reporting of declared tiers; document the one-time cleanup
(dropping the fields from stored token entries). The affected surfaces are the token
entry schema, the profile store's env assembly, the launch config, the preflight step,
profile inspection output, and their tests.

## Why there is deliberately no detection machinery

A runtime check that notices the upstream fix is a warning shim for a transient state.
This file is the mechanism.

## Supersedes

revisit-fable-tier-injection-when-upstream-fixes.md.
