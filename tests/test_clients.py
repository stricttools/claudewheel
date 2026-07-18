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
            self.profiles_dir,
            self.claude_dir,
            self.token_store,
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
                selections,
                options_def,
                [],
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
        _, argv, _ = self._resolve(
            selections={
                "profile": "work",
                "model": "claude-opus-4-8",
                "permissions": "bypass",
                "directory": str(self.tmp),
            }
        )
        self.assertEqual(
            argv,
            [
                self.MC_BINARY,
                "repl",
                "--profile",
                "work",
                "--model",
                "claude-opus-4-8",
                "--permission-mode",
                "bypassPermissions",
            ],
        )

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
        with mock.patch(
            "claudewheel.clients.shutil.which", return_value="/usr/bin/miniclaude"
        ) as which:
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
            selections={"profile": "work"},
            extra_flags=["--continue"],
        )
        self.assertEqual(argv[-1], "--continue-session")
        self.assertNotIn("--continue", argv)

    def test_resume_with_id_passes_through(self) -> None:
        """claude --resume <id> -> miniclaude --resume <id>."""
        _, argv, _ = self._resolve(
            selections={"profile": "work"},
            extra_flags=["--resume", "sess-123"],
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

    def test_mcp_default_is_not_an_error(self) -> None:
        """MCP 'default' is the no-op mode and must NOT error for miniclaude."""
        _, argv, _ = self._resolve(selections={"profile": "work", "mcp": "default"})
        self.assertEqual(argv[:2], [self.MC_BINARY, "repl"])


class MiniclaudeAmbientSelectionTests(MiniclaudeAdapterTestBase):
    """Regression: ambient (remembered/configured) claude-only selections must
    be IGNORED by the miniclaude adapter, not turned into hard errors.

    Reproduces the reported bug class: ``claudewheel --client miniclaude``
    failed with "... is claude-client-only ..." whenever a claude-only value
    was remembered in last_config (or set as a config default), because that
    ambient value flowed into the miniclaude adapter, which rejected it.
    ``version`` (every launch persists one -- it is a required segment) and
    ``mcp: "strict"`` are claude-only inputs on the same footing as
    default_flags and DISALLOWED_TOOLS, so they are dropped without error. A
    contradictory *explicit, same-invocation* override is rejected upstream in
    the CLI, where the selection's provenance is known.
    """

    def test_version_selection_is_ignored_not_an_error(self) -> None:
        """A version in the selections builds a normal argv (no --model/version)."""
        _, argv, _ = self._resolve(
            selections={"profile": "work", "version": "2.1.202"},
        )
        self.assertEqual(argv, [self.MC_BINARY, "repl", "--profile", "work"])
        # The claude version name never leaks into the miniclaude argv.
        self.assertNotIn("2.1.202", argv)

    def test_version_alongside_model_and_perms_still_builds(self) -> None:
        """Version is dropped while model/permissions still map through."""
        _, argv, _ = self._resolve(
            selections={
                "profile": "work",
                "version": "2.1.202",
                "model": "claude-opus-4-8",
                "permissions": "bypass",
            }
        )
        self.assertEqual(
            argv,
            [
                self.MC_BINARY,
                "repl",
                "--profile",
                "work",
                "--model",
                "claude-opus-4-8",
                "--permission-mode",
                "bypassPermissions",
            ],
        )

    def test_mcp_strict_is_ignored_not_an_error(self) -> None:
        """An ambient mcp='strict' builds a normal argv (no strict-MCP flag)."""
        _, argv, _ = self._resolve(
            selections={"profile": "work", "mcp": "strict"},
        )
        self.assertEqual(argv, [self.MC_BINARY, "repl", "--profile", "work"])
        # No strict-MCP artifact leaks into the miniclaude argv.
        self.assertNotIn("--strict-mcp-config", argv)

    def test_mcp_strict_alongside_other_selections_still_builds(self) -> None:
        """mcp='strict' is dropped while model/permissions still map through."""
        _, argv, _ = self._resolve(
            selections={
                "profile": "work",
                "mcp": "strict",
                "version": "2.1.202",
                "model": "claude-opus-4-8",
                "permissions": "plan",
            }
        )
        self.assertEqual(
            argv,
            [
                self.MC_BINARY,
                "repl",
                "--profile",
                "work",
                "--model",
                "claude-opus-4-8",
                "--permission-mode",
                "plan",
            ],
        )


class ResolveDefaultClientTests(unittest.TestCase):
    """resolve_default_client validates config.default_client against the registry."""

    def test_absent_key_defaults_to_claude(self) -> None:
        from claudewheel.clients import resolve_default_client

        self.assertEqual(resolve_default_client({}), "claude")

    def test_valid_value_returned(self) -> None:
        from claudewheel.clients import resolve_default_client

        self.assertEqual(
            resolve_default_client({"default_client": "miniclaude"}), "miniclaude"
        )

    def test_unknown_value_is_hard_error(self) -> None:
        from claudewheel.clients import resolve_default_client

        with self.assertRaises(ValueError) as ctx:
            resolve_default_client({"default_client": "bogus"})
        msg = str(ctx.exception)
        self.assertIn("bogus", msg)
        self.assertIn("known:", msg)
        self.assertIn("claude", msg)


class ClientAvailabilityTests(MiniclaudeAdapterTestBase):
    """client_available mirrors each adapter's binary resolution."""

    def test_claude_available_when_fallback_exists(self) -> None:
        from claudewheel.clients import client_available

        self.symlink_path.write_text("#!/bin/sh\n")  # fallback now exists
        self.assertTrue(client_available("claude", self.locator, {}))

    def test_claude_unavailable_when_fallback_missing(self) -> None:
        from claudewheel.clients import client_available

        self.assertFalse(self.symlink_path.exists())
        self.assertFalse(client_available("claude", self.locator, {}))

    def test_miniclaude_available_via_config_binary(self) -> None:
        from claudewheel.clients import client_available

        self.assertTrue(
            client_available(
                "miniclaude", self.locator, {"miniclaude": {"binary": self.MC_BINARY}}
            )
        )

    def test_miniclaude_available_via_path(self) -> None:
        from claudewheel.clients import client_available

        with mock.patch(
            "claudewheel.clients.shutil.which", return_value="/usr/bin/miniclaude"
        ):
            self.assertTrue(client_available("miniclaude", self.locator, {}))

    def test_miniclaude_unavailable_when_missing(self) -> None:
        from claudewheel.clients import client_available

        with mock.patch("claudewheel.clients.shutil.which", return_value=None):
            self.assertFalse(client_available("miniclaude", self.locator, {}))


class BuildClientChoicesTests(MiniclaudeAdapterTestBase):
    """build_client_choices exposes the registry as (key, label) picker options."""

    def test_options_are_the_registry_in_order(self) -> None:
        from claudewheel.clients import CLIENT_ADAPTERS, build_client_choices

        self.symlink_path.write_text("#!/bin/sh\n")
        options, _ = build_client_choices(
            self.locator, {"miniclaude": {"binary": self.MC_BINARY}}, "claude"
        )
        keys = [key for key, _label in options]
        self.assertEqual(keys, list(CLIENT_ADAPTERS))

    def test_initial_key_honors_default_client(self) -> None:
        from claudewheel.clients import build_client_choices

        _, initial = build_client_choices(
            self.locator, {"miniclaude": {"binary": self.MC_BINARY}}, "miniclaude"
        )
        self.assertEqual(initial, "miniclaude")

    def test_available_client_label_is_bare_name(self) -> None:
        from claudewheel.clients import build_client_choices

        self.symlink_path.write_text("#!/bin/sh\n")  # claude available
        options, _ = build_client_choices(
            self.locator, {"miniclaude": {"binary": self.MC_BINARY}}, "claude"
        )
        labels = dict(options)
        self.assertEqual(labels["claude"], "claude")

    def test_unavailable_client_gets_not_installed_suffix(self) -> None:
        from claudewheel.clients import build_client_choices

        # claude fallback missing -> "(not installed)"; miniclaude missing too.
        with mock.patch("claudewheel.clients.shutil.which", return_value=None):
            options, _ = build_client_choices(self.locator, {}, "claude")
        labels = dict(options)
        self.assertEqual(labels["claude"], "claude (not installed)")
        self.assertEqual(labels["miniclaude"], "miniclaude (not installed)")
        # Keys stay the bare client names -- the suffix is display-only.
        self.assertIn("claude", labels)
        self.assertIn("miniclaude", labels)


class ResolveClientTests(unittest.TestCase):
    """resolve_client: explicit --client wins and skips the prompt."""

    def test_explicit_client_skips_prompt(self) -> None:
        from claudewheel.clients import resolve_client

        prompt = mock.MagicMock()
        result = resolve_client("miniclaude", prompt)
        self.assertEqual(result, "miniclaude")
        prompt.assert_not_called()

    def test_no_explicit_client_invokes_prompt(self) -> None:
        from claudewheel.clients import resolve_client

        prompt = mock.MagicMock(return_value="claude")
        result = resolve_client(None, prompt)
        self.assertEqual(result, "claude")
        prompt.assert_called_once()

    def test_prompt_cancellation_propagates_none(self) -> None:
        from claudewheel.clients import resolve_client

        prompt = mock.MagicMock(return_value=None)
        self.assertIsNone(resolve_client(None, prompt))


class UnknownClientTests(MiniclaudeAdapterTestBase):
    """resolve_launch_config rejects an unknown client name."""

    def test_unknown_client_raises(self) -> None:
        with mock.patch("claudewheel.launch.fetch_gh_token", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                resolve_launch_config(
                    {"profile": "work"},
                    {},
                    [],
                    locator=self.locator,
                    profiles=self.profiles,
                    client="bogus",
                )
        self.assertIn("bogus", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
