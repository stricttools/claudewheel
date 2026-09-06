# Stop injecting GH_TOKEN into launched sessions

## Context

claudewheel currently puts `GH_TOKEN` into the environment of the
Claude Code sessions it launches. Verified in a live session:
`gh auth status` shows the env token as the active auth source
(scopes `gist, read:org, repo, workflow`) — and, directly beneath it,
a keyring login for the same account with the same scopes. The env
injection is therefore redundant: every `gh` use (releases,
`gh secret set`, issue filing) works from gh's own stored auth with
no env var present.

## Problem

Environment variables are inherited by every child process of every
command a session runs. In particular, every `npm install`'s
lifecycle scripts — across all transitive dependencies — run with the
GitHub token in their environment. Env-token harvesting during
install is the standard npm supply-chain-worm playbook (the 2025 worm
did exactly this: scrape `GH_TOKEN`/`NPM_TOKEN` from env and
propagate). With `repo + workflow` scopes the worst-case chain is:
compromised postinstall reads the token → full read/write on all
repos including private ones → workflow modification on a public
repo → exfiltrate Actions secrets (`NPM_TOKEN`, Cloudflare) on next
run → poisoned releases of published packages.

Keyring/config-stored gh auth is meaningfully better on exactly this
axis: a malicious script must actively read gh's config rather than
receive the token for free. (Limit to be honest about: anything
running as the user can still execute `gh auth token` — removing the
injection narrows opportunistic exposure, which is the realistic
attacker, not targeted same-user malware.)

## Solutions

1. **Remove the `GH_TOKEN` injection (recommended).** Zero
   functionality loss — the keyring auth already covers everything.
   One deletion wherever claudewheel composes the session
   environment.
   - Pros: kills the inheritance channel immediately; nothing to
     migrate.
   - Cons: none found; any script that insisted on `$GH_TOKEN`
     specifically (rather than calling `gh`) would need
     `gh auth token` instead.
2. **Also shrink the token**: replace the broad classic OAuth token
   with fine-grained PAT(s) — selected repos, minimal permissions,
   expiry; optionally one for public release work and one for private
   repos, keyed per profile.
   - Pros: caps the blast radius even for a leak via the config-read
     path.
   - Cons: PAT management overhead; fine-grained PATs have API
     coverage gaps to verify against actual usage (releases, secret
     set).
3. **Complementary, independent of 1–2**: put `gh` write commands
   (`gh release *`, `gh secret *`, `gh issue create`, `gh repo
   create`) under `ask` in profile permissions, so outward GitHub
   actions from sessions always surface for approval.
   - Pros: human checkpoint on the agent-mistake class, matching how
     pushes are already treated.
   - Cons: friction on legitimate release flows.

## Affected

Wherever claudewheel builds the launched session's environment
(profile launch path / session-env store), plus any profile docs that
mention gh credentials.

## Effort

Solution 1 is minutes. 2 and 3 are small, independent follow-ups.
