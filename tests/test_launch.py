"""Tests for launch.py resolve_launch_config.

Covers disallowed-tools CLI flags, version selection / fallback via a
BinaryLocator, and profile resolution (config dir + OAuth token + the
hard-error contract for stale names) via an injected ProfileStore. Every
concern is injected as an explicit argument pointed at tmpdir paths -- no
module-constant patching.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.binaries import BinaryLocator
from claudewheel.defaults import DISALLOWED_TOOLS
from claudewheel.launch import resolve_launch_config
from claudewheel.profile_store import ProfileStore
from claudewheel.tokens import TokenStore, TokenStoreError


class ResolveLaunchConfigTestBase(unittest.TestCase):
    """Base class: tmpdir-backed BinaryLocator + ProfileStore per test."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.versions_dir = self.tmp / "versions"
        self.versions_dir.mkdir()
        self.symlink_path = self.tmp / "claude"
        self.tokens_file = self.tmp / "tokens.json"
        self.profiles_dir = self.tmp / "profiles"
        self.profiles_dir.mkdir()
        self.claude_dir = self.tmp / ".claude"

        self.locator = BinaryLocator(
            versions_dir=self.versions_dir,
            claude_symlink=self.symlink_path,
        )
        self.token_store = TokenStore(self.tokens_file)
        self.profiles = ProfileStore(
            self.profiles_dir,
            self.claude_dir,
            self.token_store,
        )

    def _make_profile(self, name: str) -> Path:
        """Create a discoverable profile dir under profiles_dir."""
        pdir = self.profiles_dir / name
        pdir.mkdir()
        (pdir / "settings.json").write_text("{}")
        return pdir

    def _resolve(
        self,
        selections: dict[str, str | None] | None = None,
        options_def: dict[str, object] | None = None,
        default_flags: list[str] | None = None,
        extra_flags: list[str] | None = None,
        locator: BinaryLocator | None = None,
        profiles: ProfileStore | None = None,
    ) -> tuple[str, list[str], dict[str, str]]:
        """Call resolve_launch_config with fetch_gh_token mocked out."""
        if selections is None:
            selections = {"profile": None}
        if options_def is None:
            options_def = {}
        if default_flags is None:
            default_flags = []

        with mock.patch(
            "claudewheel.launch.fetch_gh_token", autospec=True, return_value=None
        ):
            return resolve_launch_config(
                selections,
                options_def,
                default_flags,
                locator=locator or self.locator,
                profiles=profiles or self.profiles,
                extra_flags=extra_flags,
            )


class ResolveDisallowedToolsTests(ResolveLaunchConfigTestBase):
    """Verify --disallowedTools flag and its tool list appear correctly in argv."""

    def test_argv_includes_disallowed_tools_flag(self) -> None:
        """argv contains --disallowedTools and every tool in DISALLOWED_TOOLS."""
        _, argv, _ = self._resolve()

        self.assertIn("--disallowedTools", argv)
        for tool in DISALLOWED_TOOLS:
            self.assertIn(tool, argv)

    def test_disallowed_tools_follow_flag(self) -> None:
        """The tool names immediately follow --disallowedTools in argv order."""
        _, argv, _ = self._resolve()

        idx = argv.index("--disallowedTools")
        actual = argv[idx + 1 : idx + 1 + len(DISALLOWED_TOOLS)]
        self.assertEqual(actual, DISALLOWED_TOOLS)

    def test_disallowed_tools_after_other_flags(self) -> None:
        """--disallowedTools appears after default_flags and permission flags."""
        _, argv, _ = self._resolve(
            selections={"profile": None, "permissions": "bypass"},
            default_flags=["--verbose"],
        )

        idx_verbose = argv.index("--verbose")
        idx_bypass = argv.index("--dangerously-skip-permissions")
        idx_disallowed = argv.index("--disallowedTools")

        self.assertGreater(
            idx_disallowed,
            idx_verbose,
            "--disallowedTools should appear after --verbose",
        )
        self.assertGreater(
            idx_disallowed,
            idx_bypass,
            "--disallowedTools should appear after --dangerously-skip-permissions",
        )


class ResolveBinaryPathTests(ResolveLaunchConfigTestBase):
    """Version -> binary path selection and symlink fallback via the locator."""

    def test_argv_starts_with_fallback_symlink(self) -> None:
        """When no version is selected, argv[0] is the locator's fallback symlink."""
        _, argv, _ = self._resolve()
        self.assertEqual(argv[0], str(self.symlink_path))
        self.assertEqual(argv[0], str(self.locator.fallback))

    def test_selected_version_resolves_to_binary_path(self) -> None:
        """A selected, on-disk version resolves to its binary path under versions_dir."""
        binary = self.versions_dir / "2.1.116"
        binary.write_bytes(b"fake binary")

        _, argv, _ = self._resolve(selections={"profile": None, "version": "2.1.116"})
        self.assertEqual(argv[0], str(binary))

    def test_missing_version_raises_oserror(self) -> None:
        """A selected version that is not on disk raises OSError with guidance."""
        with self.assertRaises(OSError) as ctx:
            self._resolve(selections={"profile": None, "version": "9.9.9"})
        self.assertIn("9.9.9", str(ctx.exception))
        self.assertIn("not on disk", str(ctx.exception))


class ResolveProfileConfigDirTests(ResolveLaunchConfigTestBase):
    """Profile selection maps to CLAUDE_CONFIG_DIR via the ProfileStore."""

    def test_no_profile_uses_default_config_dir(self) -> None:
        """No profile -> the store's 'default' path (claude_dir), no token."""
        _, _, env = self._resolve(selections={"profile": None})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.claude_dir))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_selected_profile_sets_its_config_dir(self) -> None:
        """A discovered profile resolves to profiles_dir/<name>."""
        pdir = self._make_profile("work")
        _, _, env = self._resolve(selections={"profile": "work"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(pdir))

    def test_stale_profile_name_raises_valueerror(self) -> None:
        """A profile that no longer exists raises ValueError listing the available
        names -- the hard-error contract that replaced the silent ~/.claude fallback."""
        # No profile dirs, empty tokens -> nothing discoverable.
        with self.assertRaises(ValueError) as ctx:
            self._resolve(selections={"profile": "ghost"})
        msg = str(ctx.exception)
        self.assertIn("ghost", msg)
        self.assertIn("Available", msg)

    def test_stale_profile_lists_available_names(self) -> None:
        """The error names the profiles that DO exist."""
        self._make_profile("work")
        self._make_profile("personal")
        with self.assertRaises(ValueError) as ctx:
            self._resolve(selections={"profile": "ghost"})
        msg = str(ctx.exception)
        self.assertIn("work", msg)
        self.assertIn("personal", msg)


class ResolveTokenTests(ResolveLaunchConfigTestBase):
    """OAuth token resolution through the injected ProfileStore."""

    def test_token_from_store_sets_env(self) -> None:
        """A token present for the profile is written to CLAUDE_CODE_OAUTH_TOKEN."""
        self._make_profile("work")
        self.tokens_file.write_text(json.dumps({"work": "tok-abc"}))

        _, _, env = self._resolve(selections={"profile": "work"})
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-abc")

    def test_missing_tokens_file_yields_no_token(self) -> None:
        """A missing tokens.json is not an error and sets no OAuth token env."""
        self._make_profile("work")
        self.assertFalse(self.tokens_file.exists())

        _, _, env = self._resolve(selections={"profile": "work"})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_absent_entry_yields_no_token(self) -> None:
        """A tokens.json without the profile's entry sets no OAuth token env."""
        self._make_profile("work")
        self.tokens_file.write_text(json.dumps({"other": "tok"}))

        _, _, env = self._resolve(selections={"profile": "work"})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_no_profile_skips_token_lookup(self) -> None:
        """With no profile selected, no OAuth token is looked up even if corrupt."""
        self.tokens_file.write_text("{ not valid json")

        _, _, env = self._resolve(selections={"profile": None})
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_corrupt_tokens_raises_tokenstoreerror(self) -> None:
        """A corrupt tokens.json raises TokenStoreError naming the file path."""
        self.tokens_file.write_text("{ not valid json")

        with self.assertRaises(TokenStoreError) as ctx:
            self._resolve(selections={"profile": "work"})
        self.assertIn(str(self.tokens_file), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
