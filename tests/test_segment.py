"""Smoke tests for Segment dataclass and version_sort_key in claudewheel.segment."""

from __future__ import annotations

import unittest

from claudewheel.segment import Segment, version_sort_key


class SegmentCycleWrapTests(unittest.TestCase):
    """Verify the (n+1)-position ring with wrap=True (the default)."""

    def test_single_option_toggles_with_blank(self) -> None:
        """With one option, cycling alternates between None (blank) and "only"."""
        seg = Segment(key="k", label="K", options=["only"], wrap=True)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)
        # And the same in the other direction
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, 0)
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)

    def test_three_options_forward_includes_blank(self) -> None:
        """Forward cycling traverses [-1, 0, 1, 2] before wrapping back to -1."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=True,
        )
        # Expected ring traversal: -1 -> 0 -> 1 -> 2 -> -1 -> 0 ...
        expected = [0, 1, 2, -1, 0]
        for step, want in enumerate(expected):
            with self.subTest(step=step):
                seg.cycle(+1)
                self.assertEqual(seg.selected_idx, want)

    def test_three_options_backward_includes_blank(self) -> None:
        """Backward cycling traverses [-1, 2, 1, 0] before wrapping back to -1."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=True,
        )
        # Expected ring traversal in reverse: -1 -> 2 -> 1 -> 0 -> -1 -> 2 ...
        expected = [2, 1, 0, -1, 2]
        for step, want in enumerate(expected):
            with self.subTest(step=step):
                seg.cycle(-1)
                self.assertEqual(seg.selected_idx, want)


class SegmentCycleNoWrapTests(unittest.TestCase):
    """Verify wrap=False semantics: blank reachable from BOTH ends, no continuous wrap."""

    def test_forward_from_last_reaches_blank(self) -> None:
        """Going +1 from the last option lands on -1 (blank) -- symmetric with UP-from-first."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=False,
            selected_value="c",
        )
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)

    def test_backward_from_first_reaches_blank(self) -> None:
        """Going -1 from the first option lands on -1 (blank) -- existing behavior preserved."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=False,
            selected_value="a",
        )
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)

    def test_past_blank_stays_at_blank(self) -> None:
        """Once at blank, going further in either direction stays at blank (no continuous wrap)."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=False,
        )
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)
        # And +1 from blank still re-enters the option list at the first option
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)

    def test_forward_past_last_then_again_stays_at_blank(self) -> None:
        """DOWN from last -> blank, then DOWN again re-enters at first."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=False,
            selected_value="c",
        )
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)  # re-enters at first
        # Reset to blank and verify +1 re-enters at first
        seg.selected_value = None
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)

    def test_normal_steps_still_work(self) -> None:
        """Non-boundary cycling still moves one step at a time."""
        seg = Segment(
            key="k",
            label="K",
            options=["a", "b", "c"],
            wrap=False,
            selected_value="a",
        )
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 1)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 2)
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, 1)


class SegmentCycleEmptyTests(unittest.TestCase):
    def test_empty_options_is_noop(self) -> None:
        """With no options, cycle() must not change selected_value or raise."""
        seg = Segment(key="k", label="K", options=[], wrap=True)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)


class SegmentValuePropertyTests(unittest.TestCase):
    def test_value_none_when_blank(self) -> None:
        """value is None whenever selected_value is None."""
        seg = Segment(key="k", label="K", options=["a", "b"])
        self.assertIsNone(seg.value)

    def test_value_none_when_no_options(self) -> None:
        """value is None when selected_value is not in options."""
        seg = Segment(key="k", label="K", options=[], selected_value="x")
        self.assertIsNone(seg.value)

    def test_value_returns_selected_option(self) -> None:
        """value returns the selected_value when it exists in options."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_value="b")
        self.assertEqual(seg.value, "b")


class SegmentSelectValueTests(unittest.TestCase):
    def test_select_existing_value_returns_true(self) -> None:
        """select_value sets selected_value and returns True when the value exists."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"])
        self.assertTrue(seg.select_value("b"))
        self.assertEqual(seg.selected_idx, 1)

    def test_select_missing_value_returns_false_and_keeps_value(self) -> None:
        """select_value returns False and leaves selected_value untouched on miss."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_value="c")
        self.assertFalse(seg.select_value("zzz"))
        self.assertEqual(seg.selected_idx, 2)


class SegmentFilteredOptionsTests(unittest.TestCase):
    def test_no_search_buffer_returns_all(self) -> None:
        """Without a search buffer, filtered_options is the full option list."""
        seg = Segment(key="k", label="K", options=["abc", "xyz", "abd"])
        self.assertEqual(seg.filtered_options, ["abc", "xyz", "abd"])

    def test_with_search_buffer_returns_ranked_subset(self) -> None:
        """A search buffer fuzzy-ranks the options and drops non-matches."""
        seg = Segment(key="k", label="K", options=["abc", "xyz", "abd"])
        seg.search_buffer = "ab"
        result = seg.filtered_options
        self.assertNotIn("xyz", result)
        self.assertEqual(set(result), {"abc", "abd"})


class SegmentValueBasedSelectionTests(unittest.TestCase):
    """Tests specific to value-based selection semantics."""

    def test_cycling_after_option_mutation_preserves_value(self) -> None:
        """When options change but selected value still exists, cycling continues from it."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_value="b")
        # Mutate options: "b" moves from index 1 to index 0
        seg.state.set_defaults(["b", "x", "y"])
        self.assertEqual(seg.value, "b")
        self.assertEqual(seg.selected_idx, 0)
        # Cycling forward from "b" (now idx 0) goes to "x" (idx 1)
        seg.cycle(+1)
        self.assertEqual(seg.value, "x")

    def test_value_none_for_disappeared_option(self) -> None:
        """value returns None when selected_value is no longer in options."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_value="b")
        seg.state.set_defaults(["x", "y", "z"])
        self.assertIsNone(seg.value)

    def test_cycling_when_value_disappeared_starts_from_blank(self) -> None:
        """When selected_value has disappeared, cycling treats it as blank."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_value="b")
        seg.state.set_defaults(["x", "y", "z"])
        # selected_idx is -1 because "b" is not in new options
        self.assertEqual(seg.selected_idx, -1)
        # Cycling forward from blank goes to first option
        seg.cycle(+1)
        self.assertEqual(seg.value, "x")

    def test_computed_selected_idx_matches_position(self) -> None:
        """selected_idx correctly reports the position of selected_value."""
        seg = Segment(key="k", label="K", options=["a", "b", "c", "d"])
        seg.select_value("c")
        self.assertEqual(seg.selected_idx, 2)
        seg.select_value("a")
        self.assertEqual(seg.selected_idx, 0)
        seg.selected_value = None
        self.assertEqual(seg.selected_idx, -1)


class DisplayOptionsTests(unittest.TestCase):
    """Tests for the display_options property (Phase 7: virtual '+')."""

    def test_creatable_appends_plus(self) -> None:
        """Creatable segments have '+' appended to display_options."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True)
        self.assertEqual(seg.display_options, ["a", "b", "+"])
        # Real options do not contain "+"
        self.assertNotIn("+", seg.options)

    def test_non_creatable_no_plus(self) -> None:
        """Non-creatable segments have display_options == options."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=False)
        self.assertEqual(seg.display_options, ["a", "b"])

    def test_creatable_empty_options(self) -> None:
        """Creatable segment with no options has display_options == ['+']."""
        seg = Segment(key="k", label="K", options=[], creatable=True)
        self.assertEqual(seg.display_options, ["+"])

    def test_cycling_visits_plus_and_blank(self) -> None:
        """Cycling on a creatable segment visits all options + '+' + blank."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True, wrap=True)
        # display_options = ["a", "b", "+"], ring size = 4 (None, a, b, +)
        # Starting from blank: +1 -> a, +1 -> b, +1 -> +, +1 -> blank
        expected_values = ["a", "b", "+", None]
        for i, want in enumerate(expected_values):
            with self.subTest(step=i, want=want):
                seg.cycle(+1)
                self.assertEqual(seg.selected_value, want)

    def test_selected_idx_maps_into_display_options(self) -> None:
        """selected_idx is the index in display_options, not options."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True)
        seg.selected_value = "+"
        # "+" is at index 2 in display_options ["a", "b", "+"]
        self.assertEqual(seg.selected_idx, 2)

    def test_selected_idx_for_regular_option(self) -> None:
        """selected_idx for a regular option still works correctly."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True)
        seg.selected_value = "a"
        self.assertEqual(seg.selected_idx, 0)
        seg.selected_value = "b"
        self.assertEqual(seg.selected_idx, 1)

    def test_is_on_plus_true(self) -> None:
        """is_on_plus returns True when selected_value is '+'."""
        seg = Segment(key="k", label="K", options=["a"], creatable=True)
        seg.selected_value = "+"
        self.assertTrue(seg.is_on_plus)

    def test_is_on_plus_false_not_creatable(self) -> None:
        """is_on_plus returns False for non-creatable segments."""
        seg = Segment(key="k", label="K", options=["a"])
        seg.selected_value = "+"
        self.assertFalse(seg.is_on_plus)

    def test_value_none_on_plus(self) -> None:
        """seg.value returns None when selected_value is '+'."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True)
        seg.selected_value = "+"
        self.assertIsNone(seg.value)

    def test_value_returns_real_option(self) -> None:
        """seg.value returns the real option when not on '+'."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True)
        seg.selected_value = "a"
        self.assertEqual(seg.value, "a")

    def test_plus_not_in_filtered_options(self) -> None:
        """'+' is excluded from filtered_options during search."""
        seg = Segment(key="k", label="K", options=["alpha", "beta"], creatable=True)
        seg.search_buffer = "al"
        # Only real options are searched
        filtered = seg.filtered_options
        self.assertNotIn("+", filtered)

    def test_plus_not_in_filtered_options_empty_buffer(self) -> None:
        """With empty search buffer, filtered_options returns options (no '+')."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True)
        # filtered_options without search returns self.options (no "+")
        self.assertEqual(seg.filtered_options, ["a", "b"])
        self.assertNotIn("+", seg.filtered_options)

    def test_cycle_backward_visits_plus(self) -> None:
        """Cycling backward from blank visits '+' first on creatable."""
        seg = Segment(key="k", label="K", options=["a", "b"], creatable=True, wrap=True)
        # From blank, -1 should go to last display option: "+"
        seg.cycle(-1)
        self.assertEqual(seg.selected_value, "+")
        # Then -1 goes to "b"
        seg.cycle(-1)
        self.assertEqual(seg.selected_value, "b")

    def test_non_creatable_cycle_unchanged(self) -> None:
        """Non-creatable segments cycle through options normally (regression check)."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], wrap=True)
        expected = [0, 1, 2, -1, 0]
        for step, want in enumerate(expected):
            with self.subTest(step=step):
                seg.cycle(+1)
                self.assertEqual(seg.selected_idx, want)


class VersionSortKeyTests(unittest.TestCase):
    def test_numeric_ordering_not_alphabetical(self) -> None:
        """2.1.10 must sort *higher* than 2.1.9 (the alphabetical order is the opposite)."""
        self.assertGreater(version_sort_key("2.1.10"), version_sort_key("2.1.9"))

    def test_full_sort_round_trip(self) -> None:
        """Sorting a list of versions with this key gives numeric semver order."""
        versions = ["2.1.2", "2.1.10", "2.1.9", "2.1.100", "1.9.0"]
        result = sorted(versions, key=version_sort_key)
        self.assertEqual(result, ["1.9.0", "2.1.2", "2.1.9", "2.1.10", "2.1.100"])

    def test_non_numeric_part_replaced_with_zero(self) -> None:
        """Non-integer parts (e.g. release tags) are coerced to 0 instead of crashing."""
        # "1.0.0a" -> [1, 0, 0]
        self.assertEqual(version_sort_key("1.0.0a"), [1, 0, 0])
        # "2.foo.3" -> [2, 0, 3]
        self.assertEqual(version_sort_key("2.foo.3"), [2, 0, 3])

    def test_pure_integer_parts(self) -> None:
        """Sanity check: a clean numeric version becomes a list of ints."""
        self.assertEqual(version_sort_key("3.4.5"), [3, 4, 5])


if __name__ == "__main__":
    unittest.main()
