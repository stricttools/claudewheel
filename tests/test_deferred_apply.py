"""Tests for Phase 8: deferred application of slow discovery results."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from claudewheel.segment import (
    DiscoveryResult,
    Segment,
    SegmentBar,
    merge_slow_results,
)


def _make_bar(*keys: str) -> SegmentBar:
    """Build a minimal SegmentBar with segments for each key."""
    segments = [
        Segment(key=k, label=k.capitalize(), options=["opt1", "opt2"]) for k in keys
    ]
    return SegmentBar(segments=segments, focus_idx=0)


class ApplySlowDiscoveryDeferralTests(unittest.TestCase):
    """_apply_slow_discovery defers results for the focused segment."""

    def _make_app(self, bar: SegmentBar) -> MagicMock:
        """Build a minimal App-like object with the real methods patched in."""
        from claudewheel.app import App

        app = MagicMock(spec=App)
        app.bar = bar
        app._pending_discovery = {}
        app._slow_state_copy = None
        app.cfg = MagicMock()
        app.cfg.state = {}
        app.cfg.options_def = {}
        # Bind the real methods to our mock
        app._apply_slow_discovery = App._apply_slow_discovery.__get__(app, App)
        app._apply_pending_for_segment = App._apply_pending_for_segment.__get__(
            app, App
        )
        app._defocus = App._defocus.__get__(app, App)
        return app

    def test_focused_segment_results_are_deferred(self) -> None:
        """Results for the focused segment go to _pending_discovery, not applied."""
        bar = _make_bar("version", "model", "profile")
        bar.focus_idx = 0  # "version" is focused
        app = self._make_app(bar)

        dr_version = DiscoveryResult(values=["1.0", "2.0"])
        dr_model = DiscoveryResult(values=["opus", "sonnet"])
        app._slow_results = {"version": dr_version, "model": dr_model}

        with patch("claudewheel.app.merge_slow_results") as mock_merge:
            app._apply_slow_discovery()

            # Focused segment's results stored in pending, not merged
            self.assertIn("version", app._pending_discovery)
            self.assertIs(app._pending_discovery["version"], dr_version)

            # Unfocused segment's results merged immediately
            mock_merge.assert_called_once()
            merged_results = mock_merge.call_args[0][1]
            self.assertIn("model", merged_results)
            self.assertNotIn("version", merged_results)

    def test_focused_segment_marked_has_pending(self) -> None:
        """The focused segment gets has_pending=True when results are deferred."""
        bar = _make_bar("version", "model")
        bar.focus_idx = 0
        app = self._make_app(bar)

        app._slow_results = {"version": DiscoveryResult(values=["1.0"])}

        with patch("claudewheel.app.merge_slow_results"):
            app._apply_slow_discovery()

        self.assertTrue(bar.segments[0].has_pending)
        self.assertFalse(bar.segments[1].has_pending)

    def test_no_focused_results_skips_deferral(self) -> None:
        """When the focused segment has no slow results, nothing is deferred."""
        bar = _make_bar("version", "model")
        bar.focus_idx = 0
        app = self._make_app(bar)

        app._slow_results = {"model": DiscoveryResult(values=["opus"])}

        with patch("claudewheel.app.merge_slow_results") as mock_merge:
            app._apply_slow_discovery()

        self.assertEqual(app._pending_discovery, {})
        self.assertFalse(bar.segments[0].has_pending)
        mock_merge.assert_called_once()

    def test_slow_results_consumed_once(self) -> None:
        """_slow_results is set to None after being consumed."""
        bar = _make_bar("version")
        bar.focus_idx = 0
        app = self._make_app(bar)

        app._slow_results = {"version": DiscoveryResult(values=["1.0"])}

        with patch("claudewheel.app.merge_slow_results"):
            app._apply_slow_discovery()

        self.assertIsNone(app._slow_results)


class ApplyPendingForSegmentTests(unittest.TestCase):
    """_apply_pending_for_segment applies deferred results and clears pending."""

    def _make_app(self, bar: SegmentBar) -> MagicMock:
        from claudewheel.app import App

        app = MagicMock(spec=App)
        app.bar = bar
        app._pending_discovery = {}
        app.cfg = MagicMock()
        app.cfg.state = {}
        app.cfg.options_def = {}
        app._apply_pending_for_segment = App._apply_pending_for_segment.__get__(
            app, App
        )
        return app

    def test_applies_and_clears_pending(self) -> None:
        """Buffered results are applied and removed from _pending_discovery."""
        bar = _make_bar("version")
        seg = bar.segments[0]
        seg.has_pending = True
        app = self._make_app(bar)

        dr = DiscoveryResult(values=["3.0", "4.0"], installed={"3.0"})
        app._pending_discovery = {"version": dr}

        with patch("claudewheel.app.merge_slow_results") as mock_merge:
            app._apply_pending_for_segment(seg)

        mock_merge.assert_called_once()
        merged_results = mock_merge.call_args[0][1]
        self.assertIn("version", merged_results)
        self.assertIs(merged_results["version"], dr)

        self.assertNotIn("version", app._pending_discovery)
        self.assertFalse(seg.has_pending)

    def test_noop_when_no_pending(self) -> None:
        """Does nothing when the segment has no pending results."""
        bar = _make_bar("version")
        seg = bar.segments[0]
        app = self._make_app(bar)

        with patch("claudewheel.app.merge_slow_results") as mock_merge:
            app._apply_pending_for_segment(seg)

        mock_merge.assert_not_called()
        self.assertFalse(seg.has_pending)

    def test_only_target_segment_cleared(self) -> None:
        """Applying pending for one segment does not affect another's pending."""
        bar = _make_bar("version", "model")
        app = self._make_app(bar)

        dr_version = DiscoveryResult(values=["1.0"])
        dr_model = DiscoveryResult(values=["opus"])
        app._pending_discovery = {"version": dr_version, "model": dr_model}
        bar.segments[0].has_pending = True
        bar.segments[1].has_pending = True

        with patch("claudewheel.app.merge_slow_results"):
            app._apply_pending_for_segment(bar.segments[0])

        self.assertNotIn("version", app._pending_discovery)
        self.assertIn("model", app._pending_discovery)
        self.assertFalse(bar.segments[0].has_pending)
        self.assertTrue(bar.segments[1].has_pending)


class DefocusTests(unittest.TestCase):
    """_defocus applies pending, clears search and freeform state."""

    def _make_app(self, bar: SegmentBar) -> MagicMock:
        from claudewheel.app import App

        app = MagicMock(spec=App)
        app.bar = bar
        app._pending_discovery = {}
        app.cfg = MagicMock()
        app.cfg.state = {}
        app.cfg.options_def = {}
        app._apply_pending_for_segment = App._apply_pending_for_segment.__get__(
            app, App
        )
        app._defocus = App._defocus.__get__(app, App)
        return app

    def test_defocus_clears_search_and_freeform(self) -> None:
        """Defocus clears search_buffer and _freeform_editing on the focused segment."""
        bar = _make_bar("version")
        seg = bar.segments[0]
        seg.search_buffer = "hello"
        seg._freeform_editing = True
        app = self._make_app(bar)

        with patch("claudewheel.app.merge_slow_results"):
            app._defocus()

        self.assertEqual(seg.search_buffer, "")
        self.assertFalse(seg._freeform_editing)

    def test_defocus_applies_pending(self) -> None:
        """Defocus applies pending discovery results for the focused segment."""
        bar = _make_bar("version")
        seg = bar.segments[0]
        seg.has_pending = True
        app = self._make_app(bar)

        dr = DiscoveryResult(values=["1.0"])
        app._pending_discovery = {"version": dr}

        with patch("claudewheel.app.merge_slow_results") as mock_merge:
            app._defocus()

        mock_merge.assert_called_once()
        self.assertFalse(seg.has_pending)
        self.assertNotIn("version", app._pending_discovery)


class PendingIndicatorTests(unittest.TestCase):
    """The renderer shows a pending indicator (*) in the label."""

    def test_pending_marker_in_label(self) -> None:
        """When has_pending is True, the label includes '*' in the layout."""
        from claudewheel.renderer import Renderer
        from claudewheel.terminal import Terminal
        from claudewheel.theme import parse_theme

        bar = _make_bar("version", "model")
        bar.segments[0].has_pending = True

        term = MagicMock(spec=Terminal)
        term.rows = 24
        term.cols = 120
        theme = parse_theme({})
        renderer = Renderer(term, theme)

        layout, _ = renderer._compute_bar_layout(bar)
        # First segment has pending -> label includes "*"
        self.assertIn("*", layout[0]["label_str"])
        # Second segment does not
        self.assertNotIn("*", layout[1]["label_str"])

    def test_no_pending_no_marker(self) -> None:
        """When has_pending is False, no '*' in the label."""
        from claudewheel.renderer import Renderer
        from claudewheel.terminal import Terminal
        from claudewheel.theme import parse_theme

        bar = _make_bar("version")
        term = MagicMock(spec=Terminal)
        term.rows = 24
        term.cols = 120
        theme = parse_theme({})
        renderer = Renderer(term, theme)

        layout, _ = renderer._compute_bar_layout(bar)
        self.assertNotIn("*", layout[0]["label_str"])

    def test_pending_marker_not_shown_when_focused(self) -> None:
        """The pending marker is still shown when focused (signals new data)."""
        from claudewheel.renderer import Renderer
        from claudewheel.terminal import Terminal
        from claudewheel.theme import parse_theme

        bar = _make_bar("version")
        bar.focus_idx = 0
        bar.segments[0].has_pending = True

        term = MagicMock(spec=Terminal)
        term.rows = 24
        term.cols = 120
        theme = parse_theme({})
        renderer = Renderer(term, theme)

        layout, _ = renderer._compute_bar_layout(bar)
        # Even when focused, the pending marker shows
        self.assertIn("*", layout[0]["label_str"])


class MergeSlowResultsDirectTests(unittest.TestCase):
    """Verify merge_slow_results works correctly as a building block."""

    def test_merge_updates_discovered(self) -> None:
        """merge_slow_results sets discovered options on matching segments."""
        bar = _make_bar("version")
        state = {}
        dr = DiscoveryResult(values=["1.0", "2.0"])
        merge_slow_results(bar, {"version": dr}, state)
        self.assertEqual(bar.segments[0].state._discovered, ["1.0", "2.0"])

    def test_merge_updates_installed(self) -> None:
        """merge_slow_results sets the installed set on matching segments."""
        bar = _make_bar("version")
        state = {}
        dr = DiscoveryResult(values=["1.0"], installed={"1.0"})
        merge_slow_results(bar, {"version": dr}, state)
        self.assertTrue(bar.segments[0].state.is_installed("1.0"))

    def test_merge_preserves_selection(self) -> None:
        """merge_slow_results restores the previous selection after updating options."""
        bar = _make_bar("version")
        bar.segments[0].select_value("opt1")
        state = {}
        dr = DiscoveryResult(values=["opt1", "opt3", "opt4"])
        merge_slow_results(bar, {"version": dr}, state)
        self.assertEqual(bar.segments[0].selected_value, "opt1")


if __name__ == "__main__":
    unittest.main()
