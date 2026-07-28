"""Phase 4: the vanilla, strictly read-only "default" profile (~/.claude).

Covers the launch integration (a full ``_do_launch_sequence`` run against the
default writes nothing into ~/.claude and injects neither CLAUDE_CONFIG_DIR nor
a token), the one-time vanilla-choice preflight step, and the opt-in guardrail
inject/remove round-trip.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import cli
from claudewheel.appdata import StateFile
from claudewheel.binaries import BinaryLocator
from claudewheel.defaults import build_canonical_shared_settings
from claudewheel.hook_scripts import HOOK_SCRIPTS
from claudewheel.state import get_vanilla_guardrails_opt_in
from tests.wheelhelpers import FakeTerminal, hash_snapshot, snapshot_tree


class _FakeCfg:
    """Minimal AppConfigStore stand-in for _do_launch_sequence."""

    def __init__(self) -> None:
        self.config = {
            "health_check_on_launch": False,
            "default_flags": [],
            "clients": {},
            "theme": "auto",
        }
        self.options_def: dict[str, Any] = {}
        self.state: dict[str, Any] = {}

    def load_theme(self, name: str) -> dict[str, Any]:  # pragma: no cover - inert
        return {}


class _VanillaTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._home_patch = mock.patch.object(
            Path, "home", autospec=True, return_value=self.home
        )
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

        self.cw = self.home / ".claudewheel"
        self.cw.mkdir(parents=True, exist_ok=True)
        self.claude_dir = self.home / ".claude"

        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(self.cw, claude_dir=self.claude_dir)

        self.project = self.home / "project"
        self.project.mkdir(parents=True, exist_ok=True)

        self.locator = BinaryLocator(
            versions_dir=self.home / "versions",
            claude_symlink=self.home / "claude",
        )

    def _canonical_hooks(self) -> dict[str, Any]:
        return build_canonical_shared_settings(self.ws.scripts_dir)["hooks"]


class VanillaLaunchIntegrationTests(_VanillaTestBase):
    """A full _do_launch_sequence against the default is truly vanilla."""

    def _launch(self, profile: str | None, interactive: bool = False) -> dict[str, str]:
        """Drive the REAL _do_launch_sequence (resolve NOT stubbed), capturing env."""
        captured: dict[str, dict[str, str]] = {}

        def _capture(cwd: str, argv: list[str], env: dict[str, str]) -> None:
            captured["env"] = env

        out = io.StringIO()
        with (
            mock.patch(
                "claudewheel.hooks.run_hooks", autospec=True, return_value=True
            ),
            mock.patch(
                "claudewheel.launch.fetch_gh_token", autospec=True, return_value=None
            ),
            mock.patch("claudewheel.launch.do_launch", _capture),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            cli._do_launch_sequence(
                self.ws,
                self.locator,
                _FakeCfg(),  # type: ignore[arg-type]
                {"profile": profile, "directory": str(self.project)},
                interactive=interactive,
            )
        return captured["env"]

    def test_explicit_default_writes_nothing_to_claude_dir(self) -> None:
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        (self.claude_dir / "settings.json").write_text("{}\n")
        before = snapshot_tree(self.claude_dir)

        env = self._launch("default")

        self.assertEqual(snapshot_tree(self.claude_dir), before)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_no_profile_fallback_is_vanilla(self) -> None:
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        before = snapshot_tree(self.claude_dir)

        env = self._launch(None)

        self.assertEqual(snapshot_tree(self.claude_dir), before)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_ambient_config_dir_stripped_on_launch(self) -> None:
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(
            "os.environ",
            {"CLAUDE_CONFIG_DIR": "/ambient", "CLAUDE_CODE_OAUTH_TOKEN": "amb"},
        ):
            env = self._launch("default")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
