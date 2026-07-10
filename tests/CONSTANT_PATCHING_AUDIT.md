# Constant-patching audit

This is a live execution checklist for the larger refactor that migrates every
filesystem-touching test onto the shared `tests/wheelhelpers.py` sandbox base
class. Each line lists a test file, the module-level path constants it patches
(as `module.CONSTANT`, where the module is the one whose *by-value* copy of the
constant is rebound), and a checkbox to tick once that file has been migrated to
`SandboxHomeTestCase` / `patch_constants_across`.

Path constants are captured by value into ~20 consuming modules at import time,
so each consumer's copy must be patched separately. The shared helper
(`patch_config_constants`) already covers the `config`-module set used by the
migration and theme-auto tests.

- [ ] test_app.py — state.STATE_FILE
- [ ] test_cli.py — cli.CLAUDE_SYMLINK, cli.OPTIONS_FILE, cli.PROFILES_DIR, cli.SHARED_DIR, cli.VERSIONS_DIR, constants.PROFILES_DIR, constants.TOKENS_FILE, profile_info.PROFILES_DIR, profile_ops.PROFILES_DIR, profile_ops.TOKENS_FILE
- [ ] test_deploy_hooks.py — cli.SCRIPTS_DIR
- [ ] test_discover.py — discovery.PROFILES_DIR, discovery.SHARED_DIR, discovery.SKILLS_DIR, discovery.TOKENS_FILE
- [ ] test_health.py — discovery.PROFILES_DIR, discovery.SHARED_DIR, discovery.SKILLS_DIR, discovery.TOKENS_FILE, health.OPTIONS_FILE, health.PROFILES_DIR, health.SCRIPTS_DIR, health.SHARED_SETTINGS_FILE, health.SKILLS_DIR, health.TOKENS_FILE, profile_info.PROFILES_DIR, profile_info.TOKENS_FILE
- [ ] test_helpers_meta.py — (none; consumes SandboxHomeTestCase directly)
- [ ] test_import.py — import_.SHARED_DIR
- [ ] test_inode.py — health.INODES_FILE, state.INODES_FILE
- [ ] test_install.py — install.VERSIONS_DIR
- [ ] test_migrate.py — migrate.PROFILES_DIR
- [ ] test_migration.py — config.CONFIG_DIR, config.CONFIG_FILE, config.HOOKS_DIR, config.OPTIONS_FILE, config.SCRIPTS_DIR, config.SEGMENTS_FILE, config.SHARED_SETTINGS_FILE, config.STATE_FILE, config.THEMES_DIR, profile_ops.OPTIONS_FILE, profile_ops.PROFILES_DIR, profile_ops.TOKENS_FILE, state.STATE_FILE
- [ ] test_mv.py — mv.SHARED_DIR
- [ ] test_patch_profiles.py — discovery.PROFILES_DIR, discovery.TOKENS_FILE, patch_profiles.SCRIPTS_DIR, patch_profiles.SHARED_SETTINGS_FILE
- [ ] test_profile.py — profile.TOKENS_FILE (plus discovery.{PROFILES_DIR,TOKENS_FILE,SHARED_DIR,SKILLS_DIR} via SandboxHomeTestCase.patch_constants_across)
- [ ] test_profile_info.py — discovery.SHARED_DIR, discovery.SKILLS_DIR, profile_info.OPTIONS_FILE, profile_info.PROFILES_DIR, profile_info.TOKENS_FILE
- [ ] test_profile_ops.py — discovery.SHARED_DIR, discovery.SKILLS_DIR, profile_ops.OPTIONS_FILE, profile_ops.TOKENS_FILE, state.STATE_FILE
- [ ] test_reconcile.py — discovery.PROFILES_DIR, discovery.TOKENS_FILE, reconcile.SHARED_SETTINGS_FILE
- [ ] test_shared_settings.py — discovery.PROFILES_DIR, health.PROFILES_DIR, health.SHARED_SETTINGS_FILE
- [ ] test_state.py — config.STATE_FILE, state.INODES_FILE, state.STATE_FILE
- [ ] test_stats.py — stats.SHARED_DIR
- [ ] test_theme_auto.py — config.* via patch_config_constants (CONFIG_DIR, CONFIG_FILE, SEGMENTS_FILE, OPTIONS_FILE, STATE_FILE, THEMES_DIR, HOOKS_DIR, SCRIPTS_DIR, SHARED_SETTINGS_FILE)
- [ ] test_tokens.py — tokens.TOKENS_FILE
- [ ] test_wizard.py — config.CONFIG_DIR, config.CONFIG_FILE, config.HOOKS_DIR, config.OPTIONS_FILE, config.SCRIPTS_DIR, config.SEGMENTS_FILE, config.SHARED_DIR, config.SHARED_SETTINGS_FILE, config.STATE_FILE, config.THEMES_DIR, profile_ops.PROFILES_DIR, state.STATE_FILE, tokens.TOKENS_FILE, wizard.CLAUDE_SYMLINK, wizard.PROFILES_DIR, wizard.SCRIPTS_DIR, wizard.SHARED_DIR, wizard.SHARED_SETTINGS_FILE, wizard.SKILLS_DIR
- [ ] test_workspace_contracts.py — discovery.PROFILES_DIR, discovery.TOKENS_FILE, profile.TOKENS_FILE
