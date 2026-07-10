"""Tests for profile.resolve_profile().

resolve_profile() is now a thin facade over Workspace.default().profiles.env().
These tests exercise it end-to-end against a real sandbox workspace (no mocks):
resolution is pointed at the sandbox via the public CLAUDEWHEEL_CONFIG_DIR env
var, and the ~/.claude default is covered by the poisoned Path.home from
SandboxHomeTestCase.

Scenario accounting (relative to the pre-facade ResolveProfileTests):

- PRESERVED (4): happy path with a token; profile without a token entry;
  unknown profile raises ValueError listing the available profiles; a
  bare-string legacy token entry still resolves.
- DELETED (1): the old "metadata missing config_dir raises ValueError" case is
  unrepresentable -- config_dir is now computed from the on-disk profile
  directory, never stored in options.json metadata.
- INVERTED (1): a corrupt tokens.json now RAISES TokenStoreError (naming the
  tokens.json path) instead of being silently ignored.
- ADDED (1): read-only resolution against a chmod-locked (0o555 dirs / 0o444
  files) sandbox workspace, proving zero-write resolution.

No mock of AppConfigStore (or any other production symbol) remains in this file.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from claudewheel.profile import resolve_profile
from claudewheel.tokens import TokenStoreError
from tests.wheelhelpers import SandboxHomeTestCase, write_json


def _set_tree_mode(root: Path, dir_mode: int, file_mode: int) -> None:
    """chmod every dir/file under *root* (inclusive). Files first, then dirs."""
    dirs: list[Path] = [root]
    files: list[Path] = []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            dirs.append(Path(dp) / d)
        for f in fns:
            files.append(Path(dp) / f)
    for f in files:
        os.chmod(f, file_mode)
    for d in dirs:
        os.chmod(d, dir_mode)


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

    def _write_tokens(self, tokens: dict) -> None:
        write_json(self.launcher_dir / "tokens.json", tokens)

    def test_valid_profile_with_token(self) -> None:
        """Returns both CLAUDE_CONFIG_DIR and CLAUDE_CODE_OAUTH_TOKEN (dict entry)."""
        pdir = self.make_profile("work")
        self._write_tokens({"work": {"token": "tok_dict", "created": "2025-01-01"}})

        result = resolve_profile("work")

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_dict")

    def test_valid_profile_with_bare_string_token(self) -> None:
        """A legacy bare-string token entry still resolves to the token."""
        pdir = self.make_profile("work")
        self._write_tokens({"work": "tok_bare"})

        result = resolve_profile("work")

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_bare")

    def test_valid_profile_without_token(self) -> None:
        """Returns only CLAUDE_CONFIG_DIR when no token entry exists."""
        pdir = self.make_profile("personal")
        # tokens.json stays {} from the sandbox.

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

    def test_corrupt_tokens_file_raises(self) -> None:
        """Corrupt tokens.json is a hard error naming the file (inverted contract)."""
        self.make_profile("work")
        (self.launcher_dir / "tokens.json").write_text("not valid json{")

        with self.assertRaises(TokenStoreError) as ctx:
            resolve_profile("work")

        self.assertIn("tokens.json", str(ctx.exception))

    def test_readonly_resolution(self) -> None:
        """Resolution succeeds against a chmod-locked, read-only workspace."""
        pdir = self.make_profile("work")
        self._write_tokens({"work": "tok_ro"})

        # Restore write bits before sandbox cleanup (LIFO: runs before rmtree).
        self.addCleanup(_set_tree_mode, self.launcher_dir, 0o755, 0o644)
        _set_tree_mode(self.launcher_dir, dir_mode=0o555, file_mode=0o444)

        result = resolve_profile("work")

        self.assertEqual(result["CLAUDE_CONFIG_DIR"], str(pdir))
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_ro")


if __name__ == "__main__":
    unittest.main()
