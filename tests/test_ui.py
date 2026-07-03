"""Tests for run_selection() in claudewheel.ui."""

from __future__ import annotations

import signal
import unittest
from unittest import mock

from claudewheel.constants import ALT_SCREEN_ON, CLEAR_SCREEN
from claudewheel.ui import run_selection
from claudewheel import ui as ui_mod


class FakeTerminal:
    """A mock Terminal that feeds pre-recorded keystrokes and captures output."""

    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self._index = 0
        self.rows = 40
        self.cols = 120
        self.output: list[str] = []
        self.enter_raw_calls: list[bool] = []
        self.exit_raw_called = False
        self.closed = False

    def enter_raw(self, alt_screen: bool = True) -> None:
        self.enter_raw_calls.append(alt_screen)

    def exit_raw(self) -> None:
        self.exit_raw_called = True

    def close(self) -> None:
        self.closed = True

    def get_size(self) -> tuple[int, int]:
        return self.rows, self.cols

    def read_key(self) -> str:
        if self._index >= len(self._keys):
            # Safety net: if keys are exhausted, cancel the form
            return "ESC"
        key = self._keys[self._index]
        self._index += 1
        return key

    def write(self, text: str) -> None:
        self.output.append(text)

    def flush(self) -> None:
        pass


OPTIONS = [("alpha", "Alpha option"), ("beta", "Beta option"), ("gamma", "Gamma option")]


class RunSelectionTestBase(unittest.TestCase):
    """Patches Terminal and signal.signal so run_selection needs no real TTY."""

    def setUp(self) -> None:
        self._signal_patch = mock.patch(
            "claudewheel.ui.signal.signal",
            return_value=signal.SIG_DFL,
        )
        self.signal_mock = self._signal_patch.start()
        self.addCleanup(self._signal_patch.stop)

    def _run(self, keys: list[str], options=None, **kwargs) -> str | None:
        if options is None:
            options = OPTIONS
        self.term = FakeTerminal(keys)
        with mock.patch("claudewheel.ui.Terminal", return_value=self.term):
            return run_selection("Pick one", options, **kwargs)

    def _output(self) -> str:
        return "".join(self.term.output)


class EnterReturnsKeyTests(RunSelectionTestBase):
    """ENTER returns the focused option's key."""

    def test_enter_returns_first_key(self) -> None:
        result = self._run(["ENTER"])
        self.assertEqual(result, "alpha")

    def test_enter_after_down_returns_second_key(self) -> None:
        result = self._run(["DOWN", "ENTER"])
        self.assertEqual(result, "beta")


class CancelTests(RunSelectionTestBase):
    """ESC and CTRL_C return None."""

    def test_esc_returns_none(self) -> None:
        self.assertIsNone(self._run(["ESC"]))

    def test_ctrl_c_returns_none(self) -> None:
        self.assertIsNone(self._run(["CTRL_C"]))

    def test_esc_after_navigation_returns_none(self) -> None:
        self.assertIsNone(self._run(["DOWN", "DOWN", "ESC"]))

    def test_keyboard_interrupt_returns_none(self) -> None:
        term = FakeTerminal([])
        term.read_key = mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch("claudewheel.ui.Terminal", return_value=term):
            self.assertIsNone(run_selection("Pick one", OPTIONS))

    def test_empty_options_returns_none(self) -> None:
        term = FakeTerminal([])
        with mock.patch("claudewheel.ui.Terminal", return_value=term):
            self.assertIsNone(run_selection("Pick one", []))
        # No terminal work should happen for an empty list
        self.assertEqual(term.enter_raw_calls, [])


class NavigationTests(RunSelectionTestBase):
    """UP/DOWN move focus and wrap at both ends."""

    def test_down_moves_focus(self) -> None:
        self.assertEqual(self._run(["DOWN", "DOWN", "ENTER"]), "gamma")

    def test_down_wraps_to_first(self) -> None:
        self.assertEqual(self._run(["DOWN", "DOWN", "DOWN", "ENTER"]), "alpha")

    def test_up_wraps_to_last(self) -> None:
        self.assertEqual(self._run(["UP", "ENTER"]), "gamma")

    def test_up_then_down_returns_to_start(self) -> None:
        self.assertEqual(self._run(["UP", "DOWN", "ENTER"]), "alpha")

    def test_unrelated_keys_ignored(self) -> None:
        self.assertEqual(self._run(["x", "TAB", "LEFT", "ENTER"]), "alpha")


class InitialKeyTests(RunSelectionTestBase):
    """initial_key pre-focuses the matching option."""

    def test_initial_key_prefocuses(self) -> None:
        self.assertEqual(self._run(["ENTER"], initial_key="beta"), "beta")

    def test_initial_key_last_option(self) -> None:
        self.assertEqual(self._run(["ENTER"], initial_key="gamma"), "gamma")

    def test_missing_initial_key_falls_back_to_first(self) -> None:
        self.assertEqual(self._run(["ENTER"], initial_key="nope"), "alpha")

    def test_none_initial_key_focuses_first(self) -> None:
        self.assertEqual(self._run(["ENTER"], initial_key=None), "alpha")

    def test_navigation_from_initial_key(self) -> None:
        self.assertEqual(self._run(["DOWN", "ENTER"], initial_key="beta"), "gamma")


class AltScreenModeTests(RunSelectionTestBase):
    """Rendering and terminal setup differ between the two screen modes."""

    def test_default_enters_raw_with_alt_screen(self) -> None:
        self._run(["ENTER"])
        self.assertEqual(self.term.enter_raw_calls, [True])

    def test_no_alt_screen_enters_raw_without_alt_screen(self) -> None:
        self._run(["ENTER"], use_alt_screen=False)
        self.assertEqual(self.term.enter_raw_calls, [False])

    def test_alt_screen_mode_emits_clear_screen(self) -> None:
        self._run(["ENTER"])
        self.assertIn(CLEAR_SCREEN, self._output())

    def test_no_alt_screen_mode_emits_no_clear_screen(self) -> None:
        self._run(["DOWN", "ENTER"], use_alt_screen=False)
        out = self._output()
        self.assertNotIn(CLEAR_SCREEN, out)
        self.assertNotIn(ALT_SCREEN_ON, out)

    def test_no_alt_screen_renders_labels(self) -> None:
        self._run(["ENTER"], use_alt_screen=False)
        out = self._output()
        for _key, label in OPTIONS:
            self.assertIn(label, out)


class CleanupTests(RunSelectionTestBase):
    """The finally block restores terminal state and the SIGWINCH handler."""

    def test_exit_raw_and_close_called(self) -> None:
        self._run(["ENTER"])
        self.assertTrue(self.term.exit_raw_called)
        self.assertTrue(self.term.closed)

    def test_exit_raw_and_close_called_on_cancel(self) -> None:
        self._run(["ESC"])
        self.assertTrue(self.term.exit_raw_called)
        self.assertTrue(self.term.closed)

    def test_sigwinch_installed_before_enter_raw_and_restored(self) -> None:
        self._run(["ENTER"])
        # Two calls: install form handler, then restore previous handler
        self.assertEqual(self.signal_mock.call_count, 2)
        for call in self.signal_mock.call_args_list:
            self.assertEqual(call.args[0], signal.SIGWINCH)

    def test_inline_form_erased_on_exit(self) -> None:
        self._run(["ENTER"], use_alt_screen=False)
        # The last write erases the form: cursor-up + clear to end of screen
        last = self.term.output[-1]
        self.assertIn("\x1b[0J", last)

    def test_exhausted_keys_cancel(self) -> None:
        """If the key list runs out, the FakeTerminal returns ESC to cancel."""
        self.assertIsNone(self._run(["DOWN"] * 2))


class ResizeHandlerTests(RunSelectionTestBase):
    """The installed SIGWINCH handler re-renders the form."""

    def test_resize_handler_rerenders(self) -> None:
        self._run(["ENTER"])
        handler = self.signal_mock.call_args_list[0].args[1]
        writes_before = len(self.term.output)
        handler(signal.SIGWINCH, None)
        self.assertGreater(len(self.term.output), writes_before)


class WizardColorImportTests(unittest.TestCase):
    """The wizard shares ui's color constants (moved from wizard to ui)."""

    def test_wizard_uses_ui_colors(self) -> None:
        from claudewheel import wizard as wizard_mod
        self.assertIs(wizard_mod.ACCENT, ui_mod.ACCENT)
        self.assertIs(wizard_mod.DIM_CLR, ui_mod.DIM_CLR)

    def test_color_values(self) -> None:
        self.assertEqual(ui_mod.ACCENT, (107, 138, 255))
        self.assertEqual(ui_mod.DIM_CLR, (136, 136, 136))


if __name__ == "__main__":
    unittest.main()
