"""Tests for the preflight step framework and its wiring into _do_launch_sequence."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli, effects
from claudewheel.binaries import BinaryLocator
from claudewheel.preflight import (
    Decision,
    PreflightContext,
    PreflightStep,
    StepResult,
    _model_version_guard_run,
    _plan_declaration_run,
    _release_notes_seen_run,
    run_preflight,
)
from claudewheel.profile_data import ProfileDataStore
from claudewheel.tokens import plan_by_key, plan_keys
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    FakeAppConfigStore,
    inert_locator,
    inert_workspace,
    write_token_entry,
)

# Never created on disk. Workspace and BinaryLocator construction is pure value
# assembly, and the synthetic steps below read no path off either object, so the
# stand-ins built from this root touch nothing.
_INERT_ROOT = Path(tempfile.gettempdir()) / "claudewheel-inert-preflight-root"


def _ctx(interactive: bool = True) -> PreflightContext:
    """A PreflightContext whose non-step fields are inert stand-ins.

    Synthetic steps never touch workspace/locator/cfg, so real but unpopulated
    objects of the declared types are sufficient.
    """
    return PreflightContext(
        selections={},
        workspace=inert_workspace(_INERT_ROOT),
        locator=inert_locator(_INERT_ROOT),
        cfg=FakeAppConfigStore(),
        interactive=interactive,
    )


def _step(
    name: str,
    result: StepResult,
    log: list[str],
    *,
    runs_in_non_interactive: bool = True,
    renders_ui: bool = False,
) -> PreflightStep:
    """A synthetic step that records its name when run and returns *result*."""

    def _run(ctx: PreflightContext) -> StepResult:
        log.append(name)
        return result

    return PreflightStep(
        name=name,
        runs_in_non_interactive=runs_in_non_interactive,
        renders_ui=renders_ui,
        run=_run,
    )


class RunPreflightUnitTests(unittest.TestCase):
    """Direct tests of the sequence runner with synthetic steps."""

    def test_all_continue_returns_none(self) -> None:
        log: list[str] = []
        steps = [
            _step("a", StepResult.cont(), log),
            _step("b", StepResult.cont(), log),
        ]
        self.assertIsNone(run_preflight(_ctx(), steps))
        self.assertEqual(log, ["a", "b"])

    def test_ordering_is_deterministic(self) -> None:
        """Steps run in registration order regardless of their content."""
        log: list[str] = []
        steps = [_step(n, StepResult.cont(), log) for n in ("one", "two", "three")]
        run_preflight(_ctx(), steps)
        self.assertEqual(log, ["one", "two", "three"])

    def test_abort_halts_and_returns_message(self) -> None:
        log: list[str] = []
        steps = [
            _step("a", StepResult.cont(), log),
            _step("b", StepResult.abort("stop here"), log),
            _step("c", StepResult.cont(), log),
        ]
        result = run_preflight(_ctx(), steps)
        assert result is not None
        self.assertTrue(result.is_abort)
        self.assertEqual(result.decision, Decision.ABORT)
        self.assertEqual(result.message, "stop here")
        # "c" must never run after the abort.
        self.assertEqual(log, ["a", "b"])

    def test_non_interactive_skips_flagged_steps(self) -> None:
        """runs_in_non_interactive=False steps are skipped when interactive is False."""
        log: list[str] = []
        steps = [
            _step("always", StepResult.cont(), log, runs_in_non_interactive=True),
            _step("tui_only", StepResult.cont(), log, runs_in_non_interactive=False),
        ]
        run_preflight(_ctx(interactive=False), steps)
        self.assertEqual(log, ["always"])

    def test_interactive_runs_all_steps(self) -> None:
        log: list[str] = []
        steps = [
            _step("always", StepResult.cont(), log, runs_in_non_interactive=True),
            _step("tui_only", StepResult.cont(), log, runs_in_non_interactive=False),
        ]
        run_preflight(_ctx(interactive=True), steps)
        self.assertEqual(log, ["always", "tui_only"])

    def test_defaults_to_module_registered_list(self) -> None:
        """With no steps argument, the module-level PREFLIGHT_STEPS is used.

        Pinned to an empty list here so the contract (defaults to the module
        list) is verified independently of whatever concrete steps are
        registered.
        """
        with mock.patch("claudewheel.preflight.PREFLIGHT_STEPS", []):
            self.assertIsNone(run_preflight(_ctx()))


# ---------------------------------------------------------------------------
# Wiring into _do_launch_sequence (both TUI and skip-TUI paths).
# ---------------------------------------------------------------------------


class LaunchSequenceWiringTests(unittest.TestCase):
    """The preflight runs on both launch paths, and ABORT stops the launch."""

    def _run_sequence(
        self, steps: list[PreflightStep], interactive: bool
    ) -> mock.MagicMock:
        """Drive _do_launch_sequence with the launch/exec boundary stubbed.

        Returns the do_launch mock so callers can assert whether the launch was
        reached.
        """
        do_launch_mock = mock.MagicMock()
        with (
            mock.patch("claudewheel.preflight.PREFLIGHT_STEPS", steps),
            mock.patch("claudewheel.hooks.run_hooks", autospec=True, return_value=True),
            mock.patch("claudewheel.state.save_launch_state", autospec=True),
            mock.patch("claudewheel.state.record_inode", autospec=True),
            mock.patch(
                "claudewheel.launch.resolve_launch_config",
                autospec=True,
                return_value=("/cwd", ["/bin/claude"], {}),
            ),
            mock.patch("claudewheel.launch.do_launch", do_launch_mock),
        ):
            cli._do_launch_sequence(
                inert_workspace(_INERT_ROOT),
                mock.MagicMock(),
                FakeAppConfigStore(),
                {"profile": "p"},
                interactive=interactive,
            )
        return do_launch_mock

    def test_step_runs_on_tui_path(self) -> None:
        log: list[str] = []
        do_launch_mock = self._run_sequence(
            [_step("s", StepResult.cont(), log)], interactive=True
        )
        self.assertEqual(log, ["s"])
        do_launch_mock.assert_called_once()

    def test_step_runs_on_skip_tui_path(self) -> None:
        log: list[str] = []
        do_launch_mock = self._run_sequence(
            [_step("s", StepResult.cont(), log)], interactive=False
        )
        self.assertEqual(log, ["s"])
        do_launch_mock.assert_called_once()

    def test_abort_prints_message_and_exits_before_launch(self) -> None:
        log: list[str] = []
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                self._run_sequence(
                    [_step("s", StepResult.abort("blocked: fix X"), log)],
                    interactive=True,
                )
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("blocked: fix X", err.getvalue())

    def test_non_interactive_skips_tui_only_step_in_sequence(self) -> None:
        log: list[str] = []
        steps = [
            _step("always", StepResult.cont(), log, runs_in_non_interactive=True),
            _step("tui_only", StepResult.cont(), log, runs_in_non_interactive=False),
        ]
        self._run_sequence(steps, interactive=False)
        self.assertEqual(log, ["always"])


# ---------------------------------------------------------------------------
# Model -> minimum CLI version guard.
# ---------------------------------------------------------------------------


class ModelVersionGuardTests(unittest.TestCase):
    """The model-version-guard preflight step (defaults.MODEL_MIN_CLI_VERSION)."""

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)
        self.versions_dir = self.tmp / "versions"
        self.versions_dir.mkdir(parents=True)
        self.symlink = self.tmp / "bin" / "claude"
        self.symlink.parent.mkdir(parents=True)
        self.loc = BinaryLocator(
            versions_dir=self.versions_dir,
            claude_symlink=self.symlink,
        )

    def tearDown(self) -> None:
        self._tmp_obj.cleanup()

    def _ctx_with(self, selections: dict[str, str | None]) -> PreflightContext:
        return PreflightContext(
            selections=selections,
            workspace=inert_workspace(self.tmp),
            locator=self.loc,
            cfg=FakeAppConfigStore(),
            interactive=True,
        )

    def _install(self, name: str) -> Path:
        p = self.versions_dir / name
        p.write_text("#!/bin/sh\n")
        return p

    def test_registered_after_reconcile_guardrails(self) -> None:
        from claudewheel.preflight import PREFLIGHT_STEPS

        names = [s.name for s in PREFLIGHT_STEPS]
        self.assertIn("model-version-guard", names)
        self.assertIn("reconcile-guardrails", names)
        self.assertGreater(
            names.index("model-version-guard"),
            names.index("reconcile-guardrails"),
        )

    def test_step_is_always_on_no_ui(self) -> None:
        from claudewheel.preflight import PREFLIGHT_STEPS

        step = next(s for s in PREFLIGHT_STEPS if s.name == "model-version-guard")
        self.assertTrue(step.runs_in_non_interactive)
        self.assertFalse(step.renders_ui)

    def test_old_selected_version_blocks(self) -> None:
        ctx = self._ctx_with({"model": "claude-opus-5", "version": "2.1.100"})
        result = _model_version_guard_run(ctx)
        self.assertTrue(result.is_abort)
        self.assertIn("claude-opus-5", result.message)
        self.assertIn("2.1.219", result.message)
        self.assertIn("2.1.100", result.message)
        self.assertIn("claudewheel install", result.message)

    def test_new_enough_selected_version_passes(self) -> None:
        ctx = self._ctx_with({"model": "claude-opus-5", "version": "2.1.219"})
        self.assertEqual(_model_version_guard_run(ctx).decision, Decision.CONTINUE)

    def test_newer_selected_version_passes(self) -> None:
        ctx = self._ctx_with({"model": "claude-opus-5", "version": "2.1.220"})
        self.assertEqual(_model_version_guard_run(ctx).decision, Decision.CONTINUE)

    def test_no_selected_version_falls_back_to_symlink_target_block(self) -> None:
        self._install("2.1.100")
        self.symlink.symlink_to(self.versions_dir / "2.1.100")
        ctx = self._ctx_with({"model": "claude-opus-5", "version": None})
        result = _model_version_guard_run(ctx)
        self.assertTrue(result.is_abort)
        self.assertIn("2.1.100", result.message)

    def test_no_selected_version_falls_back_to_symlink_target_pass(self) -> None:
        self._install("2.1.219")
        self.symlink.symlink_to(self.versions_dir / "2.1.219")
        ctx = self._ctx_with({"model": "claude-opus-5", "version": None})
        self.assertEqual(_model_version_guard_run(ctx).decision, Decision.CONTINUE)

    def test_undeterminable_version_passes(self) -> None:
        # No selected version and no symlink -> cannot determine -> CONTINUE.
        ctx = self._ctx_with({"model": "claude-opus-5"})
        self.assertEqual(_model_version_guard_run(ctx).decision, Decision.CONTINUE)

    def test_unknown_model_passes(self) -> None:
        ctx = self._ctx_with({"model": "claude-sonnet-4-6", "version": "1.0.0"})
        self.assertEqual(_model_version_guard_run(ctx).decision, Decision.CONTINUE)

    def test_no_model_selected_passes(self) -> None:
        ctx = self._ctx_with({"version": "1.0.0"})
        self.assertEqual(_model_version_guard_run(ctx).decision, Decision.CONTINUE)

    def test_1m_suffix_stripped_before_lookup(self) -> None:
        # A "[1m]" variant of a guarded model still resolves to the base model
        # in the table; an old binary must still block.
        ctx = self._ctx_with({"model": "claude-opus-5[1m]", "version": "2.1.100"})
        result = _model_version_guard_run(ctx)
        self.assertTrue(result.is_abort)
        self.assertIn("claude-opus-5", result.message)


class PlanDeclarationStepTests(unittest.TestCase):
    """The plan-declaration preflight step.

    A profile launching on claudewheel's stored token is exactly the case where
    Claude Code cannot work its own tier out: the setup token carries no
    profile scope, so the tier resolves to null and tier-dependent checks fail
    closed. The step is the pre-launch writer of the three: it asks, through the
    same composite picker every other surface uses, and stores the answer.
    """

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)
        self.ws = Workspace.open(root=self.tmp, claude_dir=self.tmp / ".claude")
        (self.tmp / "profiles" / "work").mkdir(parents=True)

    def _ctx(
        self, interactive: bool = True, profile: str | None = "work"
    ) -> PreflightContext:
        return PreflightContext(
            selections={"profile": profile},
            workspace=self.ws,
            locator=inert_locator(self.tmp),
            cfg=FakeAppConfigStore(),
            interactive=interactive,
        )

    def _data(self, name: str = "work") -> ProfileDataStore:
        return self.ws.profiles.data_for(name)

    def _give_token(self, name: str = "work") -> None:
        write_token_entry(self.tmp / "profiles" / name, {"token": "tok"})

    def test_registered_before_approved_hooks(self) -> None:
        from claudewheel.preflight import PREFLIGHT_STEPS

        names = [s.name for s in PREFLIGHT_STEPS]
        self.assertIn("plan-declaration", names)
        self.assertLess(names.index("plan-declaration"), names.index("approved-hooks"))

    def test_registered_to_run_non_interactively(self) -> None:
        """It has to reach the headless path -- that is where it hard-errors."""
        from claudewheel.preflight import PREFLIGHT_STEPS

        step = next(s for s in PREFLIGHT_STEPS if s.name == "plan-declaration")
        self.assertTrue(step.runs_in_non_interactive)

    def test_a_declared_plan_passes_without_prompting(self) -> None:
        write_token_entry(
            self.tmp / "profiles" / "work",
            {"token": "tok", "subscriptionType": "max"},
        )
        with mock.patch("claudewheel.wizard.pick_plan", autospec=True) as picker:
            result = _plan_declaration_run(self._ctx())
        self.assertEqual(result.decision, Decision.CONTINUE)
        picker.assert_not_called()

    def test_a_profile_without_a_stored_token_passes(self) -> None:
        """Session-authed profiles read their tier from Claude Code's own file."""
        with mock.patch("claudewheel.wizard.pick_plan", autospec=True) as picker:
            result = _plan_declaration_run(self._ctx())
        self.assertEqual(result.decision, Decision.CONTINUE)
        picker.assert_not_called()

    def test_the_vanilla_default_passes(self) -> None:
        with mock.patch("claudewheel.wizard.pick_plan", autospec=True) as picker:
            result = _plan_declaration_run(self._ctx(profile="default"))
        self.assertEqual(result.decision, Decision.CONTINUE)
        picker.assert_not_called()

    def test_interactive_prompt_stores_the_answer(self) -> None:
        self._give_token()
        with (
            mock.patch("claudewheel.preflight._make_terminal", autospec=True),
            mock.patch(
                "claudewheel.wizard.pick_plan",
                autospec=True,
                return_value=plan_by_key("max-5x"),
            ),
        ):
            result = _plan_declaration_run(self._ctx())
        self.assertEqual(result.decision, Decision.CONTINUE)
        self.assertEqual(self._data().tier(), ("default_claude_max_5x", "max"))
        self.assertEqual(self._data().token(), "tok")

    def test_a_cancelled_prompt_aborts_naming_the_command(self) -> None:
        self._give_token()
        with (
            mock.patch("claudewheel.preflight._make_terminal", autospec=True),
            mock.patch(
                "claudewheel.wizard.pick_plan", autospec=True, return_value=None
            ),
        ):
            result = _plan_declaration_run(self._ctx())
        self.assertTrue(result.is_abort)
        self.assertIn("profile set-plan work", result.message)
        self.assertFalse(self._data().declares_plan())

    def test_headless_launch_aborts_naming_the_command_and_the_plans(self) -> None:
        self._give_token()
        with mock.patch("claudewheel.wizard.pick_plan", autospec=True) as picker:
            result = _plan_declaration_run(self._ctx(interactive=False))
        picker.assert_not_called()
        self.assertTrue(result.is_abort)
        self.assertIn("profile set-plan work", result.message)
        for key in plan_keys():
            self.assertIn(key, result.message)

    def test_a_rate_limit_tier_alone_is_not_a_declaration(self) -> None:
        write_token_entry(
            self.tmp / "profiles" / "work",
            {"token": "tok", "rateLimitTier": "default_claude_max_20x"},
        )
        with mock.patch("claudewheel.wizard.pick_plan", autospec=True):
            result = _plan_declaration_run(self._ctx(interactive=False))
        self.assertTrue(result.is_abort)

    def test_a_terminal_less_launch_stops_before_exec_naming_the_remedy(self) -> None:
        """End to end: no terminal, no declaration -> nonzero exit, no launch.

        The step runs through the real launch sequence here, on the path a
        flag-driven headless invocation takes, so nothing between it and the
        exec can quietly swallow the refusal.
        """
        from claudewheel.preflight import PREFLIGHT_STEPS

        self._give_token()
        step = next(s for s in PREFLIGHT_STEPS if s.name == "plan-declaration")
        do_launch_mock = mock.MagicMock()
        err = io.StringIO()

        with (
            mock.patch("claudewheel.preflight.PREFLIGHT_STEPS", [step]),
            mock.patch("claudewheel.hooks.run_hooks", autospec=True, return_value=True),
            mock.patch("claudewheel.launch.do_launch", do_launch_mock),
            redirect_stderr(err),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli._do_launch_sequence(
                    self.ws,
                    inert_locator(self.tmp),
                    FakeAppConfigStore(),
                    {"profile": "work"},
                    interactive=False,
                )

        self.assertNotEqual(ctx.exception.code, 0)
        do_launch_mock.assert_not_called()
        self.assertIn("profile set-plan work", err.getvalue())


class _RecordingHandle:
    """A stand-in for strictcli's effects handle that only records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        def record(*args: object, **kwargs: object) -> None:
            self.calls.append((name, args))

        return record


class _PreviewCtx:
    """The minimum dispatch context ``effects._handle`` accepts as previewing."""

    def __init__(self, handle: _RecordingHandle) -> None:
        self.dry_run = True
        self.effects = handle
        self.lines: list[str] = []

    def info(self, msg: str) -> None:
        self.lines.append(msg)


class ReleaseNotesSeenStepTests(unittest.TestCase):
    """The release-notes-seen preflight step.

    Claude Code prints its "Updated to latest. Got N features..." summary when
    ``lastReleaseNotesSeen`` in the profile's own ``.claude.json`` holds a
    version lower than the running one. Pre-seeding the key with the version
    about to be launched is the only way to prevent it -- there is no switch.
    """

    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)
        self.ws = Workspace.open(root=self.tmp, claude_dir=self.tmp / ".claude")
        self.profile_dir = self.ws.profiles.path_for("work")
        self.profile_dir.mkdir(parents=True)
        self.claude_json = self.profile_dir / ".claude.json"

    def _ctx(self, **selections: str | None) -> PreflightContext:
        base: dict[str, str | None] = {"profile": "work", "version": "2.1.263"}
        base.update(selections)
        return PreflightContext(
            selections=base,
            workspace=self.ws,
            locator=inert_locator(self.tmp),
            cfg=FakeAppConfigStore(),
            interactive=True,
        )

    def _write(self, data: dict[str, object]) -> None:
        self.claude_json.write_text(json.dumps(data, indent=2) + "\n")

    def _read(self) -> dict[str, object]:
        data = json.loads(self.claude_json.read_text())
        assert isinstance(data, dict)
        return data

    def test_registered_right_after_the_model_version_guard(self) -> None:
        from claudewheel.preflight import PREFLIGHT_STEPS

        names = [s.name for s in PREFLIGHT_STEPS]
        self.assertIn("release-notes-seen", names)
        self.assertEqual(
            names.index("release-notes-seen"),
            names.index("model-version-guard") + 1,
        )

    def test_step_is_always_on_no_ui(self) -> None:
        from claudewheel.preflight import PREFLIGHT_STEPS

        step = next(s for s in PREFLIGHT_STEPS if s.name == "release-notes-seen")
        self.assertTrue(step.runs_in_non_interactive)
        self.assertFalse(step.renders_ui)

    def test_absent_key_is_written_with_the_launched_version(self) -> None:
        self._write({"numStartups": 3})
        self.assertEqual(
            _release_notes_seen_run(self._ctx()).decision, Decision.CONTINUE
        )
        self.assertEqual(self._read()["lastReleaseNotesSeen"], "2.1.263")

    def test_a_lower_stored_version_is_overwritten(self) -> None:
        self._write({"lastReleaseNotesSeen": "2.1.100"})
        _release_notes_seen_run(self._ctx())
        self.assertEqual(self._read()["lastReleaseNotesSeen"], "2.1.263")

    def test_an_equal_stored_version_leaves_the_file_byte_identical(self) -> None:
        self._write({"lastReleaseNotesSeen": "2.1.263"})
        before = self.claude_json.read_bytes()
        handle = _RecordingHandle()
        with effects.bound(_PreviewCtx(handle)):
            _release_notes_seen_run(self._ctx())
        self.assertEqual(self.claude_json.read_bytes(), before)
        self.assertEqual(handle.calls, [])

    def test_a_higher_stored_version_is_left_alone(self) -> None:
        self._write({"lastReleaseNotesSeen": "2.2.0"})
        before = self.claude_json.read_bytes()
        _release_notes_seen_run(self._ctx())
        self.assertEqual(self.claude_json.read_bytes(), before)

    def test_the_default_profile_is_never_touched(self) -> None:
        claude_json = self.ws.profiles.path_for("default") / ".claude.json"
        claude_json.parent.mkdir(parents=True)
        claude_json.write_text("{}\n")
        _release_notes_seen_run(self._ctx(profile="default"))
        self.assertEqual(claude_json.read_text(), "{}\n")

    def test_a_missing_file_is_not_created(self) -> None:
        self.assertEqual(
            _release_notes_seen_run(self._ctx()).decision, Decision.CONTINUE
        )
        self.assertFalse(self.claude_json.exists())

    def test_an_unparsable_file_is_untouched_and_reported(self) -> None:
        self.claude_json.write_text("{not json")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                _release_notes_seen_run(self._ctx()).decision, Decision.CONTINUE
            )
        self.assertEqual(self.claude_json.read_text(), "{not json")
        self.assertIn("release notes as seen", out.getvalue())

    def test_a_non_object_document_is_untouched_and_reported(self) -> None:
        self.claude_json.write_text("[1, 2]\n")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(
                _release_notes_seen_run(self._ctx()).decision, Decision.CONTINUE
            )
        self.assertEqual(self.claude_json.read_text(), "[1, 2]\n")
        self.assertIn("release notes as seen", out.getvalue())

    def test_a_non_version_symlink_target_writes_nothing(self) -> None:
        """No selection means the symlink target's name IS the version.

        That name need not be a version at all -- a symlink pointing at a
        ``stable`` or ``latest`` entry resolves to exactly that string, and
        writing it into the key would only be rewritten by the client on the
        next launch. The step declines rather than writing junk.
        """
        self._write({"numStartups": 1})
        before = self.claude_json.read_bytes()
        versions = self.tmp / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        target = versions / "stable"
        target.write_text("")
        symlink = self.tmp / "bin" / "claude"
        symlink.parent.mkdir(parents=True, exist_ok=True)
        symlink.symlink_to(target)
        self.assertEqual(
            _release_notes_seen_run(self._ctx(version=None)).decision,
            Decision.CONTINUE,
        )
        self.assertEqual(self.claude_json.read_bytes(), before)

    def test_every_other_key_is_preserved(self) -> None:
        self._write(
            {
                "numStartups": 7,
                "projects": {"/home/x": {"allowedTools": []}},
                "lastReleaseNotesSeen": "2.1.100",
            }
        )
        _release_notes_seen_run(self._ctx())
        data = self._read()
        self.assertEqual(data["numStartups"], 7)
        self.assertEqual(data["projects"], {"/home/x": {"allowedTools": []}})
        self.assertEqual(data["lastReleaseNotesSeen"], "2.1.263")

    def test_no_determinable_version_writes_nothing(self) -> None:
        self._write({"numStartups": 1})
        before = self.claude_json.read_bytes()
        _release_notes_seen_run(self._ctx(version=None))
        self.assertEqual(self.claude_json.read_bytes(), before)

    def test_dry_run_records_the_write_and_leaves_the_file_alone(self) -> None:
        self._write({"lastReleaseNotesSeen": "2.1.100"})
        before = self.claude_json.read_bytes()
        handle = _RecordingHandle()
        with effects.bound(_PreviewCtx(handle)):
            _release_notes_seen_run(self._ctx())
        self.assertEqual(self.claude_json.read_bytes(), before)
        self.assertEqual([name for name, _ in handle.calls], ["write"])
        recorded_path, recorded_text = handle.calls[0][1]
        self.assertEqual(recorded_path, str(self.claude_json))
        self.assertEqual(
            json.loads(str(recorded_text))["lastReleaseNotesSeen"], "2.1.263"
        )


if __name__ == "__main__":
    unittest.main()
