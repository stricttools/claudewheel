"""Regression tests for merge_slow_results.

Tests construct segments with initial discovered values (not defaults) to
match the real build_segment_bar flow where discovered values get replaced
by slow discovery.
"""

from __future__ import annotations

import unittest

from claudewheel.segment import Segment, SegmentBar, SegmentState, merge_slow_results


def _make_segment(key: str, discovered: list[str], selected_idx: int = -1,
                  creatable: bool = False) -> Segment:
    """Build a segment with values seeded as discovered (not defaults).

    This mirrors the real flow: build_segment_bar calls set_discovered()
    with fast-discovery results, then merge_slow_results replaces them.
    Creatable segments also get "+" in ephemeral.
    """
    seg = Segment(
        key=key,
        label=key.title(),
        selected_idx=selected_idx,
        creatable=creatable,
    )
    seg.state.set_discovered(list(discovered))
    if creatable:
        seg.state.add_ephemeral("+")
    return seg


class MergeSlowResultsTests(unittest.TestCase):
    def test_selection_preserved_when_value_exists(self) -> None:
        """If the currently selected value appears in new results, it stays selected."""
        seg = _make_segment("version", ["1.0", "2.0"], selected_idx=1)  # "2.0"
        bar = SegmentBar(segments=[seg])
        results = {"version": ["1.0", "2.0", "3.0"]}
        merge_slow_results(bar, results, {})
        self.assertEqual(seg.value, "2.0")
        self.assertEqual(seg.options, ["1.0", "2.0", "3.0"])

    def test_selection_falls_back_to_last_config(self) -> None:
        """When the selected value disappears, fall back to last_config from state."""
        seg = _make_segment("version", ["1.0", "2.0"], selected_idx=1)  # "2.0"
        bar = SegmentBar(segments=[seg])
        # New results do not contain "2.0"
        results = {"version": ["1.0", "3.0"]}
        state = {"last_config": {"version": "3.0"}}
        merge_slow_results(bar, results, state)
        self.assertEqual(seg.value, "3.0")

    def test_selection_lost_when_no_fallback(self) -> None:
        """When the selected value disappears and no last_config, selection is lost."""
        seg = _make_segment("version", ["1.0", "2.0"], selected_idx=1)  # "2.0"
        bar = SegmentBar(segments=[seg])
        results = {"version": ["1.0", "3.0"]}
        merge_slow_results(bar, results, {})
        # current_value "2.0" is no longer in discovered (and there are no
        # defaults), so select_value("2.0") fails. selected_idx stays at 1,
        # which now points to "3.0" in the new discovered list.
        self.assertEqual(seg.selected_idx, 1)
        self.assertEqual(seg.value, "3.0")

    def test_plus_present_for_creatable_segment(self) -> None:
        """Creatable segments keep '+' from ephemeral after merge."""
        seg = _make_segment("profile", ["default"], selected_idx=0, creatable=True)
        bar = SegmentBar(segments=[seg])
        # Results replace discovered; "+" stays in ephemeral from build time
        results = {"profile": ["default", "work"]}
        merge_slow_results(bar, results, {})
        self.assertIn("+", seg.options)
        self.assertEqual(seg.options, ["default", "work", "+"])

    def test_plus_not_appended_for_non_creatable(self) -> None:
        """Non-creatable segments do NOT get '+' appended."""
        seg = _make_segment("version", ["1.0"], selected_idx=0)
        bar = SegmentBar(segments=[seg])
        results = {"version": ["1.0", "2.0"]}
        merge_slow_results(bar, results, {})
        self.assertNotIn("+", seg.options)

    def test_plus_not_duplicated(self) -> None:
        """Even if results contain '+', it only appears once (ephemeral dedup)."""
        seg = _make_segment("profile", ["default"], selected_idx=0, creatable=True)
        bar = SegmentBar(segments=[seg])
        # Discovery returns "+" in the list, but ephemeral already has it
        results = {"profile": ["default", "work", "+"]}
        merge_slow_results(bar, results, {})
        self.assertEqual(seg.options.count("+"), 1)

    def test_installed_set_updated(self) -> None:
        """The _installed_ sideband key updates the segment's installed set."""
        seg = _make_segment("version", ["1.0"], selected_idx=0)
        bar = SegmentBar(segments=[seg])
        results = {
            "version": ["1.0", "2.0", "3.0"],
            "_installed_version": {"1.0", "2.0"},
        }
        merge_slow_results(bar, results, {})
        self.assertEqual(seg.state._installed, {"1.0", "2.0"})

    def test_segment_without_results_untouched(self) -> None:
        """Segments not present in results keep their original options and selection."""
        seg_a = _make_segment("version", ["1.0", "2.0"], selected_idx=1)
        seg_b = _make_segment("profile", ["default"], selected_idx=0)
        bar = SegmentBar(segments=[seg_a, seg_b])
        # Only version has results
        results = {"version": ["3.0", "4.0"]}
        merge_slow_results(bar, results, {})
        # seg_b should be completely untouched
        self.assertEqual(seg_b.options, ["default"])
        self.assertEqual(seg_b.selected_idx, 0)
        self.assertEqual(seg_b.value, "default")

    def test_no_selection_falls_back_to_last_config(self) -> None:
        """When no value was selected (idx=-1), last_config is used as fallback."""
        seg = _make_segment("version", ["old"], selected_idx=-1)
        bar = SegmentBar(segments=[seg])
        results = {"version": ["1.0", "2.0"]}
        state = {"last_config": {"version": "2.0"}}
        merge_slow_results(bar, results, state)
        self.assertEqual(seg.value, "2.0")

    def test_last_config_not_used_when_current_value_exists(self) -> None:
        """Even if last_config differs, the current selection takes precedence."""
        seg = _make_segment("version", ["1.0", "2.0"], selected_idx=0)  # "1.0"
        bar = SegmentBar(segments=[seg])
        results = {"version": ["1.0", "2.0", "3.0"]}
        state = {"last_config": {"version": "3.0"}}
        merge_slow_results(bar, results, state)
        # current_value "1.0" exists in new results, so it should be selected
        self.assertEqual(seg.value, "1.0")


if __name__ == "__main__":
    unittest.main()
