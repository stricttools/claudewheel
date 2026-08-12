---
title: Health Checks and Preflight
description: "How claudewheel's diagnostic health checks and pre-launch preflight steps work: what each check detects, how to interpret output, common problems and solutions, and the reconciliation model."
nav_group: "Concepts"
order: 6
---

# Health Checks and Preflight

claudewheel runs two categories of diagnostics: **health checks** (on-demand
inspection of the entire profile fleet) and **preflight steps** (a fixed
sequence of gates that run before every session launch). Health checks are
read-only and report problems. Preflight steps can block a launch or
self-heal configuration drift.

## Health checks

Run health checks with:

```
claudewheel health
```

The command runs every registered check, prints a one-line result for each,
and exits. There is no `--fix` flag; checks only report. Remediation
commands are suggested in the output where applicable.

### Output format

Each line is formatted as:

```
[OK]   label: detail
[WARN] label: detail
```

`OK` means the check passed. `WARN` means something needs attention; the
detail message explains what and usually names the command to fix it.

### Check inventory

The checks run in a fixed order. Token data is read per profile, inside the
checks that need it; an unreadable entry degrades gracefully (it surfaces as a
failed token check naming that profile, rather than crashing the entire run).

#### tmpfs

Runs `df --output=pcent /tmp` and warns when usage exceeds 80%. Claude Code
scratchpad directories live on tmpfs, so high usage can cause session
failures.

#### /tmp/claude

Measures the real disk usage of `/tmp/claude-$UID/` (the per-user Claude
Code scratch tree). Counts only regular files via `st_blocks * 512` (real
block usage, not apparent `st_size`) and never follows symlinks, so symlink
targets living outside `/tmp` are excluded. Warns above 1 GB.

#### shared-symlinks

Verifies that every managed profile's shared directories
(`projects`, `session-env`, `file-history`, `tasks`, `todos`, `paste-cache`,
and optionally `skills`) are symlinks pointing into
`~/.claudewheel/shared/`. A directory that is a real directory instead of a
symlink, or a broken symlink, is flagged. The `default` profile (`~/.claude`)
is excluded -- it is Claude Code's own config dir and is never managed by
claudewheel.

#### hooks-wired

Checks that every managed profile's `settings.json` contains the 4 canonical
hook wirings:

| Event | Matcher | Script |
|---|---|---|
| `UserPromptSubmit` | (empty) | `hook-timestamp` |
| `PreToolUse` | `Agent` | `hook-block-worktree` |
| `PreToolUse` | `Bash` | `hook-block-unsafe-commands` |
| `PostToolUse` | `Bash` | `hook-advise-commands` |

A hook entry matches only when its event, matcher, and the exact canonical
command path (under the current `scripts_dir`) all agree. A hook pointing at
the right script name under a stale directory does not pass.

Fix: `claudewheel patch-profiles`

#### settings-defaults

Verifies per-profile settings that claudewheel expects to be set:

- `awaySummaryEnabled` must be `false`
- `cleanupPeriodDays` must be at least 365
- `autoMemoryEnabled` must be `false`
- `permissions.disableAutoMode` must be `"disable"`
- `claudewheel.disallowedTools` must contain all canonical disallowed tools
- No inert top-level `disallowedTools` key (Claude Code ignores it at the
  top level; it belongs under the `claudewheel` namespace)

Fix: `claudewheel patch-profiles`

#### settings-drift

Compares each managed profile's `hooks` and `disallowedTools` against
`shared-settings.json`. Reports structural differences (missing keys, extra
keys, value mismatches) per profile. This catches profiles that have drifted
from the fleet-wide baseline.

#### canonical-drift

Compares each managed profile's `permissions.deny` and `permissions.ask`
arrays against the canonical guardrail model, and flags any
`permissions.allow` entry that conflicts with a canonical deny/ask rule.
Also checks the `profileDefaults` block in `shared-settings.json` (which
seeds new profiles).

Reports missing canonical entries, extra non-canonical entries, and
conflicting allows per profile.

Fix: `claudewheel reconcile-permissions`

#### hook-drift

Byte-compares each deployed hook script in `~/.claudewheel/scripts/` against
the in-memory model generated from the guardrail spec. Drift means a
deployed script no longer matches what `claudewheel deploy-hooks` would
write -- typically a stale copy left after upgrading claudewheel. Scripts not
yet deployed are skipped (absence is not drift).

Fix: `claudewheel deploy-hooks <name> --force-overwrite`

#### hook-path-drift

Detects hook command paths in `shared-settings.json` and per-profile
`settings.json` that reference a scripts directory other than the current
one. This happens when the `~/.claudewheel/` workspace was relocated (e.g.
from a home directory change). Only claudewheel-managed scripts (whose
basename is in the hook script registry) are checked; user-custom hooks are
ignored.

Fix: `claudewheel patch-profiles`

#### tokens

Verifies every managed profile holds its own stored token entry. Profiles that
have neither credentials nor a token (brand-new profiles that have not set up
auth) are skipped. The
`default` profile is excluded (it has no claudewheel-managed token).

#### token-expiry

Warns when any token is within 30 days of its expiry. Tokens issued by
`claude setup-token` have a 365-day TTL computed from their creation date.
Externally-issued tokens with unknown expiry are never flagged.

Fix: `claude setup-token` to re-authenticate

#### auth-shadow

Detects profiles where a session-scoped `.credentials.json` `claudeAiOauth`
entry shadows the profile's own long-lived token. The session credential
takes precedence at runtime, which can cause surprising auth behavior when
the session credential expires.

Fix: `claudewheel profile fix-auth <name>`

#### orphan-profiles

Finds directories under `~/.claudewheel/profiles/` that are not discoverable
as profiles (no `.credentials.json`, no `settings.json`, no `.claudewheel/`
data directory) and
not listed in `options.json`. For each orphan, also reports any broken
symlinks inside the directory.

#### file-perms

Verifies per-profile secrets are locked down: `.credentials.json` and the
stored token entry at mode `0600`, and the `.claudewheel/` data directory that
holds the entry at mode `0700`. Overly permissive modes are flagged.

#### inode-renames

Compares inode records (from `inodes.json` in the shared store) against the
filesystem to detect directory renames. When two paths share an inode and
one path no longer exists, the check reports the rename and suggests the
migration command. Stale entries (deleted directories with no matching
inode) are pruned automatically when possible.

Fix: `claudewheel mv --post-hoc <old> <new>`


## Preflight steps

Preflight steps run automatically after TUI selections are saved but before
the Claude Code binary is launched. Each step receives a read-only context
(selections, workspace, binary locator, config store, interactive flag) and
returns either `CONTINUE` (proceed to the next step) or `ABORT` (stop the
launch with an actionable message). There is no "skip" verdict -- a step
that has nothing to do returns `CONTINUE`.

Steps run in a fixed registration order. The first `ABORT` halts the entire
sequence. Steps marked `renders_ui=True` manage their own raw-mode terminal;
steps marked `runs_in_non_interactive=False` are skipped on the
non-interactive (print/skip-TUI) code path.

### Step sequence

#### 1. vanilla-choice

Runs when the selected profile is the `default` (`~/.claude`). On the first
interactive launch, renders a one-time choice page: stay vanilla (no
claudewheel guardrails applied to `~/.claude`) or opt in to claudewheel's
hook wiring. The choice is persisted in `state.json` and never asked again.

- Opted in: claudewheel additively injects its canonical hook wiring into
  `~/.claude/settings.json` via `merge_hooks` (never pruning, never touching
  non-hook keys).
- Opted out: `~/.claude/settings.json` is left untouched.
- Non-interactive path: an existing opt-in is honored (guardrails ensured);
  an unset flag stays vanilla without prompting or persisting.

This step never aborts.

#### 2. reconcile-guardrails

Runs the unified reconcile core (`reconcile_workspace`) over
`shared-settings.json` and every managed profile to heal the guardrail
surface to canonical. Deploys missing hook scripts and prunes drift.
Best-effort: any failure is swallowed so a reconcile problem never blocks a
launch. Concurrent launches racing on the same files are safe because the
output is idempotent.

This step never aborts.

#### 3. model-version-guard

Checks whether the selected model requires a minimum Claude Code CLI
version. If the effective binary version (explicit selection or symlink
target) is older than the model's minimum, the launch aborts with a message
naming the required version and the install command.

#### 4. approved-hooks

Gates the launch on the target project's Claude Code hooks
(`.claude/settings.json` and `settings.local.json` in the project directory).
If the project defines hooks, the combined fingerprint is compared against a
stored per-project approval:

- Matching fingerprint: continue silently.
- New or changed fingerprint, interactive: render an approval page listing
  every hook (event, matcher, command). Approve persists the fingerprint;
  decline aborts.
- New or changed fingerprint, non-interactive: abort (never silent trust).
- Malformed project hooks config: abort.

#### 5. scratchpad-cleanup

Interactive-only. Scans `/tmp/claude-$UID/` for stale per-project scratchpad
directories. When stale directories are found, renders a confirmation page
listing each one with its size and age. Confirm deletes them all (per-dir
errors are reported but do not abort). Decline sets a 7-day snooze so the
prompt does not appear on every launch.

This step never aborts.


## The reconciliation model

claudewheel maintains a canonical guardrail model (defined in
`guardrail.py` and `defaults.py`) that specifies the exact state every
managed profile's settings must converge to. Two CLI commands and one
preflight step enforce this convergence.

### What is reconciled

The reconcile core (`reconcile.py`) brings each target file's guardrail
sections into exact agreement with the canonical model:

- **hooks**: the entire hooks structure is replaced with the canonical
  wiring. User-added hook entries are pruned.
- **disallowedTools**: made exactly equal to the canonical list. In profile
  settings it lives under the `claudewheel` namespace; the inert top-level
  key is dropped. In `shared-settings.json` it lives at the top level.
- **permissions.deny / permissions.ask**: made exactly equal to
  `canonical_deny_rules()` / `canonical_ask_rules()` -- missing canonical
  entries added, non-canonical entries pruned.
- **permissions.allow**: only entries in `ALLOW_CONFLICTS` are removed; all
  other allow entries are left alone. Nothing is ever added to allow.

Non-guardrail keys in each settings file are left untouched.

### Targets

1. Every managed profile's `settings.json` under
   `~/.claudewheel/profiles/<name>/`
2. `~/.claudewheel/shared-settings.json` (fleet-wide baseline, including its
   `profileDefaults` block which seeds new profiles)

The `default` profile (`~/.claude`) is unconditionally excluded from
reconciliation.

### Hook script deployment

Wiring that references missing scripts would not be functional.
Reconciliation deploys any missing hook scripts from the in-memory model
to `~/.claudewheel/scripts/` before writing hook entries into settings
files. This is part of canonical: the scripts and the wiring are a unit.

### Compare-then-write

Every target is compared before writing. A file already at canonical state
is left byte-identical (no write happens). All writes go through
mode-preserving atomic write paths, so sensitive file permissions are not
accidentally loosened.

### CLI commands

Both commands perform the same reconciliation; they exist as separate entry
points for historical reasons and are interchangeable.

**`claudewheel reconcile-permissions`** accepts an optional `--profile <name>`
to scope to a single profile (shared-settings is left alone in that case).

**`claudewheel patch-profiles`** takes no flags of its own.

Both are declared *consequential*, because the reconciliation is exact and
prunes hand-added permission rules, hook entries and `disallowedTools` drift
with nothing backed up. So both confirm before writing, and both refuse
outright when stdin is not a terminal:

```
error: stdin is not interactive; pass --approve-consequential to confirm
```

`--approve-consequential` is the way a script consents. `--dry-run` previews
the per-target diff and writes nothing; it is never gated, so it is always the
safe first invocation. (The old hand-rolled `--apply` flag is gone --
`--dry-run` is the framework's now and is the only mode flag.)

### Preflight auto-heal

The `reconcile-guardrails` preflight step (step 2) runs
`reconcile_workspace` with `dry_run=False` on every launch. This means
configuration drift is self-healed automatically before each session starts,
without requiring manual intervention. The step is non-fatal: if
reconciliation fails for any reason, the launch proceeds.


## Common problems and solutions

### Stale tokens

**Symptom**: `[WARN] token-expiry: expiring soon: profile-name (~5d)`

Tokens issued by `claude setup-token` expire after 365 days. When a token
is within 30 days of expiry, the health check warns.

**Fix**: Run `claude setup-token` in the affected profile's context to
re-authenticate and get a fresh token.

### Missing shared symlinks

**Symptom**: `[WARN] shared-symlinks: broken: myprofile/projects, myprofile/todos`

Each managed profile's shared directories should be symlinks into
`~/.claudewheel/shared/`. A real directory or missing symlink means the
profile is not sharing data with other profiles.

**Fix**: Delete the real directory (after backing up any data) and re-create
the symlink, or delete and recreate the profile with `claudewheel profile
create`.

### Permission drift

**Symptom**: `[WARN] canonical-drift: myprofile: permissions.deny: missing [...]`

The profile's permission arrays have diverged from the canonical guardrail
model. This happens when claudewheel is upgraded and the canonical model
gains new rules, or when something edits the profile's settings directly.

**Fix**: `claudewheel reconcile-permissions` (or `claudewheel patch-profiles`).
Use `--dry-run` first to preview what would change; the write itself confirms,
and needs `--approve-consequential` when there is no terminal.

### Disk usage (/tmp)

**Symptom**: `[WARN] tmpfs: 85% used (>80% threshold)` or
`[WARN] /tmp/claude: 1500 MB (>1 GB threshold)`

Claude Code scratchpad data under `/tmp` can accumulate. The scratchpad
cleanup preflight step offers to delete stale directories, but if it was
snoozed or the session was non-interactive, cleanup does not happen.

**Fix**: Manually delete stale directories under `/tmp/claude-$UID/`, or
launch an interactive session to trigger the scratchpad cleanup prompt.

### Auth shadow

**Symptom**: `[WARN] auth-shadow: shadowed: myprofile`

A session-scoped credential in `.credentials.json` is overriding the
long-lived token. This can cause auth to break when the session credential
expires.

**Fix**: `claudewheel profile fix-auth myprofile` to remove the shadowing
session credential.

### Relocated workspace (hook path drift)

**Symptom**: `[WARN] hook-path-drift: myprofile: /old/path/scripts/hook-block-unsafe-commands`

Hook command paths still reference the old `~/.claudewheel/` location after
a workspace move.

**Fix**: `claudewheel patch-profiles` rewrites all hook command paths to the
current scripts directory.

### Orphan profiles

**Symptom**: `[WARN] orphan-profiles: orphans: old-profile`

Leftover directories from profiles that were incompletely deleted or renamed.

**Fix**: Inspect and delete manually if they contain no useful data.

### File permission issues

**Symptom**: `[WARN] file-perms: myprofile/.credentials.json is 0o644`

Credential files should be mode `0600` (readable only by the owner).

**Fix**: `chmod 600 ~/.claudewheel/profiles/myprofile/.credentials.json` (or
the reported file path).
