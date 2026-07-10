"""Tests for the Workspace root path object in claudewheel.workspace."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest.mock import patch

import claudewheel
from claudewheel.tokens import TokenStore
from claudewheel.workspace import Workspace

from tests.wheelhelpers import SandboxHomeTestCase


class WorkspaceOpenTests(SandboxHomeTestCase):
    """Constructors and pure-value construction guarantees."""

    def test_open_claude_dir_defaults_to_home_claude_at_call_time(self) -> None:
        """claude_dir defaults to the (poisoned) home's .claude, not import-time."""
        ws = Workspace.open(self.launcher_dir)
        self.assertEqual(ws.claude_dir, self.home / ".claude")

    def test_open_explicit_claude_dir_is_honored(self) -> None:
        custom = self.home / "elsewhere" / ".claude"
        ws = Workspace.open(self.launcher_dir, custom)
        self.assertEqual(ws.claude_dir, custom)

    def test_open_performs_no_filesystem_writes(self) -> None:
        """Constructing a Workspace at a fresh root creates nothing on disk."""
        fresh = self.home / "brand-new-root"
        self.assertFalse(fresh.exists())
        Workspace.open(fresh)
        self.assertFalse(fresh.exists())

    def test_open_on_readonly_directory_succeeds(self) -> None:
        """A 0o555 root does not block pure-value construction."""
        ro = self.home / "readonly-root"
        ro.mkdir()
        ro.chmod(0o555)
        self.addCleanup(lambda: ro.chmod(0o755))
        ws = Workspace.open(ro)
        self.assertEqual(ws.root, ro)
        # Deriving paths must not touch disk either.
        self.assertEqual(ws.tokens_file, ro / "tokens.json")

    def test_workspace_is_frozen(self) -> None:
        ws = Workspace.open(self.launcher_dir)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ws.root = self.home  # type: ignore[misc]


class WorkspaceDefaultTests(SandboxHomeTestCase):
    """default() env-var handling."""

    def test_default_falls_back_to_home_claudewheel(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDEWHEEL_CONFIG_DIR", None)
            ws = Workspace.default()
        self.assertEqual(ws.root, self.home / ".claudewheel")

    def test_default_honors_env_override(self) -> None:
        override = self.home / "custom-config"
        with patch.dict(os.environ,
                        {"CLAUDEWHEEL_CONFIG_DIR": str(override)}):
            ws = Workspace.default()
        self.assertEqual(ws.root, override)

    def test_default_expands_user_in_override(self) -> None:
        with patch.dict(os.environ,
                        {"CLAUDEWHEEL_CONFIG_DIR": "~/tilde-config"}):
            ws = Workspace.default()
        self.assertEqual(ws.root, self.home / "tilde-config")

    def test_env_var_read_in_exactly_one_source_file(self) -> None:
        """Permanent guard: CLAUDEWHEEL_CONFIG_DIR appears in only workspace.py."""
        pkg_dir = Path(claudewheel.__file__).parent
        offenders = [
            py.name
            for py in sorted(pkg_dir.glob("*.py"))
            if "CLAUDEWHEEL_CONFIG_DIR" in py.read_text()
        ]
        self.assertEqual(offenders, ["workspace.py"])


class WorkspacePathTests(SandboxHomeTestCase):
    """All derived path properties resolve against root and match the layout."""

    def test_paths_match_sandbox_layout(self) -> None:
        root = self.launcher_dir
        ws = Workspace.open(root)
        expected = {
            "profiles_dir": root / "profiles",
            "tokens_file": root / "tokens.json",
            "options_file": root / "options.json",
            "state_file": root / "state.json",
            "config_file": root / "config.json",
            "segments_file": root / "segments.json",
            "themes_dir": root / "themes",
            "hooks_dir": root / "hooks",
            "scripts_dir": root / "scripts",
            "shared_dir": root / "shared",
            "skills_dir": root / "skills",
            "shared_settings_file": root / "shared-settings.json",
            "inodes_file": root / "shared" / "inodes.json",
        }
        for attr, value in expected.items():
            self.assertEqual(getattr(ws, attr), value, attr)

    def test_paths_match_constants_names(self) -> None:
        """Property values mirror the constants module's names when root is the
        sandbox's CONFIG_DIR (constructed from sandbox, not real constants)."""
        sp = self.sandbox_paths
        ws = Workspace.open(sp["CONFIG_DIR"])
        self.assertEqual(ws.profiles_dir, sp["PROFILES_DIR"])
        self.assertEqual(ws.tokens_file, sp["TOKENS_FILE"])
        self.assertEqual(ws.options_file, sp["OPTIONS_FILE"])
        self.assertEqual(ws.state_file, sp["STATE_FILE"])
        self.assertEqual(ws.config_file, sp["CONFIG_FILE"])
        self.assertEqual(ws.segments_file, sp["SEGMENTS_FILE"])
        self.assertEqual(ws.themes_dir, sp["THEMES_DIR"])
        self.assertEqual(ws.hooks_dir, sp["HOOKS_DIR"])
        self.assertEqual(ws.scripts_dir, sp["SCRIPTS_DIR"])
        self.assertEqual(ws.shared_dir, sp["SHARED_DIR"])
        self.assertEqual(ws.skills_dir, sp["SKILLS_DIR"])
        self.assertEqual(ws.shared_settings_file, sp["SHARED_SETTINGS_FILE"])
        self.assertEqual(ws.inodes_file, sp["INODES_FILE"])


class WorkspaceStoreTests(SandboxHomeTestCase):
    """The tokens store accessor."""

    def test_tokens_accessor_is_path_injected_tokenstore(self) -> None:
        ws = Workspace.open(self.launcher_dir)
        store = ws.tokens
        self.assertIsInstance(store, TokenStore)
        self.assertEqual(store.path, ws.tokens_file)

    def test_tokens_accessor_round_trips(self) -> None:
        ws = Workspace.open(self.launcher_dir)
        ws.tokens.add("prof", "tok-through-workspace")
        self.assertEqual(ws.tokens.token_for("prof"), "tok-through-workspace")


if __name__ == "__main__":
    import unittest

    unittest.main()
