---
title: Profiles
description: "How claudewheel profiles work: the ~/.claudewheel/ layout, profile discovery, the creation wizard, shared store symlinks, per-profile token storage inside each profile directory, and the isolation model."
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

The `default` profile is the exception: it resolves to an empty environment
(no config dir override, no token injection).

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

- **Delete** (`profile delete`): removes the profile directory (unlinking
  symlinks without following them into shared data, and taking the stored
  token with it), unregisters from `options.json`, and clears any `last_config`
  reference in `state.json`. Refuses to delete the `default` profile.
  Refuses to delete profiles with real data at shared-dir names unless
  `--force-delete` is passed.

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
