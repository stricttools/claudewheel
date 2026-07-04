"""Tests for the widget layer (run_form, show_page) and run_selection() in claudewheel.ui."""

from __future__ import annotations

import signal
import unittest
from unittest import mock

from claudewheel.constants import ALT_SCREEN_ON, CLEAR_SCREEN
from claudewheel.defaults import DEFAULT_THEME_DARK
from claudewheel.theme import parse_theme
from claudewheel.ui import FormField, get_field, run_form, run_selection, show_page
from claudewheel import ui as ui_mod

THEME = parse_theme(DEFAULT_THEME_DARK)


class FakeTerminal:
    """A mock Terminal that feeds pre-recorded keystrokes and captures output."""

    def __init__(self, keys: list[str], in_raw: bool = False):
        self._keys = list(keys)
        self._index = 0
        self.rows = 40
        self.cols = 120
        self.output: list[str] = []
        self.enter_raw_calls: list[bool] = []
        self.exit_raw_called = False
        self.closed = False
        self._in_raw = in_raw

    def enter_raw(self, alt_screen: bool = True) -> None:
        self.enter_raw_calls.append(alt_screen)
        self._in_raw = True

    def exit_raw(self) -> None:
        self.exit_raw_called = True
        self._in_raw = False

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
    """Patches signal.signal so run_selection needs no real signal handling."""

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
        return run_selection("Pick one", options, THEME, self.term, **kwargs)

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
        self.assertIsNone(run_selection("Pick one", OPTIONS, THEME, term))

    def test_empty_options_returns_none(self) -> None:
        term = FakeTerminal([])
        self.assertIsNone(run_selection("Pick one", [], THEME, term))
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
    """The finally block restores terminal state and the SIGWINCH handler.

    The caller owns the terminal now: run_selection exits raw mode when it
    entered it, but never closes a terminal it didn't create.
    """

    def test_exit_raw_called_but_terminal_not_closed(self) -> None:
        self._run(["ENTER"])
        self.assertTrue(self.term.exit_raw_called)
        self.assertFalse(self.term.closed)

    def test_exit_raw_called_on_cancel(self) -> None:
        self._run(["ESC"])
        self.assertTrue(self.term.exit_raw_called)
        self.assertFalse(self.term.closed)

    def test_sigwinch_installed_before_enter_raw_and_restored(self) -> None:
        self._run(["ENTER"])
        # Six calls: install SIGWINCH/SIGTERM/SIGHUP, restore all three
        self.assertEqual(self.signal_mock.call_count, 6)
        sigwinch_calls = [c for c in self.signal_mock.call_args_list
                          if c.args[0] == signal.SIGWINCH]
        self.assertEqual(len(sigwinch_calls), 2)

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


class SelectionThemingTests(RunSelectionTestBase):
    """run_selection colors are theme-driven -- the old hardcoded ACCENT and
    DIM_CLR module constants are gone."""

    def test_hardcoded_color_constants_removed(self) -> None:
        self.assertFalse(hasattr(ui_mod, "ACCENT"))
        self.assertFalse(hasattr(ui_mod, "DIM_CLR"))

    def test_title_uses_theme_title_fg(self) -> None:
        self._run(["ESC"])
        self.assertIn(THEME.forms_title_fg, self._output())

    def test_focused_option_uses_theme_focus_span(self) -> None:
        self._run(["ESC"])
        self.assertIn(THEME.forms_focus_bg + THEME.forms_focus_fg, self._output())

    def test_hints_use_theme_hint_fg(self) -> None:
        self._run(["ESC"])
        self.assertIn(THEME.forms_hint_fg, self._output())


# ---------------------------------------------------------------------------
# Widget layer: run_form and show_page
# ---------------------------------------------------------------------------


def _sample_fields() -> list[FormField]:
    """A small form covering every field type except select."""
    return [
        FormField("name", "text", label="Name", value=""),
        FormField("path", "readonly", label="Path", value="/some/where"),
        FormField("mode", "radio", label="Mode", value="fast",
                  options=["fast", "slow", "safe"]),
        FormField("verbose", "checkbox", label="Verbose", value=True),
        FormField("go", "button", label="Go"),
    ]


class FormRunnerTestBase(unittest.TestCase):
    """Patches signal.signal so run_form needs no real signal handling."""

    def setUp(self) -> None:
        self._signal_patch = mock.patch(
            "claudewheel.ui.signal.signal",
            return_value=signal.SIG_DFL,
        )
        self.signal_mock = self._signal_patch.start()
        self.addCleanup(self._signal_patch.stop)

    def _run(self, keys: list[str], fields: list[FormField] | None = None,
             in_raw: bool = False, **kwargs) -> dict | None:
        if fields is None:
            fields = _sample_fields()
        self.fields = fields
        self.term = FakeTerminal(keys, in_raw=in_raw)
        return run_form("Sample form", fields, THEME, self.term, **kwargs)

    def _output(self) -> str:
        return "".join(self.term.output)


class FormSubmitTests(FormRunnerTestBase):
    """ENTER submits from text and button fields and returns all values."""

    def test_enter_on_text_submits(self) -> None:
        result = self._run(list("abc") + ["ENTER"])
        self.assertEqual(result["name"], "abc")

    def test_enter_on_button_submits(self) -> None:
        # Name -> Mode -> Verbose -> Go (readonly skipped)
        result = self._run(["TAB", "TAB", "TAB", "ENTER"])
        self.assertIsNotNone(result)

    def test_all_values_returned_including_readonly(self) -> None:
        result = self._run(["ENTER"], fields=[
            FormField("a", "text", label="A", value="x"),
            FormField("b", "readonly", label="B", value="ro"),
            FormField("c", "checkbox", label="C", value=False),
        ])
        self.assertEqual(result, {"a": "x", "b": "ro", "c": False})

    def test_enter_on_radio_does_not_submit(self) -> None:
        # TAB to Mode (radio), ENTER is ignored, then ESC cancels
        result = self._run(["TAB", "ENTER", "ESC"])
        self.assertIsNone(result)

    def test_enter_on_checkbox_does_not_submit(self) -> None:
        result = self._run(["TAB", "TAB", "ENTER", "ESC"])
        self.assertIsNone(result)

    def test_no_focusable_fields_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._run([], fields=[FormField("r", "readonly", label="R", value="v")])


class FormCancelTests(FormRunnerTestBase):
    """ESC, CTRL_C, and KeyboardInterrupt cancel the form."""

    def test_esc_returns_none(self) -> None:
        self.assertIsNone(self._run(["ESC"]))

    def test_ctrl_c_returns_none(self) -> None:
        self.assertIsNone(self._run(["CTRL_C"]))

    def test_keyboard_interrupt_returns_none(self) -> None:
        term = FakeTerminal([])
        term.read_key = mock.Mock(side_effect=KeyboardInterrupt)
        fields = _sample_fields()
        self.assertIsNone(run_form("Sample", fields, THEME, term))


class FormTraversalTests(FormRunnerTestBase):
    """TAB/SHIFT_TAB/UP/DOWN traverse focusable fields, skipping readonly."""

    def test_tab_skips_readonly(self) -> None:
        # TAB from Name lands on Mode (radio), where RIGHT cycles the value
        result = self._run(["TAB", "RIGHT", "SHIFT_TAB"] + list("n") + ["ENTER"])
        self.assertEqual(result["mode"], "slow")

    def test_down_and_up_traverse(self) -> None:
        # DOWN from Name to Mode, cycle, UP back to Name, type, submit
        result = self._run(["DOWN", "RIGHT", "UP"] + list("x") + ["ENTER"])
        self.assertEqual(result["mode"], "slow")
        self.assertEqual(result["name"], "x")

    def test_tab_wraps_forward(self) -> None:
        # 4 TABs: Name -> Mode -> Verbose -> Go -> Name (wrap)
        result = self._run(["TAB", "TAB", "TAB", "TAB"] + list("w") + ["ENTER"])
        self.assertEqual(result["name"], "w")

    def test_shift_tab_wraps_backward(self) -> None:
        # SHIFT_TAB from Name wraps to Go (button); ENTER submits
        result = self._run(list("z") + ["SHIFT_TAB", "ENTER"])
        self.assertEqual(result["name"], "z")


class FormRadioTests(FormRunnerTestBase):
    """LEFT/RIGHT/SPACE cycle radio options with wrapping."""

    def test_right_cycles_forward(self) -> None:
        result = self._run(["TAB", "RIGHT", "SHIFT_TAB"] + list("a") + ["ENTER"])
        self.assertEqual(result["mode"], "slow")

    def test_left_cycles_backward_with_wrap(self) -> None:
        result = self._run(["TAB", "LEFT", "SHIFT_TAB"] + list("a") + ["ENTER"])
        self.assertEqual(result["mode"], "safe")

    def test_space_cycles_forward(self) -> None:
        result = self._run(["TAB", " ", " ", "SHIFT_TAB"] + list("a") + ["ENTER"])
        self.assertEqual(result["mode"], "safe")


class FormCheckboxTests(FormRunnerTestBase):
    """SPACE toggles checkbox fields."""

    def test_space_toggles_off(self) -> None:
        result = self._run(["TAB", "TAB", " ", "SHIFT_TAB", "SHIFT_TAB"]
                           + list("a") + ["ENTER"])
        self.assertFalse(result["verbose"])

    def test_space_toggles_back_on(self) -> None:
        result = self._run(["TAB", "TAB", " ", " ", "SHIFT_TAB", "SHIFT_TAB"]
                           + list("a") + ["ENTER"])
        self.assertTrue(result["verbose"])


class FormTextEditTests(FormRunnerTestBase):
    """Text fields accept printable characters, spaces, and backspace."""

    def test_typing_appends(self) -> None:
        result = self._run(list("hello") + ["ENTER"])
        self.assertEqual(result["name"], "hello")

    def test_backspace_deletes(self) -> None:
        result = self._run(list("hey") + ["BACKSPACE"] + ["ENTER"])
        self.assertEqual(result["name"], "he")

    def test_backspace_on_empty_is_safe(self) -> None:
        result = self._run(["BACKSPACE"] + list("ok") + ["ENTER"])
        self.assertEqual(result["name"], "ok")

    def test_space_appends_to_text(self) -> None:
        result = self._run(list("a b") + ["ENTER"])
        self.assertEqual(result["name"], "a b")

    def test_on_change_updates_dependent_field(self) -> None:
        def sync(fields: list[FormField]) -> None:
            get_field(fields, "echo").value = get_field(fields, "src").value

        fields = [
            FormField("src", "text", label="Src", value="", on_change=sync),
            FormField("echo", "readonly", label="Echo", value=""),
        ]
        result = self._run(list("ab") + ["BACKSPACE", "ENTER"], fields=fields)
        self.assertEqual(result["echo"], "a")


class FormVisibilityTests(FormRunnerTestBase):
    """Conditional visibility hides fields from rendering and traversal."""

    def _toggle_fields(self) -> list[FormField]:
        def expanded(fields: list[FormField]) -> bool:
            return get_field(fields, "adv").value == "show"

        return [
            FormField("adv", "radio", label="Advanced", value="hide",
                      options=["hide", "show"]),
            FormField("extra", "checkbox", label="Extra", value=True,
                      visible=expanded),
            FormField("go", "button", label="Go"),
        ]

    def test_hidden_field_skipped_in_traversal(self) -> None:
        # Collapsed: TAB from adv goes straight to Go
        result = self._run(["TAB", "ENTER"], fields=self._toggle_fields())
        self.assertTrue(result["extra"])  # untouched default

    def test_hidden_field_not_rendered(self) -> None:
        self._run(["ESC"], fields=self._toggle_fields())
        self.assertNotIn("Extra", self._output())

    def test_expanding_reveals_field(self) -> None:
        # Expand, TAB to Extra, toggle off, TAB to Go, submit
        result = self._run(["RIGHT", "TAB", " ", "TAB", "ENTER"],
                           fields=self._toggle_fields())
        self.assertFalse(result["extra"])

    def test_expanded_field_rendered(self) -> None:
        self._run(["RIGHT", "ESC"], fields=self._toggle_fields())
        self.assertIn("Extra", self._output())

    def test_hidden_field_value_still_returned(self) -> None:
        result = self._run(["TAB", "ENTER"], fields=self._toggle_fields())
        self.assertIn("extra", result)


class FormValidationTests(FormRunnerTestBase):
    """The validate hook blocks submits and shows a themed error."""

    def _validate(self, fields: list[FormField]) -> str | None:
        return "name required" if not get_field(fields, "name").value else None

    def test_failed_validation_blocks_submit(self) -> None:
        result = self._run(["ENTER", "ESC"], validate=self._validate)
        self.assertIsNone(result)

    def test_error_rendered_with_error_color(self) -> None:
        self._run(["ENTER", "ESC"], validate=self._validate)
        out = self._output()
        self.assertIn("name required", out)
        self.assertIn(THEME.forms_error_fg, out)

    def test_fix_then_submit_succeeds(self) -> None:
        result = self._run(["ENTER"] + list("ok") + ["ENTER"],
                           validate=self._validate)
        self.assertEqual(result["name"], "ok")

    def test_error_cleared_on_typing(self) -> None:
        self._run(["ENTER", "x", "ESC"], validate=self._validate)
        # Last frame (after typing "x") must not contain the error text
        last_frame = self.term.output[-1]
        self.assertNotIn("name required", last_frame)


class FormThemingTests(FormRunnerTestBase):
    """All colors come from the theme's forms_* fields."""

    def test_title_uses_title_fg(self) -> None:
        self._run(["ESC"])
        self.assertIn(THEME.forms_title_fg, self._output())

    def test_focused_field_uses_focus_span(self) -> None:
        self._run(["ESC"])
        out = self._output()
        self.assertIn(THEME.forms_focus_bg + THEME.forms_focus_fg, out)

    def test_text_cursor_uses_cursor_fg(self) -> None:
        self._run(["ESC"])
        self.assertIn(THEME.forms_cursor_fg + "_", self._output())

    def test_hints_use_hint_fg_and_are_lowercase(self) -> None:
        self._run(["ESC"])
        out = self._output()
        self.assertIn(THEME.forms_hint_fg, out)
        self.assertIn("esc: cancel", out)

    def test_readonly_field_uses_forms_readonly_fg(self) -> None:
        """Readonly field values use the themed readonly color, not bare DIM."""
        from claudewheel.constants import DIM
        self._run(["ESC"])
        out = self._output()
        # The readonly value "/some/where" should be styled with forms_readonly_fg
        self.assertIn(THEME.forms_readonly_fg + "/some/where", out)
        # Bare DIM must not appear in text-rendering context
        # (DIM followed by a printable character would indicate bare usage)
        for i, _ in enumerate(out):
            if out[i:i + len(DIM)] == DIM:
                # After DIM, the next non-escape char should not be a
                # printable letter/slash (which would indicate value text)
                after = out[i + len(DIM):i + len(DIM) + 1]
                self.assertNotIn(after, "/abcdefghijklmnopqrstuvwxyz",
                                 "Bare DIM used for text rendering")

    def test_selected_radio_uses_bold_with_themed_fg(self) -> None:
        """The selected radio option uses BOLD + themed field color, not bare BOLD."""
        from claudewheel.constants import BOLD as BOLD_SEQ
        self._run(["ESC"])
        out = self._output()
        # The selected radio "fast" should be rendered with BOLD + forms_field_fg
        self.assertIn(BOLD_SEQ + THEME.forms_field_fg + "(*) fast", out)


class FormTerminalSemanticsTests(FormRunnerTestBase):
    """Borrowed vs owned terminal handling."""

    def test_owned_cooked_terminal_gets_raw_cycle(self) -> None:
        self._run(["ESC"])
        self.assertEqual(self.term.enter_raw_calls, [True])
        self.assertTrue(self.term.exit_raw_called)

    def test_owned_terminal_never_closed(self) -> None:
        self._run(["ESC"])
        self.assertFalse(self.term.closed)

    def test_owned_inline_mode_enters_raw_without_alt_screen(self) -> None:
        self._run(["ESC"], use_alt_screen=False)
        self.assertEqual(self.term.enter_raw_calls, [False])

    def test_borrowed_raw_terminal_untouched(self) -> None:
        self._run(["ESC"], in_raw=True)
        self.assertEqual(self.term.enter_raw_calls, [])
        self.assertFalse(self.term.exit_raw_called)
        self.assertFalse(self.term.closed)

    def test_borrowed_mode_renders_fullscreen_despite_inline_flag(self) -> None:
        self._run(["ESC"], in_raw=True, use_alt_screen=False)
        self.assertIn(CLEAR_SCREEN, self._output())

    def test_borrowed_mode_still_swaps_sigwinch(self) -> None:
        self._run(["ESC"], in_raw=True)
        # Six calls: install SIGWINCH/SIGTERM/SIGHUP, restore all three
        self.assertEqual(self.signal_mock.call_count, 6)
        sigwinch_calls = [c for c in self.signal_mock.call_args_list
                          if c.args[0] == signal.SIGWINCH]
        self.assertEqual(len(sigwinch_calls), 2)

    def test_inline_mode_emits_no_clear_screen(self) -> None:
        self._run(["ESC"], use_alt_screen=False)
        self.assertNotIn(CLEAR_SCREEN, self._output())

    def test_inline_form_erased_on_exit(self) -> None:
        self._run(["ENTER"], use_alt_screen=False, fields=[
            FormField("a", "text", label="A", value="x"),
        ])
        self.assertIn("\x1b[0J", self.term.output[-1])

    def test_resize_handler_rerenders(self) -> None:
        self._run(["ESC"])
        handler = self.signal_mock.call_args_list[0].args[1]
        writes_before = len(self.term.output)
        handler(signal.SIGWINCH, None)
        self.assertGreater(len(self.term.output), writes_before)


class SelectFieldTests(FormRunnerTestBase):
    """The select field type: UP/DOWN cycle options, ENTER submits."""

    def _select_fields(self) -> list[FormField]:
        return [FormField("choice", "select", value="a",
                          options=[("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")])]

    def test_enter_returns_focused_key(self) -> None:
        result = self._run(["ENTER"], fields=self._select_fields())
        self.assertEqual(result["choice"], "a")

    def test_down_cycles_and_wraps(self) -> None:
        result = self._run(["DOWN", "DOWN", "DOWN", "DOWN", "ENTER"],
                           fields=self._select_fields())
        self.assertEqual(result["choice"], "b")

    def test_up_wraps_to_last(self) -> None:
        result = self._run(["UP", "ENTER"], fields=self._select_fields())
        self.assertEqual(result["choice"], "c")

    def test_unrelated_keys_ignored(self) -> None:
        result = self._run(["x", "LEFT", "ENTER"], fields=self._select_fields())
        self.assertEqual(result["choice"], "a")

    def test_focused_option_rendered_with_focus_span(self) -> None:
        self._run(["ESC"], fields=self._select_fields())
        out = self._output()
        self.assertIn(THEME.forms_focus_bg + THEME.forms_focus_fg + "> Alpha", out)


class ShowPageTests(FormRunnerTestBase):
    """show_page renders a fullscreen page and waits for one keypress."""

    def _show(self, keys: list[str], in_raw: bool = False, **kwargs) -> None:
        self.term = FakeTerminal(keys, in_raw=in_raw)
        show_page("Done", ["line one", "line two"], THEME, self.term, **kwargs)

    def test_renders_title_and_lines(self) -> None:
        self._show(["x"])
        out = self._output()
        self.assertIn("Done", out)
        self.assertIn("line one", out)
        self.assertIn("line two", out)

    def test_renders_hint(self) -> None:
        self._show(["x"])
        self.assertIn("press any key to continue", self._output())

    def test_consumes_exactly_one_key(self) -> None:
        term = FakeTerminal(["x", "y"])
        self.term = term
        show_page("Done", ["l"], THEME, term)
        self.assertEqual(term._index, 1)

    def test_owned_terminal_gets_alt_screen_raw_cycle(self) -> None:
        self._show(["x"])
        self.assertEqual(self.term.enter_raw_calls, [True])
        self.assertTrue(self.term.exit_raw_called)
        self.assertFalse(self.term.closed)

    def test_borrowed_terminal_untouched(self) -> None:
        self._show(["x"], in_raw=True)
        self.assertEqual(self.term.enter_raw_calls, [])
        self.assertFalse(self.term.exit_raw_called)

    def test_keyboard_interrupt_tolerated(self) -> None:
        term = FakeTerminal([])
        term.read_key = mock.Mock(side_effect=KeyboardInterrupt)
        self.term = term
        show_page("Done", ["l"], THEME, term)  # must not raise
        self.assertIn("Done", self._output())


class FormSessionSignalTests(unittest.TestCase):
    """_form_session must save/restore SIGTERM and SIGHUP alongside SIGWINCH."""

    def setUp(self) -> None:
        self._signal_patch = mock.patch(
            "claudewheel.ui.signal.signal",
            return_value=signal.SIG_DFL,
        )
        self.signal_mock = self._signal_patch.start()
        self.addCleanup(self._signal_patch.stop)

    def _installed_signals(self) -> list[int]:
        """Return the signal numbers that were passed to signal.signal() calls."""
        return [call.args[0] for call in self.signal_mock.call_args_list]

    def test_sigterm_saved_and_restored_owned(self) -> None:
        """SIGTERM handler is installed on entry and restored on exit (owned mode)."""
        term = FakeTerminal(["ESC"])
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        signals = self._installed_signals()
        # SIGTERM must appear at least twice: install and restore
        self.assertGreaterEqual(signals.count(signal.SIGTERM), 2)

    def test_sighup_saved_and_restored_owned(self) -> None:
        """SIGHUP handler is installed on entry and restored on exit (owned mode)."""
        term = FakeTerminal(["ESC"])
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        signals = self._installed_signals()
        self.assertGreaterEqual(signals.count(signal.SIGHUP), 2)

    def test_sigterm_saved_and_restored_borrowed(self) -> None:
        """SIGTERM handler is installed/restored even in borrowed mode."""
        term = FakeTerminal(["ESC"], in_raw=True)
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        signals = self._installed_signals()
        self.assertGreaterEqual(signals.count(signal.SIGTERM), 2)

    def test_sighup_saved_and_restored_borrowed(self) -> None:
        """SIGHUP handler is installed/restored even in borrowed mode."""
        term = FakeTerminal(["ESC"], in_raw=True)
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        signals = self._installed_signals()
        self.assertGreaterEqual(signals.count(signal.SIGHUP), 2)

    def test_owned_mode_handler_raises_system_exit(self) -> None:
        """In owned mode, the SIGTERM handler calls exit_raw then raises SystemExit."""
        term = FakeTerminal(["ESC"])
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        # Find the SIGTERM handler (first call with SIGTERM)
        handler = None
        for call in self.signal_mock.call_args_list:
            if call.args[0] == signal.SIGTERM and callable(call.args[1]):
                handler = call.args[1]
                break
        self.assertIsNotNone(handler, "SIGTERM handler not installed")
        # Calling the handler should raise SystemExit
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)

    def test_borrowed_mode_handler_raises_system_exit(self) -> None:
        """In borrowed mode, the SIGTERM handler raises SystemExit."""
        term = FakeTerminal(["ESC"], in_raw=True)
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        handler = None
        for call in self.signal_mock.call_args_list:
            if call.args[0] == signal.SIGTERM and callable(call.args[1]):
                handler = call.args[1]
                break
        self.assertIsNotNone(handler, "SIGTERM handler not installed")
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)

    def test_borrowed_mode_handler_does_not_call_exit_raw(self) -> None:
        """In borrowed mode, the handler must NOT call exit_raw (the outer owner does)."""
        term = FakeTerminal(["ESC"], in_raw=True)
        term.exit_raw = mock.Mock()
        run_form("T", [FormField("a", "text", label="A", value="")],
                 THEME, term)
        # exit_raw should not have been called during the form session
        # (borrowed mode never calls exit_raw)
        term.exit_raw.assert_not_called()
        # Now call the SIGTERM handler -- it should also NOT call exit_raw
        handler = None
        for call in self.signal_mock.call_args_list:
            if call.args[0] == signal.SIGTERM and callable(call.args[1]):
                handler = call.args[1]
                break
        self.assertIsNotNone(handler)
        term.exit_raw.reset_mock()
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)
        term.exit_raw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
