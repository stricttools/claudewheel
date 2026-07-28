"""Tests for the preflight step framework and its wiring into _do_launch_sequence."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from claudewheel import cli
from claudewheel.preflight import (
    Decision,
    PreflightContext,
    PreflightStep,
    StepResult,
    run_preflight,
)


def _ctx(interactive: bool = True) -> PreflightContext:
    """A PreflightContext whose non-step fields are inert stand-ins.

    Synthetic steps never touch workspace/locator/cfg, so None is sufficient.
    """
    return PreflightContext(
        selections={},
        workspace=None,  # type: ignore[arg-type]
        locator=None,  # type: ignore[arg-type]
        cfg=None,  # type: ignore[arg-type]
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

    def test_defaults_to_empty_registered_list(self) -> None:
        """With no steps argument, the registered (empty) list is a no-op."""
        self.assertIsNone(run_preflight(_ctx()))


# ---------------------------------------------------------------------------
# Wiring into _do_launch_sequence (both TUI and skip-TUI paths).
# ---------------------------------------------------------------------------


class _FakeWs:
    """Minimal Workspace stand-in for _do_launch_sequence."""

    def __init__(self) -> None:
        self.hooks_dir = "/tmp/hooks"
        self.shared = object()
        self.profiles = object()


class _FakeCfg:
    """Minimal AppConfigStore stand-in with the attrs the launch path reads."""

    def __init__(self) -> None:
        # health_check_on_launch False so the health block is a no-op and we do
        # not need to stub run_health_check.
        self.config = {
            "health_check_on_launch": False,
            "default_flags": [],
            "clients": {},
        }
        self.options_def = {}
        self.state = {}


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
            mock.patch(
                "claudewheel.hooks.run_hooks", autospec=True, return_value=True
            ),
            mock.patch("claudewheel.state.save_launch_state", autospec=True),
            mock.patch("claudewheel.state.record_inode", autospec=True),
            mock.patch(
                "claudewheel.launch.resolve_launch_config",
                autospec=True,
                return_value=("/cwd", ["/bin/claude"], {}),
            ),
            mock.patch("claudewheel.launch.do_launch", do_launch_mock),
            mock.patch("claudewheel.cli._write_tier_stub", autospec=True),
        ):
            cli._do_launch_sequence(
                _FakeWs(),  # type: ignore[arg-type]
                mock.MagicMock(),
                _FakeCfg(),  # type: ignore[arg-type]
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


if __name__ == "__main__":
    unittest.main()
