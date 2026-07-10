"""Red-green parity tests for keybinding registry dispatch.

These tests exercise every key x mode x state combination against the
_handle_key contract, ensuring the registry-based dispatch produces
identical behavior to the old match/case implementation.
"""

from __future__ import annotations

import unittest
from unittest import mock

from claudewheel.app import App
from claudewheel.segment import Segment, SegmentBar


def _make_segment(
    key: str = "model",
    label: str = "Model",
    options: list[str] | None = None,
    searchable: bool = False,
    freeform: bool = False,
    creatable: bool = False,
    tab_advances: bool = True,
) -> Segment:
    """Build a segment with useful defaults for testing."""
    seg = Segment(
        key=key,
        label=label,
        options=options or ["opus", "sonnet"],
        searchable=searchable,
        freeform=freeform,
        creatable=creatable,
        tab_advances=tab_advances,
    )
    return seg


def _make_profile_segment(
    discovered: list[str] | None = None,
    pinned: list[str] | None = None,
) -> Segment:
    """Build a profile segment for testing."""
    seg = Segment(key="profile", label="Profile", creatable=True)
    seg.state.collection_order = ["pinned", "discovered"]
    if discovered:
        seg.state.set_discovered(discovered)
    if pinned:
        for p in pinned:
            seg.state.add_pinned(p)
    return seg


def _make_app(seg: Segment, extra_segments: list[Segment] | None = None,
              focus_idx: int = 0) -> App:
    """Build a minimal App with a real _handle_key and seg focused."""
    app = object.__new__(App)
    app.terminal = mock.MagicMock()
    app.theme = mock.MagicMock()
    app.renderer = mock.MagicMock()
    app.cfg = mock.MagicMock()
    app.cfg.state = {}
    segments = [seg] + (extra_segments or [])
    app.bar = SegmentBar(segments=segments, focus_idx=focus_idx)
    app._flash = ""
    app._show_provenance = False
    app._pending_discovery = {}
    app._bindings = app._build_bindings()
    return app


# ===========================================================================
# MAIN MODE: Navigation keys
# ===========================================================================


class MainModeNavigationTests(unittest.TestCase):
    """LEFT, RIGHT, SHIFT_TAB, UP, DOWN navigation in main mode."""

    def test_left_moves_focus_left(self):
        seg1 = _make_segment(key="a", label="A")
        seg2 = _make_segment(key="b", label="B")
        app = _make_app(seg1, extra_segments=[seg2], focus_idx=1)
        result = app._handle_key("LEFT")
        self.assertIsNone(result)
        self.assertEqual(app.bar.focus_idx, 0)

    def test_shift_tab_moves_focus_left(self):
        seg1 = _make_segment(key="a", label="A")
        seg2 = _make_segment(key="b", label="B")
        app = _make_app(seg1, extra_segments=[seg2], focus_idx=1)
        result = app._handle_key("SHIFT_TAB")
        self.assertIsNone(result)
        self.assertEqual(app.bar.focus_idx, 0)

    def test_right_moves_focus_right(self):
        seg1 = _make_segment(key="a", label="A")
        seg2 = _make_segment(key="b", label="B")
        app = _make_app(seg1, extra_segments=[seg2])
        result = app._handle_key("RIGHT")
        self.assertIsNone(result)
        self.assertEqual(app.bar.focus_idx, 1)

    def test_up_cycles_selection_up(self):
        seg = _make_segment(options=["a", "b", "c"])
        seg.select_value("b")
        app = _make_app(seg)
        app._handle_key("UP")
        self.assertEqual(seg.selected_value, "a")

    def test_down_cycles_selection_down(self):
        seg = _make_segment(options=["a", "b", "c"])
        seg.select_value("a")
        app = _make_app(seg)
        app._handle_key("DOWN")
        self.assertEqual(seg.selected_value, "b")

    def test_up_clears_search_buffer(self):
        seg = _make_segment(searchable=True)
        seg.search_buffer = "op"
        app = _make_app(seg)
        app._handle_key("UP")
        self.assertEqual(seg.search_buffer, "")

    def test_down_clears_search_buffer(self):
        seg = _make_segment(searchable=True)
        seg.search_buffer = "op"
        app = _make_app(seg)
        app._handle_key("DOWN")
        self.assertEqual(seg.search_buffer, "")

    def test_up_clears_freeform_editing(self):
        seg = _make_segment(freeform=True)
        seg._freeform_editing = True
        seg.search_buffer = ""  # Not in freeform handler mode (no buffer)
        app = _make_app(seg)
        app._handle_key("UP")
        self.assertFalse(seg._freeform_editing)

    def test_down_clears_freeform_editing(self):
        seg = _make_segment(freeform=True)
        seg._freeform_editing = True
        seg.search_buffer = ""
        app = _make_app(seg)
        app._handle_key("DOWN")
        self.assertFalse(seg._freeform_editing)


# ===========================================================================
# MAIN MODE: ENTER key
# ===========================================================================


class MainModeEnterTests(unittest.TestCase):
    """ENTER in main mode: launch, install, create, wizard."""

    def test_enter_launches_when_valid(self):
        seg = _make_segment()
        seg.select_value("opus")
        app = _make_app(seg)
        result = app._handle_key("ENTER")
        self.assertEqual(result, "launch")

    def test_enter_flashes_missing_required(self):
        seg = _make_segment()
        seg.required = True
        seg.selected_value = None
        app = _make_app(seg)
        result = app._handle_key("ENTER")
        self.assertIsNone(result)
        self.assertIn("Required", app._flash)

    def test_enter_on_plus_starts_creation(self):
        seg = _make_segment(creatable=True)
        seg.selected_value = "+"
        app = _make_app(seg)
        result = app._handle_key("ENTER")
        self.assertIsNone(result)
        self.assertTrue(seg.creating)
        self.assertEqual(seg.create_buffer, "")

    def test_enter_on_profile_plus_launches_wizard(self):
        seg = _make_profile_segment(discovered=["existing"])
        seg.selected_value = "+"
        app = _make_app(seg)
        with mock.patch.object(app, "_launch_profile_wizard", return_value=None) as m:
            result = app._handle_key("ENTER")
        m.assert_called_once_with(seg)
        self.assertIsNone(result)

    def test_enter_triggers_install_for_uninstalled(self):
        seg = _make_segment(options=["1.0.0", "2.0.0"])
        seg.state.set_installed({"1.0.0"})
        seg.select_value("2.0.0")
        app = _make_app(seg)
        with mock.patch.object(app, "_run_install_flow") as m:
            result = app._handle_key("ENTER")
        self.assertIsNone(result)
        m.assert_called_once_with(seg, "2.0.0")

    def test_enter_flashes_unavailable(self):
        seg = _make_segment()
        seg.select_value("opus")
        seg.unavailable = {"opus"}
        app = _make_app(seg)
        result = app._handle_key("ENTER")
        self.assertIsNone(result)
        self.assertIn("not available", app._flash)


# ===========================================================================
# MAIN MODE: TAB key
# ===========================================================================


class MainModeTabTests(unittest.TestCase):
    """TAB in main mode: accept search, advance focus."""

    def test_tab_accepts_fuzzy_match(self):
        seg = _make_segment(searchable=True, options=["opus", "sonnet"])
        seg.search_buffer = "op"
        app = _make_app(seg)
        app._handle_key("TAB")
        self.assertEqual(seg.selected_value, "opus")
        self.assertEqual(seg.search_buffer, "")

    def test_tab_advances_focus(self):
        seg1 = _make_segment(key="a", label="A", tab_advances=True)
        seg2 = _make_segment(key="b", label="B")
        app = _make_app(seg1, extra_segments=[seg2])
        app._handle_key("TAB")
        self.assertEqual(app.bar.focus_idx, 1)

    def test_tab_on_profile_plus_launches_wizard(self):
        seg = _make_profile_segment(discovered=["existing"])
        seg.selected_value = "+"
        app = _make_app(seg)
        with mock.patch.object(app, "_launch_profile_wizard", return_value=None) as m:
            app._handle_key("TAB")
        m.assert_called_once_with(seg)

    def test_tab_on_plus_starts_creation(self):
        seg = _make_segment(creatable=True)
        seg.selected_value = "+"
        app = _make_app(seg)
        app._handle_key("TAB")
        self.assertTrue(seg.creating)


# ===========================================================================
# MAIN MODE: BACKSPACE
# ===========================================================================


class MainModeBackspaceTests(unittest.TestCase):
    """BACKSPACE in main mode: search buffer, freeform seed."""

    def test_backspace_removes_from_search_buffer(self):
        seg = _make_segment(searchable=True)
        seg.search_buffer = "op"
        app = _make_app(seg)
        app._handle_key("BACKSPACE")
        self.assertEqual(seg.search_buffer, "o")

    def test_backspace_on_freeform_seeds_from_value(self):
        seg = _make_segment(freeform=True)
        seg.select_value("opus")
        app = _make_app(seg)
        app._handle_key("BACKSPACE")
        self.assertEqual(seg.search_buffer, "opu")
        self.assertTrue(seg._freeform_editing)

    def test_backspace_noop_without_buffer_or_freeform(self):
        seg = _make_segment(searchable=True)
        seg.search_buffer = ""
        app = _make_app(seg)
        result = app._handle_key("BACKSPACE")
        self.assertIsNone(result)
        self.assertEqual(seg.search_buffer, "")


# ===========================================================================
# MAIN MODE: ESC, CTRL_C
# ===========================================================================


class MainModeEscCtrlCTests(unittest.TestCase):
    """ESC clears search, CTRL_C quits."""

    def test_esc_clears_search_buffer(self):
        seg = _make_segment(searchable=True)
        seg.search_buffer = "op"
        app = _make_app(seg)
        result = app._handle_key("ESC")
        self.assertIsNone(result)
        self.assertEqual(seg.search_buffer, "")

    def test_esc_clears_freeform_editing(self):
        seg = _make_segment(freeform=True)
        seg._freeform_editing = True
        seg.search_buffer = ""  # Not in freeform handler
        app = _make_app(seg)
        app._handle_key("ESC")
        self.assertFalse(seg._freeform_editing)

    def test_ctrl_c_returns_quit(self):
        seg = _make_segment()
        app = _make_app(seg)
        result = app._handle_key("CTRL_C")
        self.assertEqual(result, "quit")


# ===========================================================================
# MAIN MODE: CTRL_D / DELETE
# ===========================================================================


class MainModeDeleteTests(unittest.TestCase):
    """CTRL_D/DELETE triggers profile delete on profile segment."""

    def test_ctrl_d_on_profile_with_value_calls_delete_flow(self):
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = _make_app(seg)
        with mock.patch.object(app, "_delete_profile_flow") as m:
            app._handle_key("CTRL_D")
        m.assert_called_once_with(seg)

    def test_delete_on_profile_with_value_calls_delete_flow(self):
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = _make_app(seg)
        with mock.patch.object(app, "_delete_profile_flow") as m:
            app._handle_key("DELETE")
        m.assert_called_once_with(seg)

    def test_ctrl_d_ignored_on_non_profile(self):
        seg = _make_segment()
        seg.select_value("opus")
        app = _make_app(seg)
        with mock.patch.object(app, "_delete_profile_flow") as m:
            result = app._handle_key("CTRL_D")
        m.assert_not_called()
        self.assertIsNone(result)

    def test_ctrl_d_ignored_when_searching(self):
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        seg.search_buffer = "w"
        app = _make_app(seg)
        with mock.patch.object(app, "_delete_profile_flow") as m:
            app._handle_key("CTRL_D")
        m.assert_not_called()

    def test_ctrl_d_ignored_on_plus(self):
        seg = _make_profile_segment(discovered=["work"])
        seg.selected_value = "+"
        app = _make_app(seg)
        with mock.patch.object(app, "_delete_profile_flow") as m:
            app._handle_key("CTRL_D")
        m.assert_not_called()


# ===========================================================================
# MAIN MODE: Printable keys (dual-role regressions)
# ===========================================================================


class MainModePrintableTests(unittest.TestCase):
    """Printable characters: q, ?, i, and search."""

    def test_q_with_empty_search_returns_quit(self):
        """q with empty search buffer quits."""
        seg = _make_segment(searchable=True)
        seg.search_buffer = ""
        app = _make_app(seg)
        result = app._handle_key("q")
        self.assertEqual(result, "quit")

    def test_q_with_active_search_appends_to_buffer(self):
        """q with active search buffer is appended, not quit."""
        seg = _make_segment(searchable=True)
        seg.search_buffer = "o"
        app = _make_app(seg)
        result = app._handle_key("q")
        self.assertIsNone(result)
        self.assertEqual(seg.search_buffer, "oq")

    def test_q_on_non_searchable_returns_quit(self):
        """q on a non-searchable segment quits."""
        seg = _make_segment(searchable=False)
        app = _make_app(seg)
        result = app._handle_key("q")
        self.assertEqual(result, "quit")

    def test_question_mark_empty_buffer_toggles_provenance(self):
        """? with empty search buffer toggles provenance."""
        seg = _make_segment(searchable=True)
        app = _make_app(seg)
        app._show_provenance = False
        app._handle_key("?")
        self.assertTrue(app._show_provenance)

    def test_question_mark_active_search_appends(self):
        """? with active search buffer is appended."""
        seg = _make_segment(searchable=True)
        seg.search_buffer = "x"
        app = _make_app(seg)
        app._show_provenance = False
        app._handle_key("?")
        self.assertFalse(app._show_provenance)
        self.assertEqual(seg.search_buffer, "x?")

    def test_i_on_profile_no_search_value_present_inspects(self):
        """i on profile segment, no search, value present -> inspect called."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        app = _make_app(seg)
        with mock.patch.object(app, "_show_profile_inspect") as m:
            result = app._handle_key("i")
        m.assert_called_once_with(seg)
        self.assertIsNone(result)

    def test_i_on_non_profile_searches(self):
        """i on non-profile searchable segment -> appended to buffer."""
        seg = _make_segment(searchable=True)
        app = _make_app(seg)
        app._handle_key("i")
        self.assertEqual(seg.search_buffer, "i")

    def test_i_on_profile_with_search_buffer_appends(self):
        """i on profile segment with active search -> appended."""
        seg = _make_profile_segment(discovered=["work"])
        seg.select_value("work")
        seg.searchable = True
        seg.search_buffer = "w"
        app = _make_app(seg)
        with mock.patch.object(app, "_show_profile_inspect") as m:
            app._handle_key("i")
        m.assert_not_called()
        self.assertEqual(seg.search_buffer, "wi")

    def test_i_on_profile_no_value_does_not_inspect(self):
        """i on profile segment with no value (None) -> no inspect."""
        seg = _make_profile_segment(discovered=["work"])
        seg.selected_value = None
        seg.searchable = True
        app = _make_app(seg)
        with mock.patch.object(app, "_show_profile_inspect") as m:
            app._handle_key("i")
        m.assert_not_called()
        # Should enter search on searchable segment
        self.assertEqual(seg.search_buffer, "i")

    def test_printable_starts_search_on_searchable(self):
        """Any printable char starts search on a searchable segment."""
        seg = _make_segment(searchable=True)
        app = _make_app(seg)
        app._handle_key("x")
        self.assertEqual(seg.search_buffer, "x")

    def test_printable_on_freeform_seeds_from_value(self):
        """First printable on freeform segment seeds buffer from value."""
        seg = _make_segment(freeform=True, options=["opus", "sonnet"])
        seg.select_value("opus")
        app = _make_app(seg)
        app._handle_key("x")
        self.assertEqual(seg.search_buffer, "opusx")
        self.assertTrue(seg._freeform_editing)

    def test_printable_on_non_searchable_non_freeform_ignored(self):
        """Printable on non-searchable non-freeform segment is swallowed."""
        seg = _make_segment(searchable=False, freeform=False)
        app = _make_app(seg)
        result = app._handle_key("x")
        self.assertIsNone(result)
        self.assertEqual(seg.search_buffer, "")


# ===========================================================================
# CREATING MODE
# ===========================================================================


class CreatingModeTests(unittest.TestCase):
    """Keys in creating mode (seg.creating = True)."""

    def _make_creating_app(self) -> tuple[App, Segment]:
        seg = _make_segment(creatable=True)
        seg.creating = True
        seg.create_buffer = ""
        app = _make_app(seg)
        return app, seg

    def test_printable_appends_to_create_buffer(self):
        app, seg = self._make_creating_app()
        app._handle_key("h")
        self.assertEqual(seg.create_buffer, "h")

    def test_backspace_removes_from_create_buffer(self):
        app, seg = self._make_creating_app()
        seg.create_buffer = "he"
        app._handle_key("BACKSPACE")
        self.assertEqual(seg.create_buffer, "h")

    def test_esc_cancels_creation(self):
        app, seg = self._make_creating_app()
        seg.create_buffer = "hello"
        result = app._handle_key("ESC")
        self.assertIsNone(result)
        self.assertFalse(seg.creating)
        self.assertEqual(seg.create_buffer, "")

    def test_enter_with_text_confirms_creation(self):
        app, seg = self._make_creating_app()
        seg.create_buffer = "new_val"
        with mock.patch.object(app, "_confirm_create") as m:
            result = app._handle_key("ENTER")
        m.assert_called_once_with(seg)
        self.assertIsNone(result)

    def test_enter_with_empty_does_nothing(self):
        app, seg = self._make_creating_app()
        seg.create_buffer = "   "
        with mock.patch.object(app, "_confirm_create") as m:
            result = app._handle_key("ENTER")
        # Empty after strip -> no confirm
        m.assert_not_called()
        self.assertIsNone(result)

    def test_ctrl_c_in_creating_mode_quits(self):
        app, seg = self._make_creating_app()
        seg.create_buffer = "partial"
        result = app._handle_key("CTRL_C")
        self.assertEqual(result, "quit")
        self.assertFalse(seg.creating)
        self.assertEqual(seg.create_buffer, "")


# ===========================================================================
# FREEFORM MODE
# ===========================================================================


class FreeformModeTests(unittest.TestCase):
    """Keys in freeform editing mode (freeform + search_buffer + _freeform_editing)."""

    def _make_freeform_app(self, buffer: str = "opu") -> tuple[App, Segment]:
        seg = _make_segment(freeform=True, options=["opus", "sonnet"])
        seg.search_buffer = buffer
        seg._freeform_editing = True
        app = _make_app(seg)
        return app, seg

    def test_printable_appends_to_buffer(self):
        app, seg = self._make_freeform_app("op")
        app._handle_key("x")
        self.assertEqual(seg.search_buffer, "opx")

    def test_backspace_removes_from_buffer(self):
        app, seg = self._make_freeform_app("opu")
        app._handle_key("BACKSPACE")
        self.assertEqual(seg.search_buffer, "op")

    def test_backspace_exits_freeform_when_empty(self):
        app, seg = self._make_freeform_app("o")
        app._handle_key("BACKSPACE")
        self.assertEqual(seg.search_buffer, "")
        self.assertFalse(seg._freeform_editing)

    def test_enter_submits_freeform_value(self):
        app, seg = self._make_freeform_app("custom_val")
        app._handle_key("ENTER")
        self.assertEqual(seg.selected_value, "custom_val")
        self.assertEqual(seg.search_buffer, "")
        self.assertFalse(seg._freeform_editing)

    def test_enter_with_whitespace_strips(self):
        app, seg = self._make_freeform_app("  hello  ")
        app._handle_key("ENTER")
        self.assertEqual(seg.selected_value, "hello")

    def test_tab_accepts_top_fuzzy_match(self):
        app, seg = self._make_freeform_app("op")
        app._handle_key("TAB")
        self.assertEqual(seg.selected_value, "opus")
        self.assertEqual(seg.search_buffer, "")
        self.assertFalse(seg._freeform_editing)

    def test_esc_cancels_freeform(self):
        app, seg = self._make_freeform_app("partial")
        result = app._handle_key("ESC")
        self.assertIsNone(result)
        self.assertEqual(seg.search_buffer, "")
        self.assertFalse(seg._freeform_editing)

    def test_left_exits_and_moves_focus_left(self):
        seg1 = _make_segment(key="a", label="A", freeform=True)
        seg1.search_buffer = "x"
        seg1._freeform_editing = True
        seg2 = _make_segment(key="b", label="B")
        app = _make_app(seg1, extra_segments=[seg2])
        app._handle_key("LEFT")
        self.assertEqual(seg1.search_buffer, "")
        self.assertFalse(seg1._freeform_editing)

    def test_right_exits_and_moves_focus_right(self):
        seg1 = _make_segment(key="a", label="A", freeform=True)
        seg1.search_buffer = "x"
        seg1._freeform_editing = True
        seg2 = _make_segment(key="b", label="B")
        app = _make_app(seg1, extra_segments=[seg2])
        app._handle_key("RIGHT")
        self.assertEqual(seg1.search_buffer, "")
        self.assertFalse(seg1._freeform_editing)
        self.assertEqual(app.bar.focus_idx, 1)

    def test_ctrl_c_in_freeform_quits(self):
        app, seg = self._make_freeform_app("partial")
        result = app._handle_key("CTRL_C")
        self.assertEqual(result, "quit")
        self.assertEqual(seg.search_buffer, "")
        self.assertFalse(seg._freeform_editing)


# ===========================================================================
# INSTALL MODE
# ===========================================================================


# ===========================================================================
# LEFT clears search buffer and freeform editing via _defocus
# ===========================================================================


class DefocusCleanupTests(unittest.TestCase):
    """LEFT/RIGHT/SHIFT_TAB clear search buffer + freeform state via _defocus."""

    def test_left_clears_search_buffer_on_defocus(self):
        seg = _make_segment(searchable=True)
        seg.search_buffer = "abc"
        app = _make_app(seg)
        app._handle_key("LEFT")
        self.assertEqual(seg.search_buffer, "")

    def test_left_clears_freeform_editing_on_defocus(self):
        seg = _make_segment(freeform=True)
        seg._freeform_editing = True
        seg.search_buffer = ""  # Not in freeform handler path (empty buffer)
        app = _make_app(seg)
        app._handle_key("LEFT")
        self.assertFalse(seg._freeform_editing)


# ===========================================================================
# Edge cases for mode routing
# ===========================================================================


class ModeRoutingTests(unittest.TestCase):
    """Correct mode precedence: creating > freeform > main."""

    def test_creating_takes_precedence_over_freeform(self):
        """Even if freeform conditions are met, creating intercepts."""
        seg = _make_segment(freeform=True, creatable=True)
        seg.creating = True
        seg.create_buffer = ""
        seg.search_buffer = "x"
        seg._freeform_editing = True
        app = _make_app(seg)
        app._handle_key("a")
        # Goes to creating handler
        self.assertEqual(seg.create_buffer, "a")


# ===========================================================================
# CROSS-MODE: Theme switch via Mode 2031 notifications
# ===========================================================================


class ThemeSwitchTests(unittest.TestCase):
    """THEME_DARK / THEME_LIGHT keys dispatch via cross-mode (mode=None) bindings."""

    @staticmethod
    def _stub_theme_loader(app):
        """Wire the app's (mock) store so load_theme returns the default themes.

        _h_theme_switch now reads its theme via ``self.cfg.load_theme(mode)``
        (no direct file access), mirroring production where the store owns theme
        reads. The stub returns the canonical dark/light dicts by mode.
        """
        from claudewheel.defaults import DEFAULT_THEME_DARK, DEFAULT_THEME_LIGHT
        app.cfg.load_theme.side_effect = lambda mode: (
            DEFAULT_THEME_DARK if mode == "dark" else DEFAULT_THEME_LIGHT
        )

    def test_theme_dark_dispatches_in_main_mode(self):
        """THEME_DARK key triggers _h_theme_switch from main mode."""
        seg = _make_segment()
        app = _make_app(seg)
        self._stub_theme_loader(app)
        original_theme = app.theme
        result = app._handle_key("THEME_DARK")
        self.assertIsNone(result)
        # Theme was swapped (no longer the original mock)
        self.assertIsNot(app.theme, original_theme)

    def test_theme_light_dispatches_in_main_mode(self):
        """THEME_LIGHT key triggers _h_theme_switch from main mode."""
        seg = _make_segment()
        app = _make_app(seg)
        self._stub_theme_loader(app)
        original_theme = app.theme
        result = app._handle_key("THEME_LIGHT")
        self.assertIsNone(result)
        self.assertIsNot(app.theme, original_theme)

    def test_theme_dark_dispatches_in_creating_mode(self):
        """THEME_DARK works from creating mode (cross-mode binding)."""
        seg = _make_segment(creatable=True)
        seg.creating = True
        seg.create_buffer = ""
        app = _make_app(seg)
        self._stub_theme_loader(app)
        original_theme = app.theme
        result = app._handle_key("THEME_DARK")
        self.assertIsNone(result)
        self.assertIsNot(app.theme, original_theme)

    def test_theme_light_dispatches_in_freeform_mode(self):
        """THEME_LIGHT works from freeform mode (cross-mode binding)."""
        seg = _make_segment(freeform=True)
        seg.search_buffer = "text"
        seg._freeform_editing = True
        app = _make_app(seg)
        self._stub_theme_loader(app)
        original_theme = app.theme
        result = app._handle_key("THEME_LIGHT")
        self.assertIsNone(result)
        self.assertIsNot(app.theme, original_theme)

    def test_handler_swaps_theme_and_renderer_theme(self):
        """_h_theme_switch updates self.theme and self.renderer.theme."""
        seg = _make_segment()
        app = _make_app(seg)
        self._stub_theme_loader(app)
        from claudewheel.defaults import DEFAULT_THEME_DARK
        from claudewheel.theme import ThemeColors, parse_theme
        app._h_theme_switch("THEME_DARK")
        # Theme was replaced with a ThemeColors from the default dark dict
        self.assertIsInstance(app.theme, ThemeColors)
        expected = parse_theme(DEFAULT_THEME_DARK)
        self.assertEqual(app.theme.separator_char, expected.separator_char)
        # renderer.theme was also updated
        self.assertIs(app.theme, app.renderer.theme)

    def test_handler_updates_cfg_theme(self):
        """_h_theme_switch updates self.cfg.theme with the raw dict."""
        seg = _make_segment()
        app = _make_app(seg)
        self._stub_theme_loader(app)
        app._h_theme_switch("THEME_LIGHT")
        from claudewheel.defaults import DEFAULT_THEME_LIGHT
        self.assertEqual(app.cfg.theme, DEFAULT_THEME_LIGHT)

    def test_handler_does_not_disrupt_creating_mode(self):
        """THEME_DARK in creating mode does not cancel creation."""
        seg = _make_segment(creatable=True)
        seg.creating = True
        seg.create_buffer = "partial"
        app = _make_app(seg)
        self._stub_theme_loader(app)
        app._handle_key("THEME_DARK")
        # Creating mode state is undisturbed
        self.assertTrue(seg.creating)
        self.assertEqual(seg.create_buffer, "partial")


if __name__ == "__main__":
    unittest.main()
