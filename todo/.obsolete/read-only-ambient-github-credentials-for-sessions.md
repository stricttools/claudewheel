# Read-only ambient GitHub credentials for launched sessions

## Context

Sessions launched by claudewheel inherit the user's ambient GitHub authority
(the gh CLI token, credential helpers), which carries write scopes. The
fleet's no-manual-push rule is therefore enforced only by hooks and
instructions, and agents keep finding unenumerated write paths (`gh api`
POSTs, alternate remotes, explicit-URL pushes). The structural fix is to
remove ambient write authority from sessions entirely: every write path then
fails identically at the provider with a 403, including spellings nobody has
thought of yet.

## Solution

claudewheel already owns the launched session's environment (the
profile-owned variable list with per-variable symmetry tests and the vanilla
path stripping the same set). Add injection of a READ-ONLY fine-grained
GitHub token into every launched session's environment (`GH_TOKEN` /
`GITHUB_TOKEN` — gh prefers the env token over its stored one). Read paths
keep working (watching CI runs, GET api calls, listing runs and PRs); every
write path — push auth through gh's credential helper, api mutations,
release creation — is refused by GitHub regardless of command spelling.

Details to settle at implementation:

- Where the read-only token is stored (the user-level config the launcher
  already owns) and how rotation works.
- The variable joins the profile-owned list with the existing structural
  symmetry test (a variable present on one side and not the other fails
  loudly), and the vanilla path strips it like the rest.
- The token's scopes: the minimal read set that keeps the fleet's observe
  paths working (actions read, contents read, metadata; nothing else).

## Sequencing constraint (critical)

Releases are run FROM sessions, and the release tooling's pushes and
GitHub-release creation need write authority. Before this injection is
enabled, the release tooling must resolve its write credential from its own
configuration rather than the session environment (a companion todo is filed
where the push machinery lives). Enabling the read-only injection first
would break every release run from a launched session — sequence the two
together.

## Honest limits

An ambient SSH key with write access to the forge is outside the launcher's
reach — env injection does not block SSH-authenticated pushes. If such a key
exists, moving it behind a passphrase or out of the default path is a
separate, manual step; note it in the docs when this ships.

## Effort

Small: one variable in the launch-environment composition plus its symmetry
test, token provisioning documented, and the sequencing check with the
companion change.
