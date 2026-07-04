"""Tests for Phase 9: provenance overlay hotkey and glyph rendering."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claudewheel.app import App
from claudewheel.renderer import PROVENANCE_GLYPHS, Renderer
from claudewheel.segment import Segment, SegmentBar
from claudewheel.terminal import Terminal
from claudewheel.theme import parse_theme


def _make_bar(*keys: str) -> SegmentBar:
    """Build a minimal SegmentBar with segments for each key."""
    segments = [
        Segment(key=k, label=k.capitalize(), options=["opt1", "opt2"])
        for k in keys
    ]
    return SegmentBar(segments=segments, focus_idx=0)


def _make_app_mock(bar: SegmentBar) -> MagicMock:
    """Build a minimal App-like object with real _handle_key bound."""
    app = MagicMock(spec=App)
    app.bar = bar
    app._show_provenance = False
    app._pending_discovery = {}
    app._pending_install = None
    app._pending_install_seg = None
    app._flash = ""
    app.cfg = MagicMock()
    app.cfg.state = {}
    app.cfg.options_def = {}
    app._handle_key = App._handle_key.__get__(app, App)
    app._build_context = App._build_context.__get__(app, App)
    app._bindings = App._build_bindings(app)
    app._defocus = App._defocus.__get__(app, App)
    app._apply_pending_for_segment = App._apply_pending_for_segment.__get__(app, App)
    return app


class ProvenanceToggleTests(unittest.TestCase):
    """The ? key toggles _show_provenance."""

    def test_question_mark_toggles_on(self) -> None:
        """Pressing ? when provenance is off turns it on."""
        bar = _make_bar("model")
        app = _make_app_mock(bar)
        app._handle_key("?")
        self.assertTrue(app._show_provenance)

    def test_question_mark_toggles_off(self) -> None:
        """Pressing ? twice returns provenance to off."""
        bar = _make_bar("model")
        app = _make_app_mock(bar)
        app._handle_key("?")
        app._handle_key("?")
        self.assertFalse(app._show_provenance)

    def test_question_mark_during_search_is_search_char(self) -> None:
        """When search_buffer is active, ? is appended to search instead of toggling."""
        bar = _make_bar("model")
        bar.segments[0].searchable = True
        bar.segments[0].search_buffer = "ab"
        app = _make_app_mock(bar)
        app._handle_key("?")
        # Should NOT toggle provenance
        self.assertFalse(app._show_provenance)
        # Should have appended to search buffer
        self.assertEqual(bar.segments[0].search_buffer, "ab?")

    def test_question_mark_during_creation_is_ignored(self) -> None:
        """When in creation mode, ? goes to create handler, not provenance toggle."""
        bar = _make_bar("model")
        seg = bar.segments[0]
        seg.creating = True
        seg.create_buffer = ""
        app = _make_app_mock(bar)
        app._handle_key("?")
        # Should NOT toggle provenance
        self.assertFalse(app._show_provenance)
        # Should have been handled by create mode
        self.assertEqual(seg.create_buffer, "?")


class ProvenanceGlyphMappingTests(unittest.TestCase):
    """PROVENANCE_GLYPHS maps all four sources correctly."""

    def test_all_sources_have_glyphs(self) -> None:
        """Every source type has a corresponding glyph."""
        self.assertIn("discovered", PROVENANCE_GLYPHS)
        self.assertIn("pinned", PROVENANCE_GLYPHS)
        self.assertIn("defaults", PROVENANCE_GLYPHS)
        self.assertIn("ephemeral", PROVENANCE_GLYPHS)

    def test_correct_glyph_values(self) -> None:
        """Glyphs match the spec."""
        self.assertEqual(PROVENANCE_GLYPHS["discovered"], "*")
        self.assertEqual(PROVENANCE_GLYPHS["pinned"], "^")
        self.assertEqual(PROVENANCE_GLYPHS["defaults"], ".")
        self.assertEqual(PROVENANCE_GLYPHS["ephemeral"], "~")

    def test_all_glyphs_are_single_char(self) -> None:
        """Each glyph is exactly one character."""
        for source, glyph in PROVENANCE_GLYPHS.items():
            with self.subTest(source=source):
                self.assertEqual(len(glyph), 1)


class ProvenanceRenderOptionTests(unittest.TestCase):
    """_render_option prepends provenance glyphs when overlay is active."""

    def _make_renderer(self, show_provenance: bool = False) -> Renderer:
        term = MagicMock(spec=Terminal)
        term.rows = 24
        term.cols = 120
        theme = parse_theme({})
        renderer = Renderer(term, theme)
        renderer._show_provenance = show_provenance
        return renderer

    def test_no_glyph_when_overlay_off(self) -> None:
        """Without provenance overlay, no glyph prefix is emitted."""
        renderer = self._make_renderer(show_provenance=False)
        seg = Segment(key="k", label="K", options=["alpha"])
        seg.state.set_defaults(["alpha"])
        buf: list[str] = []
        renderer._render_option(buf, seg, "alpha", "alpha", "", "")
        joined = "".join(buf)
        # No provenance glyph present
        self.assertNotIn(". ", joined)

    def test_glyph_for_defaults_source(self) -> None:
        """Defaults source gets '.' glyph prefix."""
        renderer = self._make_renderer(show_provenance=True)
        seg = Segment(key="k", label="K", options=["alpha"])
        seg.state.set_defaults(["alpha"])
        buf: list[str] = []
        renderer._render_option(buf, seg, "alpha", "alpha", "", "")
        joined = "".join(buf)
        self.assertIn(". ", joined)

    def test_glyph_for_pinned_source(self) -> None:
        """Pinned source gets '^' glyph prefix."""
        renderer = self._make_renderer(show_provenance=True)
        seg = Segment(key="k", label="K")
        seg.state.add_pinned("beta")
        buf: list[str] = []
        renderer._render_option(buf, seg, "beta", "beta", "", "")
        joined = "".join(buf)
        self.assertIn("^ ", joined)

    def test_glyph_for_discovered_source(self) -> None:
        """Discovered source gets '*' glyph prefix."""
        renderer = self._make_renderer(show_provenance=True)
        seg = Segment(key="k", label="K")
        seg.state.set_discovered(["gamma"])
        buf: list[str] = []
        renderer._render_option(buf, seg, "gamma", "gamma", "", "")
        joined = "".join(buf)
        self.assertIn("* ", joined)

    def test_glyph_for_ephemeral_source(self) -> None:
        """Ephemeral source gets '~' glyph prefix."""
        renderer = self._make_renderer(show_provenance=True)
        seg = Segment(key="k", label="K")
        seg.state.add_ephemeral("delta")
        buf: list[str] = []
        renderer._render_option(buf, seg, "delta", "delta", "", "")
        joined = "".join(buf)
        self.assertIn("~ ", joined)

    def test_no_glyph_for_plus_sentinel(self) -> None:
        """The '+' sentinel never gets a provenance glyph."""
        renderer = self._make_renderer(show_provenance=True)
        seg = Segment(key="k", label="K", options=["a"], creatable=True)
        buf: list[str] = []
        renderer._render_option(buf, seg, "+", "+", "", "")
        joined = "".join(buf)
        # Should not contain any provenance glyph
        for glyph in PROVENANCE_GLYPHS.values():
            self.assertNotIn(glyph + " ", joined)

    def test_display_shortened_by_two(self) -> None:
        """When overlay is active, display text is shortened by 2 chars for the glyph prefix."""
        renderer = self._make_renderer(show_provenance=True)
        seg = Segment(key="k", label="K")
        seg.state.set_defaults(["abcdef"])
        buf: list[str] = []
        renderer._render_option(buf, seg, "abcdef", "abcdef", "", "")
        joined = "".join(buf)
        # The display should be "abcd" (6-2=4 chars), with ". " prefix
        self.assertIn(". ", joined)
        self.assertIn("abcd", joined)
        self.assertNotIn("abcdef", joined)


class ProvenanceStatusBarTests(unittest.TestCase):
    """Status bar shows legend when provenance is active, and ? hint otherwise."""

    def _make_renderer(self, show_provenance: bool = False) -> Renderer:
        term = MagicMock(spec=Terminal)
        term.rows = 24
        term.cols = 120
        theme = parse_theme({})
        renderer = Renderer(term, theme)
        renderer._show_provenance = show_provenance
        return renderer

    def test_legend_shown_when_provenance_active(self) -> None:
        """Status bar shows the provenance legend when overlay is on."""
        renderer = self._make_renderer(show_provenance=True)
        bar = _make_bar("model")
        buf: list[str] = []
        renderer._render_status(buf, bar)
        joined = "".join(buf)
        self.assertIn("* discovered", joined)
        self.assertIn("^ pinned", joined)
        self.assertIn(". default", joined)
        self.assertIn("~ ephemeral", joined)

    def test_question_mark_hint_in_normal_mode(self) -> None:
        """Status bar includes '?: sources' hint in default mode."""
        renderer = self._make_renderer(show_provenance=False)
        bar = _make_bar("model")
        buf: list[str] = []
        renderer._render_status(buf, bar)
        joined = "".join(buf)
        self.assertIn("?: sources", joined)

    def test_question_mark_hint_in_search_idle(self) -> None:
        """Searchable segment without active search shows ? hint."""
        renderer = self._make_renderer(show_provenance=False)
        bar = _make_bar("model")
        bar.segments[0].searchable = True
        buf: list[str] = []
        renderer._render_status(buf, bar)
        joined = "".join(buf)
        self.assertIn("?: sources", joined)

    def test_no_question_mark_hint_during_active_search(self) -> None:
        """During active search (search_buffer non-empty), ? hint is not shown."""
        renderer = self._make_renderer(show_provenance=False)
        bar = _make_bar("model")
        bar.segments[0].searchable = True
        bar.segments[0].search_buffer = "abc"
        buf: list[str] = []
        renderer._render_status(buf, bar)
        joined = "".join(buf)
        self.assertNotIn("?: sources", joined)

    def test_flash_overrides_legend(self) -> None:
        """Flash messages take precedence over the provenance legend."""
        renderer = self._make_renderer(show_provenance=True)
        bar = _make_bar("model")
        buf: list[str] = []
        renderer._render_status(buf, bar, flash="Required: model")
        joined = "".join(buf)
        self.assertIn("Required: model", joined)
        self.assertNotIn("* discovered", joined)


if __name__ == "__main__":
    unittest.main()
