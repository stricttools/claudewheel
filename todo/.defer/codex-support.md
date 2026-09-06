# Codex support: launch OpenAI Codex sessions, wire its hook system

## Context

claudewheel is a TUI launcher that selects a profile, model, directory, and
permissions before starting a Claude Code session. Profile data lives under
`~/.claudewheel/` (per-profile dirs with `settings.json` and credentials,
`shared-settings.json` for hooks and disallowedTools, `tokens.json` for OAuth
tokens, a shared data store symlinked into each profile). At launch,
`CLAUDE_CONFIG_DIR` points the session at the selected profile directory.

OpenAI Codex (the open-source Rust CLI, https://github.com/openai/codex,
Apache-2.0) is a directly analogous harness with directly analogous
configuration surfaces. claudewheel should be able to launch Codex sessions
with the same profile/permissions/directory selection it provides for Claude
Code — including, especially, driving Codex's hook system the way claudewheel
drives Claude Code hooks via `shared-settings.json`.

## Codex configuration system (research summary, verified 2026-09-05)

Primary docs: https://learn.chatgpt.com/docs/config-file/config-basic,
.../config-advanced, .../config-reference (append `.md` for raw markdown).

Config is TOML, layered (highest precedence first):

1. CLI flags and repeatable `-c`/`--config key=value` dotted-path overrides
   (values parsed as TOML, e.g.
   `codex -c sandbox_workspace_write.network_access=true`)
2. Project `.codex/config.toml` — every one from repo root down to cwd loads,
   closest wins; loaded only for trusted projects
   (`projects.<path>.trust_level = "trusted"`); certain keys are banned at
   project level (`model_provider(s)`, provider base URLs, `notify`,
   `profile(s)`, `otel`, ...) so a repo cannot redirect traffic or run
   arbitrary commands
3. Profile selected by `--profile <name>` — a dedicated
   `~/.codex/<name>.config.toml` overlay holding only the differing keys,
   written as top-level keys. The older inline `[profiles.<name>]` tables in
   `config.toml` were removed in CLI 0.134.0.
4. User `~/.codex/config.toml`
5. System `/etc/codex/config.toml`, then built-in defaults

Above all of these sits managed `requirements.toml`
(`/etc/codex/requirements.toml`, or MDM payload on macOS) — admin
constraints users cannot override: allowlists for permission profiles,
approval policies, sandbox modes, MCP server identities, managed hooks, etc.
(https://learn.chatgpt.com/docs/enterprise/managed-configuration)

Key facts for claudewheel:

- **`CODEX_HOME`** (env var) relocates the entire user layer — config,
  profile files, hooks, history, and credentials. This is the direct analog
  of `CLAUDE_CONFIG_DIR` and the natural hook for claudewheel's per-profile
  directory scheme. (Credential file name under `CODEX_HOME` to verify
  during implementation — believed `auth.json`.)
- Useful launch flags: `--profile <name>`, `-c` overrides,
  `--strict-config` (error on unrecognized keys), `--ignore-user-config`,
  `--cd`, `--sandbox <mode>`, `--ask-for-approval <policy>`, `-m <model>`.
- Permissions live in config as `approval_policy`
  (`untrusted` / `on-request` / `never`) plus either `sandbox_mode`
  (`read-only` / `workspace-write` / `danger-full-access` with a
  `[sandbox_workspace_write]` table) or the newer named permission profiles
  (`default_permissions` + `[permissions.<name>]`) — the two families are
  mutually exclusive in one config
  (https://learn.chatgpt.com/docs/permissions).

## Codex hook system (research summary, verified 2026-09-05)

Docs: https://learn.chatgpt.com/docs/hooks — stable, behind `features.hooks`.

- Events: `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`,
  `PostCompact`, `UserPromptSubmit`, `Stop`, `Interrupt`, `SessionStart`,
  `SessionEnd`, `SubagentStart`, `SubagentStop`.
- Configured in `hooks.json` or inline `[hooks]` tables in `config.toml`;
  regex matchers (tool name for tool events, `manual|auto` for compaction,
  start source for `SessionStart`).
- Handlers are commands only (plus a `commandWindows` variant); options:
  `timeout` (default 600 s; `SessionEnd`/`Interrupt` capped at 3 s), `async`
  (background, cannot block), `statusMessage`, output-size threshold.
- Protocol: JSON on stdin (session id, cwd, event name, tool name/input/
  response); JSON on stdout can deny or rewrite tool input (`PreToolUse`
  `updatedInput`), allow/deny permission requests, block and inject
  model-visible context (`PostToolUse`), or force continuation
  (`Stop`/`SubagentStop`). Exit code 2 = blocking decision with stderr as
  the reason.
- Trust: non-managed hooks require explicit one-time approval via `/hooks`
  before first run. Managed hooks — deployed via `requirements.toml`
  (`[hooks] managed_dir`) or MDM — bypass the trust prompt, and
  `allow_managed_hooks_only = true` disables user hooks entirely. For
  claudewheel-provisioned hooks, decide whether to pre-trust (write the
  trust state), accept the one-time prompt, or use the managed mechanism.

## Problem

claudewheel today can only launch Claude Code. Users who also run Codex get
no profile isolation, no centralized credential handling, no shared hook
provisioning, and no launcher-driven permission selection for it.

## Proposed shape

1. A per-profile Codex home under the claudewheel layout (e.g.
   `~/.claudewheel/profiles/<name>/codex/`), exported as `CODEX_HOME` at
   launch — mirrors the existing `CLAUDE_CONFIG_DIR` design, isolates
   credentials and history per profile, and keeps the centralized-layout
   promise.
2. claudewheel renders the profile's permission selection into the launched
   config: `approval_policy` + sandbox mode / permission profile, via the
   generated `config.toml` (or `-c` overrides for one-shot choices).
3. Shared hooks: the equivalent of `shared-settings.json` hooks, rendered
   into each profile's `hooks.json` (or inline `[hooks]`). Event mapping
   from the Claude Code hook events claudewheel uses is direct for the
   common ones (`SessionStart`, `UserPromptSubmit`, `PreToolUse`,
   `PostToolUse`, `Stop`); anything relying on Claude-Code-only events needs
   a per-event decision. Handle the trust question deliberately (see above).
4. TUI: a harness selector (Claude Code / Codex) alongside the existing
   profile/model/directory/permissions selection; model list and permission
   vocabulary switch with the harness.

### Alternative considered: native `--profile` files instead of `CODEX_HOME`

Lighter (one shared `~/.codex/` with per-claudewheel-profile overlay files),
but shares credentials, history, and hooks across all profiles — defeats the
isolation that is claudewheel's point. Rejected unless a shared-auth mode is
explicitly wanted later.

## Affected areas

- Profile discovery/scan (currently keyed on `.credentials.json` presence)
- Launch command construction and environment setup
- Shared-settings rendering (hooks, disallowed tools -> Codex equivalents)
- Token/credential management (`tokens.json` currently assumes Claude OAuth)
- TUI screens for harness/model/permission selection

## Effort estimate

Medium-large — multiple sessions. The launch-with-`CODEX_HOME` core is
small; hook rendering with the trust story, permission-vocabulary mapping,
and credential handling are each real design-and-test work.
