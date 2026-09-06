"""Tests for profile.resolve_profile().

resolve_profile() is now a thin facade over Workspace.default().profiles.env().
These tests exercise it end-to-end against a real sandbox workspace (no mocks):
resolution is pointed at the sandbox via the public CLAUDEWHEEL_CONFIG_DIR env
var, and the ~/.claude default is covered by the poisoned Path.home from
SandboxHomeTestCase.

Scenario accounting (relative to the pre-facade ResolveProfileTests):

- PRESERVED (3): happy path with a token; profile without a token entry;
  unknown profile raises ValueError listing the available profiles.
- DELETED (1): the old "metadata missing config_dir raises ValueError" case is
  unrepresentable -- config_dir is now computed from the on-disk profile
  directory, never stored in options.json metadata.
- INVERTED (1): a corrupt token entry now RAISES TokenStoreError (naming the
  entry's path) instead of being silently ignored.
- ADDED (1): read-only resolution against a chmod-locked (0o555 dirs / 0o444
  files) sandbox workspace, proving zero-write resolution.

No mock of AppConfigStore (or any other production symbol) remains in this file.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any

from claudewheel.profile import resolve_profile
from claudewheel.tokens import TokenStoreError
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    SandboxHomeTestCase,
    build_profile_dir,
    set_tree_mode as _set_tree_mode,
    write_token_entry,
)


class ResolveProfileTests(SandboxHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The public mechanism: point Workspace.default() at the sandbox root.
        self._orig_cw = os.environ.get("CLAUDEWHEEL_CONFIG_DIR")
        os.environ["CLAUDEWHEEL_CONFIG_DIR"] = str(self.launcher_dir)
        self.addCleanup(self._restore_cw)

    def _restore_cw(self) -> None:
        if self._orig_cw is None:
            os.environ.pop("CLAUDEWHEEL_CONFIG_DIR", None)
        else:
            os.environ["CLAUDEWHEEL_CONFIG_DIR"] = self._orig_cw

    def _write_token(self, name: str, entry: dict[str, Any]) -> None:
        self.write_token(name, entry)

    def test_valid_profile_with_token(self) -> None:
        """Returns both CLAUDE_CONFIG_DIR and CLAUDE_CODE_OAUTH_TOKEN (dict entry)."""
        pdir = self.make_profile("work")
        self._write_token("work", {"token": "tok_dict", "created": "2025-01-01"})

        result = resolve_profile("work")

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_dict")

    def test_valid_profile_without_token(self) -> None:
        """Returns only CLAUDE_CONFIG_DIR when no token entry exists."""
        pdir = self.make_profile("personal")
        # The profile stores no token entry at all.

        result = resolve_profile("personal")

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", result)

    def test_missing_profile_raises(self) -> None:
        """Raises ValueError listing available profiles for an unknown name."""
        self.make_profile("work")

        with self.assertRaises(ValueError) as ctx:
            resolve_profile("nonexistent")

        self.assertIn("nonexistent", str(ctx.exception))
        self.assertIn("work", str(ctx.exception))

    def test_corrupt_token_entry_raises(self) -> None:
        """A corrupt token entry is a hard error naming the file (inverted contract)."""
        self.make_profile("work")
        path = self.write_token("work", {"token": "t"})
        path.write_text("not valid json{")

        with self.assertRaises(TokenStoreError) as ctx:
            resolve_profile("work")

        self.assertIn(str(path), str(ctx.exception))

    def test_readonly_resolution(self) -> None:
        """Resolution succeeds against a chmod-locked, read-only workspace."""
        pdir = self.make_profile("work")
        self._write_token("work", {"token": "tok_ro"})

        # Restore write bits before sandbox cleanup (LIFO: runs before rmtree).
        self.addCleanup(_set_tree_mode, self.launcher_dir, 0o755, 0o644)
        _set_tree_mode(self.launcher_dir, dir_mode=0o555, file_mode=0o444)

        result = resolve_profile("work")

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_ro")

    def test_explicit_none_workspace_uses_default(self) -> None:
        """workspace=None is exactly the omitted argument: the default workspace."""
        pdir = self.make_profile("work")
        self._write_token("work", {"token": "tok_none"})

        result = resolve_profile("work", workspace=None)

        self.assertEqual(result, resolve_profile("work"))
        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_none")


class ResolveProfileInjectedWorkspaceTests(SandboxHomeTestCase):
    """Resolution against a workspace the caller injected.

    CLAUDEWHEEL_CONFIG_DIR is deliberately UNSET in these tests: the injected
    workspace is the only thing pointing resolution at the alternate root, so a
    resolution that reached for the env var (or for Workspace.default()) would
    look in the sandbox's own ~/.claudewheel instead and find nothing.
    """

    def setUp(self) -> None:
        super().setUp()
        self._orig_cw = os.environ.pop("CLAUDEWHEEL_CONFIG_DIR", None)
        self.addCleanup(self._restore_cw)

        self.alt_root = self.home / "alt-workspace"
        (self.alt_root / "profiles").mkdir(parents=True)
        self.alt_workspace = Workspace.open(self.alt_root)

    def _restore_cw(self) -> None:
        if self._orig_cw is not None:
            os.environ["CLAUDEWHEEL_CONFIG_DIR"] = self._orig_cw

    def _make_alt_profile(self, name: str) -> Path:
        return build_profile_dir(
            self.alt_root / "profiles",
            name,
            parents=True,
            exist_ok=True,
            credentials=True,
        )

    def test_injected_workspace_resolves_against_its_root(self) -> None:
        """The env points into the injected root, with no env var consulted."""
        pdir = self._make_alt_profile("work")
        write_token_entry(pdir, {"token": "tok_injected"})
        self.assertNotIn("CLAUDEWHEEL_CONFIG_DIR", os.environ)

        result = resolve_profile("work", workspace=self.alt_workspace)

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_injected")
        # The default workspace (the sandbox's own ~/.claudewheel) does not
        # carry this profile at all, so the injection is what answered.
        with self.assertRaises(ValueError):
            resolve_profile("work")

    def test_injected_workspace_missing_profile_raises(self) -> None:
        """An unknown name errors as on the default path: ValueError listing names."""
        self._make_alt_profile("work")

        with self.assertRaises(ValueError) as ctx:
            resolve_profile("nonexistent", workspace=self.alt_workspace)

        self.assertIn("nonexistent", str(ctx.exception))
        self.assertIn("work", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
