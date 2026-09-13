# An unattended launch mode that can never block on a permission dialog

## Context

claudewheel selects a profile, model, directory and permission mode before
starting a Claude Code session. Some sessions are meant to run unattended for
hours ("do not stop for any reason"). Claude Code keeps a tool-permission
prompt ("Do you want to proceed?") open until a human answers it: there is no
timeout on that dialog and no setting that auto-answers it. The AFK timeout
that exists (`CLAUDE_AFK_TIMEOUT_MS`, `CLAUDE_AFK_COUNTDOWN_MS`) applies only
to `AskUserQuestion` dialogs, not to permission prompts.

## Problem

An unattended session ran in bypass-permissions mode and still stopped for
six hours: a shell command matched an `ask` rule in the profile's
`permissions` block, and an explicit `ask` rule prompts even in bypass mode
(deny rules block in every mode; allow rules have no effect in bypass mode;
ask rules still prompt). One matched rule turned an unattended run into a
dialog nobody was there to answer, and the work behind it sat idle until the
operator came back.

The launcher currently has no way to say "this session must never wait for a
human": the operator has to know which of the profile's rules can prompt and
hope none of them fires.

## Solutions

1. **An "unattended" toggle in the launcher that starts the session with
   `--permission-mode dontAsk`.** Recommended. `dontAsk` auto-denies every
   tool call that would otherwise prompt (ask-rule matches and
   `AskUserQuestion` included), the session never waits, and the denial with
   its reason is shown to the model so it adapts instead of retrying. It
   composes with the profile's `permissions.allow` list, which is what the
   session can still do freely. Cost: a session in this mode cannot ask
   anything, so the toggle must be an explicit choice at launch, never a
   default, and the launcher should print what it implies.

2. **A `PermissionRequest` hook in the shared settings, enabled per profile,
   that answers every request that would prompt with `deny` and a reason
   such as "session is unattended; no human is available to approve".**
   Same effect as 1 through the hook layer, with a reason the operator
   controls, and it works with any permission mode. Cost: one more hook to
   maintain; a hook that times out does not block, so it must be fast, and
   it duplicates what `dontAsk` already does in the harness.

3. **Rewrite the profile's `ask` rules into `deny` rules for unattended
   profiles.** Keeps the current mode; a denied command is refused instead of
   prompted. Cost: `deny` is stricter than the operator may want for
   attended use of the same profile, so it forces a profile split, and it
   does nothing about `AskUserQuestion`.

Whichever is chosen, the launcher's profile summary should show, before
launch, whether the session can block on a human.

## Affected files

- the launcher's permission-mode selection and the command line it builds
  for `claude` (wherever `--permission-mode` and the profile's
  `CLAUDE_CONFIG_DIR` are set)
- `~/.claudewheel/shared-settings.json` and per-profile `settings.json`
  (`permissions.ask`, `hooks`) if option 2 or 3 is taken
- the profile summary shown before launch
- README / docs describing the launch options

## Effort

Small for option 1 (one launcher option plus one flag on the command line and
a line in the summary). Medium for option 2 (hook script, settings wiring,
per-profile switch).
