"""Tests for the client-adapter seam (claudewheel.clients) via resolve_launch_config.

The claude adapter's behavior is covered by tests/test_launch.py (the refactor
is behavior-preserving). This module exercises the miniclaude adapter end to
end through resolve_launch_config, using the same dependency-injection style as
tests/test_launch.py: a tmpdir-backed BinaryLocator + ProfileStore, and
fetch_gh_token mocked out. The miniclaude binary is supplied via
clients_config (config.clients.miniclaude.binary) so no real binary or PATH
lookup is needed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.binaries import BinaryLocator
from claudewheel.launch import resolve_launch_config
from claudewheel.profile_store import ProfileStore
from claudewheel.tokens import TokenStore


class MiniclaudeAdapterTestBase(unittest.TestCase):
    """Base class: tmpdir-backed BinaryLocator + ProfileStore per test."""

    MC_BINARY = "/opt/miniclaude/bin/miniclaude"

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
            self.profiles_dir, self.claude_dir, self.token_store,
        )
        # Every test that reaches the binary step gets a discoverable "work"
        # profile so profile resolution succeeds.
        self._make_profile("work")

    def _make_profile(self, name: str) -> Path:
        pdir = self.profiles_dir / name
        pdir.mkdir()
        (pdir / "settings.json").write_text("{}")
        return pdir

    def _resolve(
        self,
        selections: dict | None = None,
        *,
        options_def: dict | None = None,
        extra_flags: list[str] | None = None,
        passthrough: list[str] | None = None,
        clients_config: dict | None = None,
    ) -> tuple[str, list[str], dict[str, str]]:
        """Call resolve_launch_config for the miniclaude client."""
        if selections is None:
            selections = {"profile": "work"}
        if options_def is None:
            options_def = {}
        if clients_config is None:
            clients_config = {"miniclaude": {"binary": self.MC_BINARY}}

        with mock.patch("claudewheel.launch.fetch_gh_token", return_value=None):
            return resolve_launch_config(
                selections, options_def, [],
                locator=self.locator,
                profiles=self.profiles,
                extra_flags=extra_flags,
                client="miniclaude",
                clients_config=clients_config,
                passthrough=passthrough,
            )


class MiniclaudeArgvTests(MiniclaudeAdapterTestBase):
    """Argv shape for typical and per-flag selections."""

    def test_typical_selection_full_argv(self) -> None:
        """A typical selection set maps to `miniclaude repl` with model + perm mode."""
        _, argv, _ = self._resolve(selections={
            "profile": "work",
            "model": "claude-opus-4-8",
            "permissions": "bypass",
            "directory": str(self.tmp),
        })
        self.assertEqual(argv, [
            self.MC_BINARY, "repl",
            "--profile", "work",
            "--model", "claude-opus-4-8",
            "--permission-mode", "bypassPermissions",
        ])

    def test_model_omitted_when_not_selected(self) -> None:
        """No model selection -> no --model flag."""
        _, argv, _ = self._resolve(selections={"profile": "work"})
        self.assertEqual(argv, [self.MC_BINARY, "repl", "--profile", "work"])
        self.assertNotIn("--model", argv)

    def test_permission_mode_omitted_when_not_selected(self) -> None:
        """No permissions selection -> no --permission-mode flag."""
        _, argv, _ = self._resolve(selections={"profile": "work"})
        self.assertNotIn("--permission-mode", argv)

    def test_binary_from_config(self) -> None:
        """The configured clients.miniclaude.binary is argv[0]."""
        _, argv, _ = self._resolve(
            clients_config={"miniclaude": {"binary": "/custom/mc"}},
        )
        self.assertEqual(argv[0], "/custom/mc")

    def test_binary_from_path_when_unconfigured(self) -> None:
        """With no config binary, shutil.which('miniclaude') supplies argv[0]."""
        with mock.patch("claudewheel.clients.shutil.which", return_value="/usr/bin/miniclaude") as which:
            _, argv, _ = self._resolve(clients_config={})
        which.assert_called_once_with("miniclaude")
        self.assertEqual(argv[0], "/usr/bin/miniclaude")

    def test_env_assembly_unchanged(self) -> None:
        """env carries CLAUDE_CONFIG_DIR just like the claude client."""
        _, _, env = self._resolve(selections={"profile": "work"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.profiles_dir / "work"))


class MiniclaudePermissionMappingTests(MiniclaudeAdapterTestBase):
    """Every claudewheel permission value maps to its miniclaude mode."""

    def _perm_mode(self, perm: str) -> str:
        _, argv, _ = self._resolve(selections={"profile": "work", "permissions": perm})
        idx = argv.index("--permission-mode")
        return argv[idx + 1]

    def test_bypass_maps_to_bypassPermissions(self) -> None:
        self.assertEqual(self._perm_mode("bypass"), "bypassPermissions")

    def test_default_maps_to_default(self) -> None:
        self.assertEqual(self._perm_mode("default"), "default")

    def test_plan_maps_to_plan(self) -> None:
        self.assertEqual(self._perm_mode("plan"), "plan")

    def test_auto_maps_to_auto(self) -> None:
        self.assertEqual(self._perm_mode("auto"), "auto")


class MiniclaudeSessionFlagTests(MiniclaudeAdapterTestBase):
    """Session-flag translation from claude form to miniclaude form."""

    def test_continue_maps_to_continue_session(self) -> None:
        """claude --continue -> miniclaude --continue-session."""
        _, argv, _ = self._resolve(
            selections={"profile": "work"}, extra_flags=["--continue"],
        )
        self.assertEqual(argv[-1], "--continue-session")
        self.assertNotIn("--continue", argv)

    def test_resume_with_id_passes_through(self) -> None:
        """claude --resume <id> -> miniclaude --resume <id>."""
        _, argv, _ = self._resolve(
            selections={"profile": "work"}, extra_flags=["--resume", "sess-123"],
        )
        self.assertEqual(argv[-2:], ["--resume", "sess-123"])


class MiniclaudeHardErrorTests(MiniclaudeAdapterTestBase):
    """Every claude-only / unsupported input is a hard error, never a silent drop."""

    def test_missing_binary_raises(self) -> None:
        """No config binary and no PATH miniclaude -> hard error naming the config key."""
        with mock.patch("claudewheel.clients.shutil.which", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                self._resolve(clients_config={})
        msg = str(ctx.exception)
        self.assertIn("miniclaude binary not found", msg)
        self.assertIn("clients.miniclaude.binary", msg)

    def test_no_profile_raises(self) -> None:
        """No profile selected -> hard error (miniclaude requires a profile)."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve(selections={"profile": None})
        self.assertIn("requires a claudewheel profile", str(ctx.exception))

    def test_bare_resume_raises(self) -> None:
        """A bare --resume (session picker) -> hard error (no picker in miniclaude)."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve(selections={"profile": "work"}, extra_flags=["--resume"])
        self.assertIn("no session picker", str(ctx.exception))

    def test_print_raises(self) -> None:
        """--print / -p print mode -> hard error (unsupported)."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve(
                selections={"profile": "work"},
                extra_flags=["--print", "do a thing"],
            )
        self.assertIn("print mode", str(ctx.exception))

    def test_passthrough_raises(self) -> None:
        """Any passthrough after -- -> hard error (no generic passthrough)."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve(
                selections={"profile": "work"},
                extra_flags=["--foo", "bar"],
                passthrough=["--foo", "bar"],
            )
        self.assertIn("passthrough", str(ctx.exception))

    def test_version_selection_raises(self) -> None:
        """An explicit version selection -> hard error naming it as claude-client-only."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve(selections={"profile": "work", "version": "2.1.116"})
        msg = str(ctx.exception)
        self.assertIn("2.1.116", msg)
        self.assertIn("claude-client-only", msg)

    def test_mcp_strict_raises(self) -> None:
        """MCP strict -> hard error naming it as claude-client-only."""
        with self.assertRaises(ValueError) as ctx:
            self._resolve(selections={"profile": "work", "mcp": "strict"})
        msg = str(ctx.exception)
        self.assertIn("strict", msg)
        self.assertIn("claude-client-only", msg)

    def test_mcp_default_is_not_an_error(self) -> None:
        """MCP 'default' is the no-op mode and must NOT error for miniclaude."""
        _, argv, _ = self._resolve(selections={"profile": "work", "mcp": "default"})
        self.assertEqual(argv[:2], [self.MC_BINARY, "repl"])


class UnknownClientTests(MiniclaudeAdapterTestBase):
    """resolve_launch_config rejects an unknown client name."""

    def test_unknown_client_raises(self) -> None:
        with mock.patch("claudewheel.launch.fetch_gh_token", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                resolve_launch_config(
                    {"profile": "work"}, {}, [],
                    locator=self.locator,
                    profiles=self.profiles,
                    client="bogus",
                )
        self.assertIn("bogus", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
