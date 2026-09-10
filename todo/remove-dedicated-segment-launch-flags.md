# Remove the dedicated per-segment launch flags in favor of `--set`

## Context

The `launch` command accepts two ways of presetting a segment so the TUI
skips it. A handful of built-in segments (profile, github, model,
directory, mcp, permissions) have a dedicated flag each, declared in the
`segments` flag set in `claudewheel/cli.py`. Every segment, including
those and any segment the dedicated set does not cover (version is the
visible one today), is also reachable through the generic repeatable
`--set` / `-s KEY=VALUE` flag. The handler merges both sources into one
`segment_overrides` dict and rejects a segment named through both.

`docs/config.md` documents the two forms side by side ("Per-segment
flags" and "The `-s` / `--set` flag"), and `docs/cli-launch.md` lists
the flag set.

## Problem

The two forms are redundant, and the split between them is arbitrary:

- A caller presetting a full launch mixes spellings, e.g.
  `--permissions bypass -s version=2.1.263`, because version has no
  dedicated flag. There is no rule a reader can derive for which segments
  get a flag and which do not.
- Version cannot simply be given a dedicated flag under the obvious name,
  because `--version` already prints claudewheel's own version. Any
  dedicated spelling for it would be a second convention on top of the
  first.
- Every dedicated flag is a second code path (its own `Flag` entry, its
  own slot in `flag_values`, its own entry in the duplicate-rejection
  logic, its own docs row) that does exactly what `-s KEY=VALUE` already
  does.
- The segment set is configurable, so the generic form is the one that
  is actually complete; the dedicated flags can only ever cover a fixed
  subset.

## Solutions

### A. Delete the dedicated flags; `--set` is the only preset form (recommended)

Remove the `segments` flag set's dedicated entries, the `flag_values`
merge, and the dedicated-vs-set duplicate check (only `-s` duplicates
remain to reject). Update the docs so `-s KEY=VALUE` is the single
documented way to preset any segment, with the built-in segment keys
listed once.

- Pros: one form, uniform for every segment including version, no
  naming problem for version, less code, docs describe one thing.
- Cons: command lines get slightly longer (`-s profile=work` instead of
  `--profile work`); any script or shell alias on this machine that
  passes the dedicated flags must be updated. This is a pre-stable
  project, so no compatibility shim: delete and update callers.

### B. Keep the dedicated flags, add one for version under a non-conflicting name

Add e.g. `--cc-version` so every built-in segment has a dedicated flag.

- Pros: the shortest spelling for the common case.
- Cons: keeps both forms and their duplicate-check machinery, introduces
  an irregular flag name for one segment, and still leaves any
  user-configured segment reachable only through `-s`.

### C. Keep both forms as they are

- Cons: the asymmetry stays, and the redundancy stays.

## Affected files

- `claudewheel/cli.py`: the `segments` `FlagSet` (the `_segment_flag_set`
  definition), the `flag_values` merge in the launch handler, and the
  duplicate rejection that names the source flag.
- `docs/config.md`: the "Per-segment flags" and "The `-s` / `--set`
  flag" sections and the TUI-bypass examples.
- `docs/cli-launch.md`: generated flag table (regenerate).
- `docs/_CLAUDE.md` if it mentions the dedicated flags (regenerate the
  root file afterwards).
- Tests covering the dedicated flags and the dedicated-vs-set duplicate
  error.

## Effort

Small. One pass over the launch handler and flag set, one docs pass, and
a test update. Under an hour.
