---
title: Profiles
description: "How claudewheel profiles work: the ~/.claudewheel/ layout, profile discovery, the creation wizard, shared store symlinks, per-profile token storage inside each profile directory, declaring the account plan Claude Code needs, suppressing and purging the plugin marketplace, recoverable deletion through saferm, and the isolation model."
nav_group: "Concepts"
order: 4
---

# Profiles

A profile is an isolated Claude Code configuration directory. Each profile
carries its own `settings.json` (permissions, hooks, behavior flags) and its
own authentication credentials, so multiple Claude Code sessions can run
simultaneously under different identities, permission sets, and guardrail
configurations without interfering with each other.

## Why profiles exist

Claude Code stores its configuration in a single directory (by default
`~/.claude`). claudewheel overrides this via the `CLAUDE_CONFIG_DIR`
environment variable, pointing each session at a different directory under
`~/.claudewheel/profiles/`. This gives each profile:

- Independent permission rules (allow, deny, ask arrays)
- Independent hook scripts and guardrail configuration
- Independent session credentials or injected OAuth tokens
- Shared session data (projects, tasks, todos) via symlinks to a common store

The built-in `default` profile is a special case: it maps to Claude Code's
own `~/.claude` directory, is managed entirely by Claude Code, and is
read-only to claudewheel. No `CLAUDE_CONFIG_DIR` is set when launching
under `default`, and no token is injected.

## The `~/.claudewheel/` layout

All claudewheel data lives under a single root directory, defaulting to
`~/.claudewheel`. The root can be overridden by setting the
`CLAUDEWHEEL_CONFIG_DIR` environment variable. The `Workspace` class
(`workspace.py`) owns every path derivation from this root.

```
~/.claudewheel/
  config.json            # TUI configuration (segments, theme, minimap)
  segments.json          # segment bar layout
  options.json           # per-segment option lists and pinned values
  state.json             # persistent state (last selections, counts)
  shared-settings.json   # shared hooks, disallowedTools, profileDefaults

  profiles/              # one subdirectory per profile
    work/
      settings.json      # profile-specific permissions and hooks
      .claude.json       # onboarding flag (hasCompletedOnboarding)
      .credentials.json  # session credentials (written by Claude Code)
      projects -> ../shared/projects     # symlink
      session-env -> ../shared/session-env
      file-history -> ../shared/file-history
      tasks -> ../shared/tasks
      todos -> ../shared/todos
      paste-cache -> ../shared/paste-cache
      skills -> ../skills
    personal/
      ...same structure...

  shared/                # shared session data (all profiles point here)
    projects/
    session-env/
    file-history/
    tasks/
    todos/
    paste-cache/
    inodes.json

  skills/                # shared skills directory

  themes/                # custom color themes
  hooks/                 # user hook scripts
  scripts/               # deployed guardrail hook scripts
```

### Key files

- **`profiles/<name>/.claudewheel/token.json`**: the profile's own OAuth
  token entry -- a single JSON object with `token`, `created`, `expires_at`,
  and optional `rateLimitTier`/`subscriptionType` fields. The file is 0600 and
  its directory 0700 (owner-only). Managed by `ProfileDataStore`
  (`profile_data.py`); the entry format lives in `tokens.py`.

- **`shared-settings.json`**: hooks and `disallowedTools` arrays inherited by
  all profiles. When a new profile is created, the wizard reads
  `profileDefaults` from this file as the initial settings template. The
  reconcile system uses it as the canonical source for hook and permission
  drift detection.

- **`options.json`**: tracks which profile names appear in the TUI segment
  bar and which are pinned. Profile metadata (config_dir paths) is never
  persisted here; profile locations are derived from the directory scan at
  runtime.

## Profile discovery

Profiles are discovered at runtime by scanning the filesystem, not by reading
a registry file. The `ProfileStore.enumerate()` method applies these rules in
order:

1. **Default profile**: `~/.claude` qualifies as `default` whenever it is a
   directory. Since it is managed by Claude Code, claudewheel does not require
   `.credentials.json` for discovery (it may be stored elsewhere, e.g. macOS
   Keychain).

2. **Named profiles**: each subdirectory of `~/.claudewheel/profiles/` that
   contains `.credentials.json`, `settings.json`, or claudewheel's own
   `.claudewheel/` data directory qualifies as a profile with the directory
   name as its profile name.

3. **Token presence**: a profile whose own `.claudewheel/token.json` holds a
   token string is marked `has_token=True`.

The result is sorted by name. A corrupt token entry raises `TokenStoreError`
(hard error); a profile storing no entry simply has no token. `discover()`
takes an explicit policy for that error, applied per profile.

## Profile creation wizard

The `profile create` command launches an interactive form wizard
(`wizard.py`) that collects:

- **Name**: lowercase letters, digits, and hyphens. `default` is reserved.
  The config directory path (`~/.claudewheel/profiles/<name>`) is derived
  automatically and shown as a read-only field.

- **Settings source**: either "Defaults template" (uses `profileDefaults`
  from `shared-settings.json`) or the name of an existing profile to clone
  settings from.

- **Advanced checkboxes** (all default to checked):
  - Wire common hooks -- merge canonical hook scripts into the profile
  - Symlink to shared store -- create the six shared-store symlinks
  - Disable recap -- set `awaySummaryEnabled: false`
  - 10-year cleanup period -- set `cleanupPeriodDays: 3650`
  - Disable auto-memory -- set `autoMemoryEnabled: false`
  - Disable Co-Authored-By -- set `attribution: {"commit": "", "pr": ""}`

After form submission, `create_profile()` assembles the final `settings.json`
content and delegates to `ProfileStore.create()`, which performs these steps
atomically:

1. Creates the profile directory under `profiles/`
2. Writes `settings.json` via an atomic file write
3. Sets the `hasCompletedOnboarding` flag in `.claude.json` (so Claude Code
   skips its login screen when a token is injected)
4. Creates symlinks to the six shared-store subdirectories plus the `skills`
   directory (when "Symlink to shared store" is checked)
5. Registers the profile name in `options.json` (pinned)

If any step fails after the directory is created, the entire directory is
removed and the error is re-raised, leaving no partial state.

### Authentication

After profile creation, the wizard prompts for authentication with four
choices:

- **Session login** (recommended): runs `claude auth login` with
  `CLAUDE_CONFIG_DIR` set to the new profile. The user authenticates through
  a browser; Claude Code writes `.credentials.json` on success.

- **Long-lived token**: runs `claude setup-token` under a PTY, captures the
  output, and extracts the token automatically. If extraction fails, a
  manual paste prompt is offered. The token is validated against the
  Anthropic API before saving. Setup tokens are recorded with a 365-day TTL.

- **Paste token directly**: the user pastes a token manually (masked input,
  never echoed). The token is format-checked (`sk-ant-` prefix) and validated
  against the API. Since the token source is external, expiry is recorded as
  unknown.

- **Skip**: no authentication is set up. The profile can be authenticated
  later.

Every token (whether captured or pasted) goes through API validation before
being saved. If validation returns INVALID (401), one re-paste attempt is
offered. If the API is unreachable or the result is inconclusive, the user
can choose to save the unvalidated token or abort.

## Shared store and symlinks

The shared store (`~/.claudewheel/shared/`) holds session data that is
logically shared across all profiles. Rather than duplicating data per
profile, claudewheel creates symlinks inside each profile directory pointing
to the shared store.

The `SharedStore` class (`shared_store.py`) defines six canonical
subdirectories:

| Subdirectory   | Purpose                                            |
|----------------|----------------------------------------------------|
| `projects`     | Per-project session data (JSONL files, context)    |
| `session-env`  | Session environment snapshots                      |
| `file-history` | File access history for sessions                   |
| `tasks`        | Task tracking data                                 |
| `todos`        | Session todo items                                 |
| `paste-cache`  | Cached paste content                               |

A seventh symlink, `skills`, points to `~/.claudewheel/skills/` (a
top-level directory, not inside `shared/`).

### Symlink health states

The `ProfileStore.classify_shared_dirs()` method classifies each shared
entry into one of four states:

- **intact**: the symlink exists and points to the correct shared-store
  target
- **wrong-target**: the symlink exists but resolves to a different path
- **real-dir**: a real directory or file exists at the expected symlink
  location (data that would be destroyed if blindly replaced)
- **missing**: neither a symlink nor a real entry exists

The `profile show` command reports these states. A `real-dir` finding is
flagged as a danger condition because deleting the profile would destroy
actual data rather than just unlinking a symlink.

## Token management

Each profile stores its own token inside its own directory, at
`profiles/<name>/.claudewheel/token.json`, managed by the `ProfileDataStore`
class (`profile_data.py`). The file is written 0600 and its directory 0700
(atomic writes via `write_json_atomic_secret`). Because the entry lives inside
the profile directory, a rename carries it along and a delete removes it --
there is no second place to keep in step.

### Entry format

The file holds one JSON object -- it already belongs to exactly one profile,
so nothing is keyed by name:

```json
{
  "token": "sk-ant-...",
  "created": "2026-01-15",
  "expires_at": "2027-01-15",
  "rateLimitTier": "default_claude_max_20x",
  "subscriptionType": "max"
}
```

For externally-pasted tokens whose expiry is unknown, the entry carries an
`expiry_unknown: true` marker instead of `expires_at`.

### Token lifetime

The `TokenExpiryDisposition` enum controls how a token's lifetime is
recorded:

- **TTL**: a setup-token genuinely valid for 365 days. The entry gets
  `created` (today) and `expires_at` (created + 365 days).
- **UNKNOWN**: an externally-issued token. The entry gets `created` (today)
  and the `expiry_unknown` marker. Expiry is reported as unknown, never
  assumed.

The `entry_expiry()` function resolves an entry's lifetime using the
following precedence: explicit `expiry_unknown` marker yields unknown;
explicit `expires_at` date; `created` + 365 days.

### Token resolution at launch

When launching a session, `ProfileStore.env()` resolves the profile name to
environment variables:

- `CLAUDE_CONFIG_DIR` is set to the profile's directory path
- `CLAUDE_CODE_OAUTH_TOKEN` is set to the token string when the profile
  stores one
- `CLAUDE_CODE_SUBSCRIPTION_TYPE` and `CLAUDE_CODE_RATE_LIMIT_TIER` carry the
  declared plan (see below)
- `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL` suppresses the plugin
  marketplace auto-install (see below)

The `default` profile is the exception: it resolves to an empty environment
(no config dir override, no token injection).

### Plugin marketplace suppression

Left alone, Claude Code clones the official plugin marketplace into a profile
on first launch and installs three language-server plugins from it, costing
six to ten megabytes per profile for something no claudewheel profile asked
for. Every named profile therefore launches with
`CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1`.

Two things to know about that variable:

- **There is no settings key for it.** The marketplace settings keys are
  managed-policy-only, so the launch environment is the only lever. It is also
  undocumented client surface and could change in any Claude Code release.
- **The suppression is one-way per profile.** Once the client has recorded the
  install as `policy_blocked` it treats that as final, so removing the variable
  later does *not* make it try the install again. A profile that should have
  the marketplace needs it installed by hand.

A tree a profile picked up before the suppression stays where it is.
Removing it is the separate, opt-in `purge-plugins` command:

```bash
claudewheel purge-plugins --all-profiles          # every managed profile
claudewheel purge-plugins --profile work          # just this one
claudewheel purge-plugins --all-profiles --dry-run  # inventory only
```

It names the marketplaces and plugins it finds before removing them, and
reports the bytes freed. It is deliberately *not* part of the canonical
reconciliation: that one is exact and runs over every managed target, so
folding a plugin purge into it would delete plugin state on every run,
including state somebody installed on purpose. The `default` profile is never
touched, and a `plugins` entry that is a symlink is left alone -- it points at
data the profile does not own.

### The declared plan

Claude Code reads a subscription tier from `CLAUDE_CODE_SUBSCRIPTION_TYPE` /
`CLAUDE_CODE_RATE_LIMIT_TIER` and *only* from there when auth arrives as a
setup token: its own fallback, fetching the OAuth profile, is refused because
setup tokens lack the `user:profile` scope. With no declared plan the tier is
null and tier-dependent features fail closed.

So a profile that launches on a stored token declares which plan its account
is on. The two fields are stored as one pair in the profile's token entry and
map one-to-one onto the two variables:

| Plan | `subscriptionType` | `rateLimitTier` |
| --- | --- | --- |
| `max-20x` | `max` | `default_claude_max_20x` |
| `max-5x` | `max` | `default_claude_max_5x` |
| `pro` | `pro` | (none) |
| `team` | `team` | `default_claude_max_5x` |
| `enterprise` | `enterprise` | (none) |

Both fields are closed enums measured from the Claude Code binary. A value it
does not recognize is inert there -- the tier resolves to null exactly as an
absent value would -- so an unrecognized value is a hard error naming the
accepted ones rather than a stored string that quietly does nothing.

Three surfaces declare a plan, all fed by the same composite picker and the
same closed list:

1. **The create flow** asks before it captures a token. A session login is not
   asked: it stores no token, and Claude Code reads its own credential file.
2. **The pre-launch prompt** asks when a profile with a stored token has no
   declaration. Cancelling it stops the launch.
3. **`claudewheel profile set-plan <name> <plan>`** declares one without
   prompting -- the remedy a headless launch names when it refuses.

Replacing a profile's token clears the declaration: the entry is rebuilt, so
the plan stated for a retired token can never carry over to its replacement.

A launch with no controlling terminal has nobody to ask, so an undeclared
profile is a hard error naming `profile set-plan` rather than a silent
launch with a null tier.

### Auth shadow detection

An "auth shadow" occurs when a profile has both a long-lived token of its own
AND session credentials (`claudeAiOauth` key) in `.credentials.json`. The session credentials take priority in Claude Code,
effectively shadowing the long-lived token.

The `profile fix-auth` command repairs this by stripping the `claudeAiOauth`
key from `.credentials.json`. Any plan-tier fields that block carried are
discarded with it -- the declared plan is claudewheel's own and lives in the
profile's token entry. A credential file left holding nothing else is removed
rather than kept as an empty object.

A launch never writes `.credentials.json`. The declared plan reaches Claude
Code as `CLAUDE_CODE_SUBSCRIPTION_TYPE` / `CLAUDE_CODE_RATE_LIMIT_TIER` in the
launch environment, so nothing has to be stashed in the credential file.

### Token validation

The `profile check-tokens` command validates every discovered profile's
stored OAuth token against the Anthropic API, reporting which tokens are
valid, expired, or rejected.

## Profile isolation model

Each profile is isolated through directory separation and environment
variable injection:

- **Settings isolation**: each profile has its own `settings.json` with
  independent permission arrays (allow, deny, ask), hook configurations,
  and behavior flags. Profiles do not inherit from each other at runtime;
  cloning at creation time copies settings once.

- **Credential isolation**: each profile can have its own
  `.credentials.json` (session-scoped, written by Claude Code) and/or its own
  token entry under `.claudewheel/` (long-lived, managed by claudewheel). The
  two mechanisms are independent.

- **Session data sharing**: the six shared-store symlinks mean that session
  data (project context, file history, tasks) is shared across profiles.
  This is intentional: switching profiles changes permissions and identity
  but preserves project context. A profile can opt out of sharing by
  unchecking "Symlink to shared store" during creation, which gives it
  plain directories instead of symlinks.

- **Launch-time injection**: at launch, only two environment variables
  control profile selection: `CLAUDE_CONFIG_DIR` (points Claude Code at
  the profile directory) and `CLAUDE_CODE_OAUTH_TOKEN` (injects the
  stored token). No other state leaks between profiles.

### Profile operations

- **Rename** (`profile rename`): atomically renames the profile directory
  (the token entry travels inside it) and updates `options.json` and
  `state.json`. A crash-safe
  breadcrumb file (`.rename_pending`) ensures incomplete renames can be
  recovered on next startup.

- **Delete** (`profile delete`): hands the whole profile directory to
  [saferm](https://github.com/smm-h/saferm), which archives it and then removes
  it, unregisters from `options.json`, and clears any `last_config` reference
  in `state.json`. The archive walk records symlinks as links rather than
  following them, so the shared store behind a profile's links is neither
  copied out nor disturbed, and the stored token goes into the archive with the
  rest of the directory. Refuses to delete the `default` profile. Refuses to
  delete profiles with real data at shared-dir names unless
  `--force-delete-data` is passed (`--force-delete` is the separate flag for a
  profile holding a live interactive session).

  The deletion prints the handle that undoes it:

  ```
  Profile 'work' deleted.
    Archived as 4129d284-7510-4281-937d-286b42bb8d6c
    Restore it with: saferm undelete --no-update-git-index 4129d284-7510-4281-937d-286b42bb8d6c
  ```

  That one command puts the profile back in a working state, its token file at
  mode `600` and its launch environment resolving again.
  `--no-update-git-index` is on both halves of the round trip for the same
  reason: a profile can sit inside a git worktree (a version-controlled
  `~/dotfiles` is the ordinary case), and neither the archival nor the restore
  may stage that directory -- its `.credentials.json` and its stored OAuth
  token among it -- in an index claudewheel does not own.

  Nothing about the archive is recorded on claudewheel's side: saferm's own
  archive is the authority and keeps the audit trail, so the record stays
  reachable through `saferm list` and `saferm info <uuid>` afterwards.

  Two limits of the round trip, stated so nobody assumes otherwise:

  - **The archived directory's own mode is not restored.** Nested entries come
    back with the modes they had -- the token file at `600`, the
    `.claudewheel/` directory holding it at `700` -- but the profile directory
    itself comes back at the default instead of whatever it was. That is
    acceptable because nothing sensitive lives at a profile's top level; it is
    not a guarantee that it will be preserved.
  - **A profile holding a socket cannot be deleted at all.** saferm currently
    refuses the whole directory when it finds one (`archive/tar: sockets not
    supported`) rather than skipping it, and claudewheel surfaces that as a
    hard error with the profile untouched -- nothing is archived and nothing is
    removed. Delete the socket first. When saferm ships the skip, the archival
    will proceed and record the socket as skipped.

### saferm is a precondition of deletion

`profile delete` will not run without a usable saferm, and there is no flag
that makes it. claudewheel asks `saferm capabilities` what the installed binary
ships and compares the answer against the four features the delegation uses --
`machine-payloads`, `on-error-modes`, `git-index-switches` and `uuid-handles`.
No version string is ever compared: a locally built saferm reports a Go
pseudo-version no semver parser accepts, and a release number says nothing
about what a build carries.

A missing binary, a binary too old to answer the probe, and one missing a
single feature are one situation with one remedy:

- **At a terminal**, claudewheel says which of the three it is, states that
  deleting without saferm would be irreversible, and offers to install it. The
  install fetches the release's published `checksums.txt`, downloads this
  platform's asset, verifies its SHA-256 against the manifest line, and only
  then unpacks the binary into `~/.claudewheel/bin/saferm` -- where detection
  looks before it consults `PATH`. Declining aborts the deletion; so does a
  checksum mismatch, and so does a freshly installed binary that still cannot
  answer the probe.
- **Without a terminal** -- a script, an agent, a monitored job -- there is no
  second question to ask, so the refusal is a hard error naming the install and
  nothing else. Nothing was deleted and the profile is still there, which is
  what a caller reading the exit code needs to know.

Installing it yourself works just as well:

```bash
go install github.com/smm-h/saferm@v0
npm install -g saferemove
uv tool install saferm
brew install smm-h/tap/saferm
```

- **Inspect** (`profile show`): gathers a detailed report including
  registration status, credential and token presence, token expiry, auth
  shadow detection, shared-dir health, permission counts, active session
  count, and disk usage.

### The `default` profile

The `default` profile is not a claudewheel-managed profile. It is Claude
Code's own `~/.claude` directory, included in the profile list for
convenience so users can launch vanilla Claude Code sessions from the TUI.
claudewheel never writes to `~/.claude`, never injects environment variables
for it, and never applies guardrail reconciliation to it.
