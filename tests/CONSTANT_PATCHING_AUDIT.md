# Constant-patching audit

This is a live execution checklist for the larger refactor that migrates every
filesystem-touching test onto the shared `tests/wheelhelpers.py` sandbox base
class. Each line lists a test file, the module-level path constants it patches
(as `module.CONSTANT`, where the module is the one whose *by-value* copy of the
constant is rebound), and a checkbox to tick once that file has been migrated to
`SandboxHomeTestCase` / `patch_constants_across`.

Path constants were captured by value into ~20 consuming modules at import
time, so each consumer's copy had to be patched separately. Phases 1-5 moved
every consumer onto the workspace/store layer; phase 6 stripped the path
constants from `constants.py` entirely, so almost all the module-level
constant-patching listed below is now GONE (the modules no longer hold those
attributes). The only module that still exposes patchable path constants is
none — path resolution is fully workspace-driven.

- [x] test_app.py — state.STATE_FILE
- [x] test_cli.py — cli.CLAUDE_SYMLINK, cli.OPTIONS_FILE, cli.PROFILES_DIR, cli.SHARED_DIR, cli.VERSIONS_DIR, constants.PROFILES_DIR, constants.TOKENS_FILE, profile_info.PROFILES_DIR, profile_ops.PROFILES_DIR, profile_ops.TOKENS_FILE
- [x] test_deploy_hooks.py — cli.SCRIPTS_DIR
- [x] test_discover.py — discovery.PROFILES_DIR, discovery.SHARED_DIR, discovery.SKILLS_DIR, discovery.TOKENS_FILE
- [x] test_health.py — discovery.PROFILES_DIR, discovery.SHARED_DIR, discovery.SKILLS_DIR, discovery.TOKENS_FILE, health.OPTIONS_FILE, health.PROFILES_DIR, health.SCRIPTS_DIR, health.SHARED_SETTINGS_FILE, health.SKILLS_DIR, health.TOKENS_FILE, profile_info.PROFILES_DIR, profile_info.TOKENS_FILE
- [x] test_helpers_meta.py — (none; consumes SandboxHomeTestCase directly)
- [x] test_import.py — import_.SHARED_DIR
- [x] test_inode.py — health.INODES_FILE, state.INODES_FILE
- [x] test_install.py — install.VERSIONS_DIR
- [x] test_migrate.py — migrate.PROFILES_DIR
- [x] test_migration.py — config.CONFIG_DIR, config.CONFIG_FILE, config.HOOKS_DIR, config.OPTIONS_FILE, config.SCRIPTS_DIR, config.SEGMENTS_FILE, config.SHARED_SETTINGS_FILE, config.STATE_FILE, config.THEMES_DIR, profile_ops.OPTIONS_FILE, profile_ops.PROFILES_DIR, profile_ops.TOKENS_FILE, state.STATE_FILE
- [x] test_mv.py — mv.SHARED_DIR
- [x] test_patch_profiles.py — discovery.PROFILES_DIR, discovery.TOKENS_FILE, patch_profiles.SCRIPTS_DIR, patch_profiles.SHARED_SETTINGS_FILE
- [x] test_profile.py — profile.TOKENS_FILE (plus discovery.{PROFILES_DIR,TOKENS_FILE,SHARED_DIR,SKILLS_DIR} via SandboxHomeTestCase.patch_constants_across)
- [x] test_profile_info.py — discovery.SHARED_DIR, discovery.SKILLS_DIR, profile_info.OPTIONS_FILE, profile_info.PROFILES_DIR, profile_info.TOKENS_FILE
- [x] test_profile_ops.py — discovery.SHARED_DIR, discovery.SKILLS_DIR, profile_ops.OPTIONS_FILE, profile_ops.TOKENS_FILE, state.STATE_FILE
- [x] test_reconcile.py — discovery.PROFILES_DIR, discovery.TOKENS_FILE, reconcile.SHARED_SETTINGS_FILE
- [x] test_shared_settings.py — discovery.PROFILES_DIR, health.PROFILES_DIR, health.SHARED_SETTINGS_FILE
- [x] test_state.py — config.STATE_FILE, state.INODES_FILE, state.STATE_FILE
- [x] test_stats.py — stats.SHARED_DIR
- [x] test_theme_auto.py — config.* (CONFIG_DIR, CONFIG_FILE, SEGMENTS_FILE, OPTIONS_FILE, STATE_FILE, THEMES_DIR, HOOKS_DIR, SCRIPTS_DIR, SHARED_SETTINGS_FILE)
- [x] test_tokens.py — tokens.TOKENS_FILE
- [x] test_wizard.py — config.CONFIG_DIR, config.CONFIG_FILE, config.HOOKS_DIR, config.OPTIONS_FILE, config.SCRIPTS_DIR, config.SEGMENTS_FILE, config.SHARED_DIR, config.SHARED_SETTINGS_FILE, config.STATE_FILE, config.THEMES_DIR, profile_ops.PROFILES_DIR, state.STATE_FILE, tokens.TOKENS_FILE, wizard.CLAUDE_SYMLINK, wizard.PROFILES_DIR, wizard.SCRIPTS_DIR, wizard.SHARED_DIR, wizard.SHARED_SETTINGS_FILE, wizard.SKILLS_DIR
- [x] test_workspace_contracts.py — discovery.PROFILES_DIR, discovery.TOKENS_FILE, profile.TOKENS_FILE

## Exemption list

The migration to workspace-driven paths is complete: no test patches a
module-level path constant anymore, and `constants.py` is ANSI/terminal-only.
The following home-directory / path literals remain in production code and are
LEGITIMATE (they are the defaults or detection roots that define where the
workspace/locator/browser layers look, not import-time captured constants):

- `workspace.py` — `Workspace.open`/`Workspace.default` defaults (`~/.claudewheel`
  root and `~/.claude` claude_dir; the sole reader of `CLAUDEWHEEL_CONFIG_DIR`).
- `binaries.py` — `BinaryLocator.default` defaults (`~/.local/share/claude/versions`
  and `~/.local/bin/claude`).
- `discovery.py` — browser-detection directories (`_FLATPAK_EXPORT_DIRS`,
  `_SNAP_BIN_DIR`).
- `segment.py` — tilde-relativization and directory-scan base dirs.
