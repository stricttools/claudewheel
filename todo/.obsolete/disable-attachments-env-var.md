# Set CLAUDE_CODE_DISABLE_ATTACHMENTS=1 for launched sessions

## Context

Claude Code injects per-turn "attachments" into the model's context: file-
modified notices, todo reminders, @-mention expansions, plan-mode
instructions, and ~35 other types. One of them (`changed_files`) re-reads any
file the session previously read or wrote whenever its mtime changes and
injects a diff of the new content — up to 16 KB per turn. This leaked the
content of a file the user had deliberately edited outside the session and
did not want the model to see. There is no targeted off-switch; the issue
requesting one upstream was closed as not-planned.

The owner has decided: all claudewheel-launched sessions, all profiles,
should run with `CLAUDE_CODE_DISABLE_ATTACHMENTS=1`. This todo asks
claudewheel to implement that properly (as a first-class part of settings
composition), rather than someone hand-editing config ad hoc.

## Verified findings (Claude Code 2.1.220/2.1.226, binary inspection + probes)

- `CLAUDE_CODE_DISABLE_ATTACHMENTS=1` short-circuits the entire attachment
  collector. It is all-or-nothing; every `CLAUDE_CODE_*` variable in the
  binary was enumerated and no narrower knob exists.
- **Survives** (verified): top-level CLAUDE.md loading (user + project — it
  rides the system prompt, a different path), MCP server connections and
  their tools, synchronous hook output (UserPromptSubmit/PreToolUse/
  PostToolUse `command` hooks — the shared-settings hooks are unaffected),
  messages queued while a turn runs, and the subagent-type roster. The
  Edit tool's independent stale-write refusal (file modified since read)
  also survives, so the safety property behind the notices is kept.
- **Lost, accepted by the owner**: subdirectory (nested) CLAUDE.md lazy
  discovery; MCP server `instructions` text (the attachment is its only
  delivery path); plan mode becomes effectively unusable (the model never
  learns it is in plan mode — writes are still blocked by the permission
  layer, but the model has no instructions); auto mode likewise; the
  `ultrathink`/`ultracode` keywords; SendMessage to a *running* subagent
  (spawning fresh agents is unaffected); IDE/LSP diagnostics; date-change,
  token/budget, and memory-update notices; skill self-discovery.
- Quirk: the post-compaction re-injection path is NOT guarded by this env
  var, so plan-mode/MCP-instruction blocks reappear once after each
  auto-compaction.

## Problem

claudewheel needs a sanctioned way to set this env var for every launched
session. Claude Code honors a top-level `"env": {...}` object in
settings.json (applied to every session using that settings file), so the
likely mechanism is settings composition — but whether an `env` block in
`shared-settings.json` actually propagates to the effective per-profile
settings depends on claudewheel's merge semantics, which this todo's author
has not read.

## Solutions

1. **`env` block in shared-settings.json** (if the merge already propagates
   arbitrary keys): add `"CLAUDE_CODE_DISABLE_ATTACHMENTS": "1"`.
   - Pros: zero code, centralized, all profiles inherit, live-reload may
     apply it to running sessions.
   - Cons: only works if the merge is key-complete; invisible in the TUI;
     no per-session opt-out (a session that wants plan mode is stuck).
2. **First-class claudewheel setting** (e.g. under `profileDefaults` or a
   dedicated key) that claudewheel exports into the launch environment.
   - Pros: explicit, documented, independent of settings-merge semantics;
     can be overridden per profile.
   - Cons: small code change; another config key to maintain.
3. **TUI launch toggle** (alongside profile/model/permissions selection),
   defaulting to on.
   - Pros: per-session control — the one real casualty (plan mode) can be
     recovered by flipping the toggle off for a planning session.
   - Cons: most work; one more prompt in the launch flow.

The most correct solution regardless of effort: 2 + 3 combined — a
config-backed default (on, all profiles) with a per-launch override in the
TUI, since plan mode is the only loss that plausibly ever needs an opt-out.

## Affected files

Wherever claudewheel composes shared settings into profile settings and
wherever the TUI builds the launch environment/flow. (Author has not read
the source; discover during implementation.)

## Effort

Small. Option 1 is a config edit; option 2 is minor plumbing; option 3 adds
a TUI element. Combined 2+3: a few hours including tests.
