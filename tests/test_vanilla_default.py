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
from claudewheel.state import (
    get_vanilla_guardrails_opt_in,
    set_vanilla_guardrails_opt_in,
)
from tests.wheelhelpers import FakeTerminal, snapshot_tree


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


class VanillaChoiceStepTests(_VanillaTestBase):
    """The one-time vanilla-vs-guardrails preflight step."""

    def setUp(self) -> None:
        super().setUp()
        # A real appconfig so theme resolution works in the prompt page.
        from tests.wheelhelpers import setup_temp_config_dir

        setup_temp_config_dir(self.home)
        self.cfg = self.ws.appconfig()

    def _ctx(self, profile: str | None, interactive: bool = True):
        from claudewheel.preflight import PreflightContext

        return PreflightContext(
            selections={"profile": profile, "directory": str(self.project)},
            workspace=self.ws,
            locator=mock.MagicMock(),
            cfg=self.cfg,
            interactive=interactive,
        )

    def _run(self, ctx, keys: list[str]) -> FakeTerminal:
        from claudewheel import preflight

        term = FakeTerminal(keys)
        with mock.patch.object(
            preflight, "_make_terminal", autospec=True, return_value=term
        ):
            preflight._vanilla_choice_run(ctx)
        return term

    def _opt_in(self):
        return get_vanilla_guardrails_opt_in(
            StateFile(self.ws.state_file), str(self.project)
        )

    def _claude_settings(self) -> Path:
        return self.claude_dir / "settings.json"

    def test_first_launch_opt_in_persists_and_injects(self) -> None:
        self._run(self._ctx("default"), ["g"])
        self.assertTrue(self._opt_in())
        s = json.loads(self._claude_settings().read_text())
        self.assertEqual(s["hooks"], self._canonical_hooks())

    def test_first_launch_stay_vanilla_persists_false_no_write(self) -> None:
        self._run(self._ctx("default"), ["v"])  # any non-g key stays vanilla
        self.assertIs(self._opt_in(), False)
        self.assertFalse(self._claude_settings().exists())

    def test_non_interactive_unset_does_not_prompt_or_persist(self) -> None:
        term = self._run(self._ctx("default", interactive=False), ["g"])
        # Never prompted: the terminal factory result was untouched.
        self.assertEqual(term.output, [])
        self.assertIsNone(self._opt_in())
        self.assertFalse(self._claude_settings().exists())

    def test_never_reprompts_once_answered(self) -> None:
        set_vanilla_guardrails_opt_in(
            StateFile(self.ws.state_file), str(self.project), False
        )
        term = self._run(self._ctx("default"), ["g"])
        self.assertEqual(term.output, [])  # no prompt rendered
        self.assertIs(self._opt_in(), False)
        self.assertFalse(self._claude_settings().exists())

    def test_already_opted_in_ensures_idempotently(self) -> None:
        set_vanilla_guardrails_opt_in(
            StateFile(self.ws.state_file), str(self.project), True
        )
        term = self._run(self._ctx("default"), [])
        self.assertEqual(term.output, [])  # no prompt: already answered
        s = json.loads(self._claude_settings().read_text())
        self.assertEqual(s["hooks"], self._canonical_hooks())

    def test_non_default_profile_is_noop(self) -> None:
        term = self._run(self._ctx("work"), ["g"])
        self.assertEqual(term.output, [])
        self.assertIsNone(self._opt_in())
        self.assertFalse(self._claude_settings().exists())


class VanillaGuardrailInjectRemoveTests(_VanillaTestBase):
    """Additive inject / exact-removal round-trip on ~/.claude/settings.json."""

    _USER_SETTINGS: dict[str, Any] = {
        "model": "claude-opus-4-8",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "/usr/local/bin/user-hook"}
                    ],
                }
            ]
        },
    }

    def _write_user_settings(self) -> bytes:
        from claudewheel.permission import save_settings

        self.claude_dir.mkdir(parents=True, exist_ok=True)
        save_settings(self.claude_dir / "settings.json", self._USER_SETTINGS)
        return (self.claude_dir / "settings.json").read_bytes()

    def test_inject_then_remove_leaves_user_content_byte_identical(self) -> None:
        from claudewheel.preflight import (
            ensure_vanilla_guardrails,
            remove_vanilla_guardrails,
        )

        original = self._write_user_settings()

        self.assertTrue(ensure_vanilla_guardrails(self.ws))
        s = json.loads((self.claude_dir / "settings.json").read_text())
        # Canonical hooks present (merged into the user's PreToolUse/Bash) ...
        self.assertEqual(s["hooks"], self._canonical_hooks_merged())
        # ... and the user's own hook survives.
        self.assertIn(
            {"type": "command", "command": "/usr/local/bin/user-hook"},
            s["hooks"]["PreToolUse"][0]["hooks"],
        )

        self.assertTrue(remove_vanilla_guardrails(self.ws))
        self.assertEqual((self.claude_dir / "settings.json").read_bytes(), original)

    def _canonical_hooks_merged(self) -> dict[str, Any]:
        """Expected hooks after merging canonical into the user's PreToolUse/Bash."""
        from copy import deepcopy

        from claudewheel.patch_profiles import merge_hooks

        hooks = deepcopy(self._USER_SETTINGS["hooks"])
        merge_hooks(hooks, self._canonical_hooks())
        return hooks

    def test_ensure_is_idempotent(self) -> None:
        from claudewheel.preflight import ensure_vanilla_guardrails

        self._write_user_settings()
        self.assertTrue(ensure_vanilla_guardrails(self.ws))
        path = self.claude_dir / "settings.json"
        first = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        # Second ensure: already present -> no write, byte-identical.
        self.assertFalse(ensure_vanilla_guardrails(self.ws))
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_ensure_creates_file_when_absent(self) -> None:
        from claudewheel.preflight import ensure_vanilla_guardrails

        self.assertTrue(ensure_vanilla_guardrails(self.ws))
        s = json.loads((self.claude_dir / "settings.json").read_text())
        self.assertEqual(s["hooks"], self._canonical_hooks())

    def test_remove_noop_when_no_cw_hooks(self) -> None:
        from claudewheel.preflight import remove_vanilla_guardrails

        original = self._write_user_settings()
        self.assertFalse(remove_vanilla_guardrails(self.ws))
        self.assertEqual((self.claude_dir / "settings.json").read_bytes(), original)

    def test_opted_out_launch_never_writes(self) -> None:
        """A default launch with opt-in explicitly False writes nothing to ~/.claude."""
        from claudewheel import preflight
        from claudewheel.preflight import PreflightContext

        self.claude_dir.mkdir(parents=True, exist_ok=True)
        set_vanilla_guardrails_opt_in(
            StateFile(self.ws.state_file), str(self.project), False
        )
        before = snapshot_tree(self.claude_dir)
        ctx = PreflightContext(
            selections={"profile": "default", "directory": str(self.project)},
            workspace=self.ws,
            locator=mock.MagicMock(),
            cfg=mock.MagicMock(),
            interactive=True,
        )
        term = FakeTerminal([])
        with mock.patch.object(
            preflight, "_make_terminal", autospec=True, return_value=term
        ):
            preflight._vanilla_choice_run(ctx)
        self.assertEqual(snapshot_tree(self.claude_dir), before)


if __name__ == "__main__":
    unittest.main()
