"""Tests for the scratchpad-cleanup preflight step (Phase 3.2).

The step is interactive-only and never aborts. It honors a state snooze, scans
the /tmp scratchpad root only when not snoozed, prompts (FakeTerminal) when stale
dirs exist, deletes on confirm, and snoozes on decline. UI is exercised by
substituting the step's ``_make_terminal`` factory; the scratchpad root is
redirected via ``scratchpad.tmp_claude_dir``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from claudewheel.appdata import StateFile
from claudewheel.preflight import PreflightContext, _scratchpad_cleanup_run
from claudewheel.scratchpad import SCRATCHPAD_STALE_DAYS
from claudewheel.state import (
    get_scratchpad_snooze_until,
    set_scratchpad_snooze_until,
)
from .wheelhelpers import FakeTerminal, SandboxHomeTestCase

_DAY = 86400.0


class ScratchpadCleanupStepTests(SandboxHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.ws.appconfig()
        # A redirected scratchpad root, so the step never touches the real
        # /tmp/claude-<uid>.
        self._root = Path(self._tmp.name) / "scratch"
        self._root.mkdir(parents=True, exist_ok=True)

    # -- helpers ----------------------------------------------------------

    def _make_dir(self, name: str, *, age_days: float, size: int = 2048) -> Path:
        """Create a scratchpad subdir whose whole tree has a uniform mtime."""
        d = self._root / name / "session1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "data.bin").write_bytes(b"x" * size)
        mtime = datetime.now(timezone.utc).timestamp() - age_days * _DAY
        # Touch files then dirs so directory mtimes are not bumped afterwards.
        for target in (
            d / "data.bin",
            d,
            self._root / name,
        ):
            os.utime(target, (mtime, mtime))
        return self._root / name

    def _ctx(self, *, interactive: bool = True) -> PreflightContext:
        return PreflightContext(
            selections={},
            workspace=self.ws,
            locator=mock.MagicMock(),
            cfg=self.cfg,
            interactive=interactive,
        )

    def _statefile(self) -> StateFile:
        return StateFile(self.ws.state_file)

    def _run_with_keys(self, keys: list[str], ctx: PreflightContext) -> tuple:
        term = FakeTerminal(keys)
        with (
            mock.patch(
                "claudewheel.scratchpad.tmp_claude_dir",
                autospec=True,
                return_value=self._root,
            ),
            mock.patch(
                "claudewheel.preflight._make_terminal",
                autospec=True,
                return_value=term,
            ),
        ):
            result = _scratchpad_cleanup_run(ctx)
        return result, term

    # -- declared flags ---------------------------------------------------

    def test_registered_after_approved_hooks_and_tui_only(self) -> None:
        from claudewheel.preflight import PREFLIGHT_STEPS

        names = [s.name for s in PREFLIGHT_STEPS]
        self.assertIn("scratchpad-cleanup", names)
        self.assertGreater(
            names.index("scratchpad-cleanup"), names.index("approved-hooks")
        )
        step = next(s for s in PREFLIGHT_STEPS if s.name == "scratchpad-cleanup")
        # Skipped entirely in non-interactive launches; renders UI.
        self.assertFalse(step.runs_in_non_interactive)
        self.assertTrue(step.renders_ui)

    # -- stale present ----------------------------------------------------

    def test_stale_dirs_offered_and_deleted_on_confirm(self) -> None:
        stale = self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        result, term = self._run_with_keys(["y"], self._ctx())
        self.assertFalse(result.is_abort)
        self.assertFalse(stale.exists())
        # The page named the stale dir.
        self.assertIn("projStale", "".join(term.output))
        # No snooze set on the delete path.
        self.assertIsNone(get_scratchpad_snooze_until(self._statefile()))

    def test_decline_leaves_intact_and_sets_snooze(self) -> None:
        stale = self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        before = datetime.now(timezone.utc)
        result, _ = self._run_with_keys(["n"], self._ctx())
        self.assertFalse(result.is_abort)
        self.assertTrue(stale.exists())
        snooze = get_scratchpad_snooze_until(self._statefile())
        self.assertIsNotNone(snooze)
        until = datetime.fromisoformat(snooze)
        # Snooze is roughly 7 days out.
        self.assertGreater(until, before + timedelta(days=6, hours=23))
        self.assertLess(until, before + timedelta(days=7, hours=1))

    def test_esc_declines_and_snoozes(self) -> None:
        stale = self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        result, _ = self._run_with_keys(["ESC"], self._ctx())
        self.assertFalse(result.is_abort)
        self.assertTrue(stale.exists())
        self.assertIsNotNone(get_scratchpad_snooze_until(self._statefile()))

    # -- snooze window ----------------------------------------------------

    def test_within_snooze_no_prompt_and_no_scan(self) -> None:
        self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        future = datetime.now(timezone.utc) + timedelta(days=3)
        set_scratchpad_snooze_until(self._statefile(), future.isoformat())

        with (
            mock.patch(
                "claudewheel.scratchpad.scan_scratchpad_dirs",
                autospec=True,
            ) as scan_mock,
            mock.patch(
                "claudewheel.preflight._make_terminal",
                autospec=True,
                side_effect=AssertionError("must not prompt within snooze"),
            ),
        ):
            result = _scratchpad_cleanup_run(self._ctx())
        self.assertFalse(result.is_abort)
        scan_mock.assert_not_called()

    def test_after_snooze_expiry_prompts_again(self) -> None:
        stale = self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        set_scratchpad_snooze_until(self._statefile(), past.isoformat())
        result, term = self._run_with_keys(["y"], self._ctx())
        self.assertFalse(result.is_abort)
        self.assertFalse(stale.exists())
        self.assertIn("projStale", "".join(term.output))

    def test_naive_future_snooze_is_honored_without_error(self) -> None:
        # A valid but offset-NAIVE ISO snooze in the future parses fine, then the
        # `now < until` comparison must not raise (offset-naive vs offset-aware).
        # Assume-UTC preserves the legitimate snooze: no prompt, no scan.
        self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        naive_future = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).replace(tzinfo=None)
        set_scratchpad_snooze_until(self._statefile(), naive_future.isoformat())

        with (
            mock.patch(
                "claudewheel.scratchpad.scan_scratchpad_dirs",
                autospec=True,
            ) as scan_mock,
            mock.patch(
                "claudewheel.preflight._make_terminal",
                autospec=True,
                side_effect=AssertionError("must not prompt within naive snooze"),
            ),
        ):
            result = _scratchpad_cleanup_run(self._ctx())
        self.assertFalse(result.is_abort)
        scan_mock.assert_not_called()

    def test_corrupt_snooze_value_is_ignored(self) -> None:
        stale = self._make_dir("projStale", age_days=SCRATCHPAD_STALE_DAYS + 5)
        set_scratchpad_snooze_until(self._statefile(), "not-a-timestamp")
        result, _ = self._run_with_keys(["y"], self._ctx())
        self.assertFalse(result.is_abort)
        self.assertFalse(stale.exists())

    # -- fresh / empty ----------------------------------------------------

    def test_fresh_dirs_never_offered(self) -> None:
        fresh = self._make_dir("projFresh", age_days=1)
        with mock.patch(
            "claudewheel.scratchpad.tmp_claude_dir",
            autospec=True,
            return_value=self._root,
        ), mock.patch(
            "claudewheel.preflight._make_terminal",
            autospec=True,
            side_effect=AssertionError("must not prompt for fresh dirs"),
        ):
            result = _scratchpad_cleanup_run(self._ctx())
        self.assertFalse(result.is_abort)
        self.assertTrue(fresh.exists())

    def test_no_stale_no_prompt(self) -> None:
        # Empty scratchpad root -> no stale dirs -> no prompt.
        with mock.patch(
            "claudewheel.scratchpad.tmp_claude_dir",
            autospec=True,
            return_value=self._root,
        ), mock.patch(
            "claudewheel.preflight._make_terminal",
            autospec=True,
            side_effect=AssertionError("must not prompt with nothing stale"),
        ):
            result = _scratchpad_cleanup_run(self._ctx())
        self.assertFalse(result.is_abort)

    # -- partial rmtree failure ------------------------------------------

    def test_rmtree_failure_on_one_dir_still_deletes_others(self) -> None:
        a = self._make_dir("projA", age_days=SCRATCHPAD_STALE_DAYS + 5)
        b = self._make_dir("projB", age_days=SCRATCHPAD_STALE_DAYS + 5)
        real_rmtree = __import__("shutil").rmtree

        def flaky(path, *args, **kwargs):
            if Path(path) == a:
                raise OSError("boom")
            return real_rmtree(path, *args, **kwargs)

        term = FakeTerminal(["y"])
        with (
            mock.patch(
                "claudewheel.scratchpad.tmp_claude_dir",
                autospec=True,
                return_value=self._root,
            ),
            mock.patch(
                "claudewheel.preflight._make_terminal",
                autospec=True,
                return_value=term,
            ),
            mock.patch("claudewheel.preflight.effects.rmtree", new=flaky),
        ):
            result = _scratchpad_cleanup_run(self._ctx())
        # Launch continues; the failing dir survives, the other is gone.
        self.assertFalse(result.is_abort)
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())


if __name__ == "__main__":
    unittest.main()
