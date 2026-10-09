# Speculative: one Claude Code config directory instead of per-profile directories joined by symlinks

Speculative: worth investigating, not decided.

## Context

Claude Code uses a single directory per process (`CLAUDE_CONFIG_DIR`) for two different kinds of data:

- per-account data: credentials, `settings.json`, `.claude.json`;
- session data: transcripts under `projects/`, plus `todos/`, `file-history/`, `session-env/`, `tasks/`, and `paste-cache/`.

Claude Code has no setting that puts session data anywhere other than its config directory.

claudewheel profiles need different accounts and settings, so each profile is its own config directory under `~/.claudewheel/profiles/<name>/`. Sessions must be resumable from any profile, so session data must be one shared copy; claudewheel therefore makes each profile's session-data subdirectories symlinks into `~/.claudewheel/shared/`.

## Problem

The symlinks make every path look profile-specific when it is not: a transcript's path read through one profile (`~/.claudewheel/profiles/emergency/projects/-home-m-Projects/<id>.jsonl`) names a profile that has nothing to do with where the file lives (`~/.claudewheel/shared/projects/-home-m-Projects/<id>.jsonl`). Agents and people reading such a path conclude that transcripts are separated by profile. The links also have to be created, repaired, and checked (`health`, profile create, rename, and delete, import, move-session, mv all deal with them).

## Options

1. **One config directory for every profile.** All profiles share a single `CLAUDE_CONFIG_DIR`; what differs per profile is passed at launch: the OAuth token through `CLAUDE_CODE_OAUTH_TOKEN` (already done), the plan-tier variables, and settings through `--settings <file>` per profile.
   - Pros: no store symlinks at all; every path is the real path; profile create, rename, and delete stop touching session data.
   - Cons: whatever is per profile and cannot be passed at launch breaks this. Unknown and to be checked first: whether permissions and hooks fully work through `--settings`, and what `.claude.json` holds that must differ per account (onboarding state, account metadata, per-project trust and MCP state). If `.claude.json` must differ, this option fails unless that state can also be separated.
2. **Ask Anthropic for a separate session-data directory setting** (for example a data-directory variable distinct from `CLAUDE_CONFIG_DIR`), then point every profile's session data at the shared store without links.
   - Pros: the cleanest result; per-account and session data separated by Claude Code itself.
   - Cons: depends on upstream; nothing to build here until it exists. Would be filed as an upstream request.
3. **Keep the links, hide them.** Every claudewheel command and message reports the shared path, never the path through a profile; `health` keeps checking the links.
   - Pros: no behavior change, small.
   - Cons: the links remain; anything reading paths outside claudewheel (Claude Code itself, other tools, agents) still sees profile-looking paths.

## Investigation needed before choosing

- List every file and key in a profile directory, and for each, whether it can differ between profiles if they share one directory (launch-time variable, `--settings`, or not at all).
- Test option 1 with a scratch home: two profiles, one directory, different tokens and settings; check auth, permissions, hooks, resume across profiles, and `.claude.json` behavior.

## Affected areas

The profile store (create, rename, delete, the shared-store links), the launch environment and arguments, `health`, `import`, `move-session`, `mv`, `migrate`, `upgrade-workspace` (a migration of existing profiles), and the docs describing the layout.

## Effort

Investigation: small to medium. Option 1 if it holds: medium (launch changes plus a one-time migration merging profile directories). Option 2: an upstream request, then small. Option 3: small.
