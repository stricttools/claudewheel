# Retire the ask tier from profile permissions

## Context

Each profile's `settings.json` carries a `permissions` object with `allow`,
`deny`, and `ask` arrays, and `shared-settings.json` contributes shared
entries. Entries in `ask` make Claude Code interrupt the session with an
interactive permission prompt every time a matching tool call occurs.

## Problem

The ask tier produces interactive prompts that interrupt work and add no
safety over the alternatives: the user either always approves a given
command class (in which case it belongs in `allow`) or never wants it run
as-is (in which case it belongs in `deny`, ideally with a hook that tells
the agent what to do instead -- the pattern already used by the rm-blocking
hook, which denies `rm` and names `saferm` as the replacement). A prompt
that is always answered the same way is pure interruption.

## Desired end state

No `ask` entries anywhere: every current ask-tier entry is reclassified as
either `allow` (let it through) or `deny` plus a hook that presents the
correct alternative in its denial message. No permission prompt should ever
fire for a pattern the profiles already know the answer to.

## Solutions

1. **Manual audit and reclassification.** Go through every profile's `ask`
   array (and any shared ask entries), decide allow vs deny-with-alternative
   per entry, write hook denial messages for the deny side, delete the empty
   `ask` arrays.
   - Pros: deliberate per-entry decisions; hooks end up documenting the
     alternatives.
   - Cons: manual; needs re-doing if future edits reintroduce ask entries.
2. **Same, plus a guard.** After the reclassification, add a check to
   claudewheel's own health/doctor surface that errors when any profile
   carries a non-empty `ask` array, so the tier stays retired.
   - Pros: structurally prevents regression instead of relying on
     discipline; matches the hard-error-over-warning philosophy.
   - Cons: slightly more work; the check needs a home in the existing
     health-check machinery.

Option 2 is the more correct solution: without the guard, the tier creeps
back.

## Affected files

- `~/.claudewheel/profiles/<name>/settings.json` (every profile's
  `permissions.ask`)
- `~/.claudewheel/shared-settings.json` (shared permission entries, hooks)
- Hook scripts referenced by the deny entries (denial messages naming the
  alternative)
- claudewheel's health-check code, if option 2 is taken

## Effort

Small: one audit pass over the ask arrays plus hook message wording; the
optional guard check is a few lines in the existing health machinery.
