# GitHub credentials: injection, identity, and the read-only-token design

## Context

claudewheel injects `GH_TOKEN` into launched sessions (`claudewheel/launch.py`: fetched
at launch via `gh auth token --user <account>`, driven by the github segment). Three
earlier filings pulled at this from different angles (remove the injection; inject a
read-only token instead; the write-path bypass class); the probes of 2026-09-06 settled
the facts and they must now be decided as ONE posture.

## Established facts (probed 2026-09-06)

- The gh keyring holds two accounts. `smm-h` is the MAIN account (owner ruling
  2026-09-06): owns the fleet (dozens of public and roughly thirty private repos),
  push+admin everywhere. `mhxv` is the machine-wide ACTIVE keyring account: owns zero
  repos, collaborates on none, cannot read the private repos — its token carries
  `write:packages` (the only token on the machine with that scope; intent unknown).
- The injected token IS `smm-h`'s keyring token (byte-identical); the injection adds no
  credential, it selects the identity. REMOVING the injection therefore flips every
  session to `mhxv` and breaks private-repo reads and HTTPS fetches — the old removal
  filing's "Cons: none found" was wrong. Removal becomes viable only after
  `gh auth switch --user smm-h` (machine-wide, affects non-Claude shells; owner's
  command to run, not the tool's).
- Per-invocation account selection without a machine-wide switch exists two ways only:
  an env `GH_TOKEN` (what claudewheel does) or a private `GH_CONFIG_DIR` with an edited
  active-user (verified working; note `GH_TOKEN` takes precedence over it).
- The git HTTPS credential helper (`gh auth git-credential`, wired in ~/.gitconfig for
  github.com) returns the env token VERBATIM and unvalidated — so an injected read-only
  token also constrains `git push` over HTTPS, not just gh subcommands.
- SSH remotes bypass all of it: a dozen repos (including this one) push via an SSH
  alias and a key with full `smm-h` rights. The SSH remote is the unstated precondition
  that makes a read-only token unable to break THIS repo's releases; switching a repo
  to HTTPS would silently route its pushes through the env token. Record the
  precondition wherever the posture is documented; separately rule whether the SSH
  write path stays open as the sanctioned human-only escape.
- Claude Code strips its own auth env vars from Bash subprocesses but passes `GH_TOKEN`
  through — every subprocess of every command (including npm lifecycle scripts across
  all transitive dependencies) sees a token with repo+workflow scope. This is the
  original supply-chain motivation and it stands.
- `fetch_gh_token` failure handling: a failed `gh auth token` silently skips the
  injection, so the session runs as the machine-wide active account instead of the
  selected one — silent identity substitution; make it loud when the github segment was
  explicitly selected.
- Neither launch path strips an AMBIENT `GH_TOKEN` (it is outside `PROFILE_ENV_KEYS`
  and the symmetry test, which never exercises the launch-level strip loop). A
  list-driven test asserting the vanilla launch env carries none of the profile keys
  and no `GH_TOKEN` is specified and ready, but its correct expectation depends on the
  posture ruling (strip ambient on vanilla only, vs never let ambient through).

## Fine-grained personal access tokens (verified against GitHub docs + gh source)

- Read-only observation set: Metadata + Actions read (+ Contents read for file/tag
  reads). `gh run list/view/watch` work; the ONLY degradation is the Checks-API
  annotations block — fine-grained tokens cannot hold checks:read (confirmed still true;
  GitHub's own endpoint pages contradict this and are wrong — a reportable docs bug);
  gh prints a 403 notice and continues. Nothing in claudewheel parses gh run output,
  and the repo's own CI checks-wait runs under the Actions-issued token — unaffected.
- Write side, if ever replacing the broad classic token for automation: releases,
  assets, secrets, HTTPS pushes all covered; pushes touching `.github/workflows/**`
  additionally need Workflows write (scaffold-regeneration commits do touch it, so the
  need arises intermittently); GitHub Packages remains classic-token-only; one resource
  owner per token.
- A mint-and-verify protocol is written and ready (must-succeed reads including a
  private repo; must-403 writes including a dry-run push over HTTPS; the SSH control
  demonstrating the boundary). Minting requires the owner at
  github.com/settings/personal-access-tokens — tokens expire hard, so the injection
  breaks the day one lapses; plan rotation.

## The posture decision (one ruling covering the three old filings)

1. Identity: keep the injection as-is (status quo, works), or switch the keyring's
   active account to `smm-h` and then remove the injection.
2. Privilege: keep injecting the broad token, or mint the read-only fine-grained token
   for sessions — accepting that releases then need a separate write authority
   (HTTPS-remote releases break outright under a read-only env token; SSH-remote
   releases keep their push but lose GitHub-Release creation), e.g. release flows
   resolving their own credential rather than the session env.
3. The command-matching layer (deny/ask rules on `gh` mutations, raw HTTP to the API
   hosts) is explicitly the WEAKER layer — a denylist that leaks (demonstrated live:
   with every git-push spelling blocked, `gh api -X DELETE` removed a remote ref
   unchallenged). Keep it as fast, legible refusals if wanted, but the structural fix
   is credential scope. Note the ask-tier interaction: a proposal to put gh writes
   under ask conflicts with the ask-tier-retirement ruling in the guardrail todo —
   resolve together.
4. Housekeeping riders: is `mhxv`'s `write:packages` intentional; comment on the
   upstream tier issue is unrelated — see the tier todo.

## Affected files

`claudewheel/launch.py` (injection, strip loop, fetch failure), `claudewheel/
profile_store.py` (`PROFILE_ENV_KEYS`), `tests/test_launch.py` (the list-driven strip
test), segment discovery for the github account list; docs.

## Supersedes

stop-injecting-gh-token-env.md, read-only-ambient-github-credentials-for-sessions.md,
ban-non-git-remote-write-paths.md.

## Effort

Small once ruled: each option is minutes-to-hours of code. The rulings and the token
mint are the substance.
