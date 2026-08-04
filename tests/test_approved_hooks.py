"""Tests for the approved-hooks preflight step (Phase 2.2 + 2.3).

The step reads the target project's Claude Code hooks and, on first sighting or
change, prompts for one-key approval (interactive) or aborts with an actionable
message (non-interactive). It never silently trusts project hooks.

UI is exercised with the shared FakeTerminal by substituting the step's
``_make_terminal`` factory; the rest of the flow (state persistence, fingerprint
comparison, realpath keying) runs for real against a sandbox workspace.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.appdata import StateFile
from claudewheel.preflight import PreflightContext, _approved_hooks_run
from claudewheel.state import get_project_hook_approvals, set_project_hook_approvals
from .wheelhelpers import FakeTerminal, SandboxHomeTestCase

_POPULATED = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
        ]
    }
}

_CHANGED = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo BYE"}]}
        ]
    }
}


class ApprovedHooksStepTests(SandboxHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.ws.appconfig()
        # A target project directory (outside the sandbox home is fine).
        self._proj = Path(self._tmp.name) / "project"
        self._proj.mkdir(parents=True, exist_ok=True)

    # -- helpers ----------------------------------------------------------

    def _write_hooks(self, data: dict, *, name: str = "settings.json") -> None:
        claude = self._proj / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / name).write_text(json.dumps(data))

    def _ctx(self, *, interactive: bool = True, directory: str | None = None) -> PreflightContext:
        return PreflightContext(
            selections={"directory": directory or str(self._proj)},
            workspace=self.ws,
            locator=mock.MagicMock(),
            cfg=self.cfg,
            interactive=interactive,
        )

    def _statefile(self) -> StateFile:
        return StateFile(self.ws.state_file)

    def _run_with_keys(self, keys: list[str], ctx: PreflightContext) -> tuple:
        """Run the step with a FakeTerminal fed *keys*; return (result, term)."""
        term = FakeTerminal(keys)
        with mock.patch(
            "claudewheel.preflight._make_terminal", autospec=True, return_value=term
        ):
            result = _approved_hooks_run(ctx)
        return result, term

    # -- no hooks ---------------------------------------------------------

    def test_no_hooks_continues_and_stores_nothing(self) -> None:
        result = _approved_hooks_run(self._ctx())
        self.assertFalse(result.is_abort)
        self.assertIsNone(get_project_hook_approvals(self._statefile(), str(self._proj)))

    def test_no_hooks_never_prompts(self) -> None:
        # A terminal factory that would explode proves no prompt is rendered.
        with mock.patch(
            "claudewheel.preflight._make_terminal",
            autospec=True,
            side_effect=AssertionError("must not prompt"),
        ):
            result = _approved_hooks_run(self._ctx())
        self.assertFalse(result.is_abort)

    # -- first sighting ---------------------------------------------------

    def test_first_sighting_approve_persists_and_continues(self) -> None:
        self._write_hooks(_POPULATED)
        result, term = self._run_with_keys(["y"], self._ctx())
        self.assertFalse(result.is_abort)
        stored = get_project_hook_approvals(self._statefile(), str(self._proj))
        from claudewheel.project_hooks import read_project_hooks

        self.assertEqual(stored, read_project_hooks(str(self._proj)).fingerprint)
        # The page listed the hook command.
        self.assertIn("echo hi", "".join(term.output))

    def test_first_sighting_decline_aborts_and_stores_nothing(self) -> None:
        self._write_hooks(_POPULATED)
        result, _ = self._run_with_keys(["n"], self._ctx())
        self.assertTrue(result.is_abort)
        self.assertIn("Declined", result.message)
        self.assertIsNone(get_project_hook_approvals(self._statefile(), str(self._proj)))

    def test_esc_declines(self) -> None:
        self._write_hooks(_POPULATED)
        result, _ = self._run_with_keys(["ESC"], self._ctx())
        self.assertTrue(result.is_abort)
        self.assertIsNone(get_project_hook_approvals(self._statefile(), str(self._proj)))

    def test_first_sighting_title_says_contributes(self) -> None:
        self._write_hooks(_POPULATED)
        _, term = self._run_with_keys(["y"], self._ctx())
        self.assertIn("contributes", "".join(term.output))

    # -- unchanged fingerprint -------------------------------------------

    def test_matching_fingerprint_never_prompts(self) -> None:
        self._write_hooks(_POPULATED)
        from claudewheel.project_hooks import read_project_hooks

        fp = read_project_hooks(str(self._proj)).fingerprint
        set_project_hook_approvals(self._statefile(), str(self._proj), fp)

        with mock.patch(
            "claudewheel.preflight._make_terminal",
            autospec=True,
            side_effect=AssertionError("must not prompt"),
        ):
            result = _approved_hooks_run(self._ctx())
        self.assertFalse(result.is_abort)

    # -- changed fingerprint ---------------------------------------------

    def test_changed_hooks_reprompt_with_changed_title(self) -> None:
        self._write_hooks(_POPULATED)
        from claudewheel.project_hooks import read_project_hooks

        old_fp = read_project_hooks(str(self._proj)).fingerprint
        set_project_hook_approvals(self._statefile(), str(self._proj), old_fp)

        # Now change the hooks.
        self._write_hooks(_CHANGED)
        result, term = self._run_with_keys(["y"], self._ctx())
        self.assertFalse(result.is_abort)
        self.assertIn("changed", "".join(term.output))
        new_fp = read_project_hooks(str(self._proj)).fingerprint
        self.assertEqual(
            get_project_hook_approvals(self._statefile(), str(self._proj)), new_fp
        )
        self.assertNotEqual(old_fp, new_fp)

    # -- non-interactive --------------------------------------------------

    def test_non_interactive_unapproved_aborts(self) -> None:
        self._write_hooks(_POPULATED)
        result = _approved_hooks_run(self._ctx(interactive=False))
        self.assertTrue(result.is_abort)
        self.assertIn("interactive", result.message.lower())
        self.assertIsNone(get_project_hook_approvals(self._statefile(), str(self._proj)))

    def test_non_interactive_matching_continues(self) -> None:
        self._write_hooks(_POPULATED)
        from claudewheel.project_hooks import read_project_hooks

        fp = read_project_hooks(str(self._proj)).fingerprint
        set_project_hook_approvals(self._statefile(), str(self._proj), fp)
        result = _approved_hooks_run(self._ctx(interactive=False))
        self.assertFalse(result.is_abort)

    # -- malformed --------------------------------------------------------

    def test_malformed_config_aborts_naming_file(self) -> None:
        claude = self._proj / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / "settings.local.json").write_text("{ broken")
        result = _approved_hooks_run(self._ctx())
        self.assertTrue(result.is_abort)
        self.assertIn("settings.local.json", result.message)

    def test_malformed_aborts_before_non_interactive_message(self) -> None:
        claude = self._proj / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / "settings.json").write_text("nope")
        result = _approved_hooks_run(self._ctx(interactive=False))
        self.assertTrue(result.is_abort)
        self.assertIn("settings.json", result.message)

    # -- realpath canonicalization ---------------------------------------

    def test_symlinked_dir_approved_once(self) -> None:
        self._write_hooks(_POPULATED)
        # Approve via the real path.
        self._run_with_keys(["y"], self._ctx(directory=str(self._proj)))

        # A symlink pointing at the same project must be recognized as approved
        # (realpath keying), so no prompt on the symlinked path.
        link = Path(self._tmp.name) / "project-link"
        link.symlink_to(self._proj)
        with mock.patch(
            "claudewheel.preflight._make_terminal",
            autospec=True,
            side_effect=AssertionError("must not prompt for an already-approved dir"),
        ):
            result = _approved_hooks_run(self._ctx(directory=str(link)))
        self.assertFalse(result.is_abort)


if __name__ == "__main__":
    unittest.main()
