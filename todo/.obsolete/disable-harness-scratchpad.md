# Disable the Claude Code harness scratchpad via claudewheel

## Context

Claude Code injects a "Scratchpad Directory" section into the system prompt of
every session, steering the model to write ALL temporary files into a
session-scoped directory under `/tmp/claude-<uid>/<munged-cwd>/<session-uuid>/scratchpad`.
The user's global rules say the opposite (avoid /tmp, keep files in real
directories, prefer writing remote-destined files directly to their
destination), but models follow the injected harness instruction by default —
it takes an explicit correction per session to override, and sessions keep
re-offending because the instruction is re-injected every time.

Since claudewheel owns session launch (profiles, settings.json, env,
CLAUDE_CONFIG_DIR), it is the right place to suppress or neutralize the
scratchpad feature for every profile at once.

## Problem

- The scratchpad instruction ("IMPORTANT: Always use this scratchpad directory
  ... instead of /tmp") directly contradicts the user's standing rules, and
  wins by recency/emphasis unless the user pushes back mid-session.
- Files written there are session-UUID-addressed, invisible to the user's
  normal workflow, not backed up, and vanish on /tmp cleanup.
- Scratchpad paths leak long UUID noise into commands and reports.

## Possible solutions

1. **Find the official off-switch.** Investigate whether current Claude Code
   exposes a setting (settings.json key, env var, or CLI flag) that disables
   the scratchpad section or relocates it. Check `claude config` surface,
   settings schema, release notes, and the injected prompt text across
   versions. If it exists: set it in `shared-settings.json` so every profile
   inherits it.
   - Pros: clean, supported, survives updates.
   - Cons: may not exist; needs re-verification per Claude Code release.
2. **Relocate rather than disable.** If the directory is configurable (e.g.,
   via TMPDIR or a dedicated env var claudewheel can set at launch), point it
   at a user-visible location (e.g. `~/.claudewheel/scratch/<profile>/`) so
   even when the model obeys the harness, files land somewhere inspectable
   and cleanable.
   - Pros: works even if there is no true off-switch; damage contained.
   - Cons: the injected prompt text may still show /tmp paths; model behavior
     unchanged, just redirected; still contradicts the "no scratchpads" rule.
3. **Counter-instruction injection.** claudewheel already manages shared
   settings and hooks; a UserPromptSubmit/SessionStart hook could inject a
   standing reminder ("scratchpad use is banned in this environment") into
   every session.
   - Pros: no dependency on upstream knobs; enforces the user rule verbatim.
   - Cons: prompt-vs-prompt arms race; consumes context; softest guarantee.
4. **Deny-rule enforcement.** Add a permissions deny pattern (or a PreToolUse
   hook) for Write/Edit/Bash operations targeting `/tmp/claude-*` paths, so
   scratchpad writes hard-fail with a message pointing at the rule.
   - Pros: hard constraint, not guidance — fits the tools-for-agents
     philosophy (agents ignore warnings; blocked operations they cannot).
   - Cons: needs careful pattern scope (must not break Claude Code internals
     that legitimately use the session dir, e.g. shell snapshots, task output
     files); hook layer must distinguish model-initiated writes from
     harness-internal ones.

Likely best shape: 1 if an off-switch exists, else 4 (hard block) + 2
(relocation as fallback). Investigate 1 first — do web research on current
Claude Code versions before building anything.

## Affected files

- `~/.claudewheel/shared-settings.json` (settings key and/or deny rules and/or
  hook registration)
- claudewheel launcher code that assembles env/settings at session start
- possibly a small hook script shipped by claudewheel

## Effort

Investigation: small (an hour of docs/schema/prompt-dump spelunking).
Implementation: small for options 1-3; medium for option 4 (pattern scoping +
testing against harness-internal writes).
