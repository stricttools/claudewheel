# Reversibility gaps: commands whose inverse is missing

## Context

strictcli is gaining declared reversibility support: a mutating command will
declare which command undoes it (verified at registration in both
directions), a command with no recovery will declare irreversible with a
mandatory reason, a warn-severity check will flag destructive commands
declaring neither, and after a real run the framework will print a paste-able
recovery command and emit a machine-readable recovery member in the JSON
result document. When this repo adopts that support, every gap below needs
either a built inverse or an honest irreversible declaration.

## Problems

1. **`purge-plugins` destroys unrecoverably while its sibling does not.** It
   removes plugin trees via a direct recursive delete (claudewheel/plugins.py,
   the rmtree call), while profile deletion routes through the archival tool
   precisely so the same class of operation is recoverable with one restore
   command (claudewheel/archiver.py says exactly this in its module
   docstring). Two destructive commands, one wired to the archiver and one
   not.
2. **`import` has no un-import.**
3. **`reset-options` destroys custom options and regenerates defaults,
   preserving nothing** — the prior state is neither archived nor emitted.

## Solutions

1. Route purge-plugins through the existing archiver delegation — it becomes
   recoverable for free and can declare its inverse honestly.
2/3. Either archive the prior state through the same delegation (making a
   restore possible), or emit the prior state in the result document before
   overwriting, or declare irreversible with a reason. Archiving is the most
   correct option and the machinery already exists in-repo.

## Affected files

- claudewheel/plugins.py (purge-plugins handler)
- claudewheel/archiver.py (the delegation seam)
- the import and reset-options handlers
- tests

## Effort

Small for purge-plugins (the delegation exists); small-medium for the other
two.
