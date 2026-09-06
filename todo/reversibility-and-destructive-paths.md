# Reversibility: destructive paths without an inverse

## Context

strictcli is gaining declared reversibility (a mutating command declares its inverse or
declares itself irreversible with a reason; a warn check flags destructive commands
declaring neither). When claudewheel adopts that, every path below needs a built
inverse or an honest irreversible declaration. The archiver seam
(`claudewheel/archiver.py`) already exists: `archive(path, *, description)` delegates to
saferm with feature negotiation, works on any path, and is the reason `profile delete`
is recoverable — and refuses to run at all without saferm.

## Inventory (verified 2026-09-06; each with its verdict)

- **purge-plugins** (`claudewheel/plugins.py`): direct recursive delete, no archiver.
  Routing through the archiver is mechanically clean but needs: a signature change (the
  archiver needs the workspace root plugins.py never sees), generalizing the refusal
  prose (it is hardcoded to profile-deletion language), declaring the archiver grant on
  the command, and a POLICY ruling — saferm becomes a precondition (the command then
  refuses in scripts without saferm, same stance as profile delete), and possibly
  joining the consequential set. Per-plugin state under the plugin data dirs is the
  part that is otherwise unrecoverable.
- **reset-options** (`claudewheel/cli.py` `_do_reset_options`): deletes options.json
  outright; the user's pinned values are the unrecoverable part. Options: archive via
  the seam, print the prior content before deleting, or declare irreversible. Note the
  version-stamp interaction (deleting options.json while the global schema counter
  stays at latest means regeneration skips migrations — see the migration-engine todo).
- **import** (`claudewheel/import_.py`): additive-only, never overwrites (collisions
  refuse without `--reid`; with it, colliding sessions get fresh ids) — so nothing to
  archive; what is missing is the OTHER direction: no manifest of what was written, so
  no un-import, and re-idd sessions cannot be matched to their source afterwards.
  Ruling: add a written manifest (new persistent state someone must own) or declare
  irreversible.
- **profile fix-auth** (`claudewheel/profile_ops.py`): deletes `.credentials.json` when
  the OAuth blob was its only key — a live credential destroyed with no archive and no
  prompt. This contradicts the project's own reasoning for making profile delete refuse
  without saferm. Ruling: archival, consequential status, both, or accept.
- **`ProfileData.remove_token`** (`claudewheel/profile_data.py`): production-dead —
  test-only callers. Wire it to a `profile remove-token` command (then it inherits the
  fix-auth archival question) or delete it.
- **stats** (`claudewheel/stats.py`): silently recursive-deletes the legacy sentinels
  directory inside a command whose help promises reporting, logging only afterwards.
  Ruling: split the cleanup out, report-before-remove, or accept.
- **uninstall** (`claudewheel/cli.py`): removes a version binary; the correct inverse
  already exists (`claudewheel install <version>` re-downloads checksum-verified), so
  no archive — but the in-use guard consults only the symlink, so a version a LIVE
  session is executing can be uninstalled. Minor guard improvement available.
- **session-registry prune** (`claudewheel/session_registry.py` via the sessions
  overview): deletes records only for provably-dead PIDs after re-reading the file —
  correctly needs no inverse; note the overview prunes ALL dead rows on one keypress
  with no confirmation (UX question, see the confirm-key todo).
- **The exact reconcile** (`patch-profiles` / `reconcile-permissions`): prunes
  hand-added rules and hook entries with nothing retained. Consent is handled (both are
  consequential); recovery is not. The retention ruling lives in the settings-authority
  todo; listed here because it is the largest instance of the class.
- **preflight temp-tree cleanup** (`claudewheel/preflight.py` + `claudewheel/
  scratchpad.py`): deletes stale per-project temp dirs under /tmp — archiving would
  defeat the purpose; correctly irreversible.
- NOT in this class, verified: `_discard_partial_profile_dir` (unwinds a failed create,
  documented as holding no user data) and the profile-rename breadcrumbs.

## Affected files

`claudewheel/plugins.py`, `claudewheel/archiver.py` (prose generalization),
`claudewheel/cli.py`, `claudewheel/import_.py`, `claudewheel/profile_ops.py`,
`claudewheel/profile_data.py`, `claudewheel/stats.py`, tests throughout.

## Supersedes

reversibility-gaps-missing-inverse-commands.md.

## Effort

Small per item once ruled; purge-plugins is the largest (prose + grant + policy).
