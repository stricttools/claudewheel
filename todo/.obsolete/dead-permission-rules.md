# Dead permission rules: patterns referencing renamed upstream commands match nothing

Filed 2026-08-03.

## Context

Profile and shared settings carry Bash permission patterns keyed on exact command prefixes. When an upstream CLI renames a command, every pattern built on the old name silently stops matching — the rule remains in the file, looks like protection, and does nothing.

## Problem (concrete instance, verified 2026-08-03)

`~/.claudewheel/shared-settings.json:94` and at least two profile settings (`profiles/hn/settings.json:32`, `profiles/emergency/settings.json:494`, plus others) contain deny rules of the form `"Bash(safegit rewrite-author:*)"`. That command was renamed upstream (`rewrite-author` became the `author rewrite` subcommand; the upstream changelog documents the rename). The deny rules therefore match no possible invocation — a silently dead guardrail. This is the highest-severity form of stale-invocation drift: not a broken script (which fails loudly) but a security control that no longer fires.

## Work

1. Update the known-dead patterns to the current command form across shared settings and every profile. Do not keep the old-form rules alongside (the old command no longer exists; a rule for it is dead weight).
2. Audit ALL Bash permission patterns in `shared-settings.json` and `profiles/*/settings.json` against the current surfaces of the fleet CLIs they reference — every pattern that names a subcommand is exposed to this failure mode.
3. Consider a small check script (`scripts/`-style) that extracts command-prefix patterns from all settings files and validates each against the named CLI's `--dump-schema` output, for repeat use after upstream renames. Planned fleet surface-scan tooling may subsume this later; the one-off audit should not wait for it.

## Affected files

`~/.claudewheel/shared-settings.json`, `~/.claudewheel/profiles/*/settings.json`, possibly a new validation script.

## Effort

S for the fix + audit; S for the optional check script.
