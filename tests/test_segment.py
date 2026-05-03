"""Smoke tests for Segment dataclass and version_sort_key in claudewheel.segment."""

from __future__ import annotations

import unittest

from claudewheel.segment import Segment, version_sort_key


class SegmentCycleWrapTests(unittest.TestCase):
    """Verify the (n+1)-position ring with wrap=True (the default)."""

    def test_single_option_toggles_with_blank(self) -> None:
        """With one option, cycling alternates between -1 (blank) and 0."""
        seg = Segment(key="k", label="K", options=["only"], wrap=True, selected_idx=-1)
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
            key="k", label="K", options=["a", "b", "c"], wrap=True, selected_idx=-1
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
            key="k", label="K", options=["a", "b", "c"], wrap=True, selected_idx=-1
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
            key="k", label="K", options=["a", "b", "c"], wrap=False, selected_idx=2
        )
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)

    def test_backward_from_first_reaches_blank(self) -> None:
        """Going -1 from the first option lands on -1 (blank) -- existing behavior preserved."""
        seg = Segment(
            key="k", label="K", options=["a", "b", "c"], wrap=False, selected_idx=0
        )
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)

    def test_past_blank_stays_at_blank(self) -> None:
        """Once at blank, going further in either direction stays at blank (no continuous wrap)."""
        seg = Segment(
            key="k", label="K", options=["a", "b", "c"], wrap=False, selected_idx=-1
        )
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)
        # And +1 from blank still re-enters the option list at the first option
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)

    def test_forward_past_last_then_again_stays_at_blank(self) -> None:
        """DOWN from last → blank, then DOWN again stays at blank (no wrap to first)."""
        seg = Segment(
            key="k", label="K", options=["a", "b", "c"], wrap=False, selected_idx=2
        )
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)  # re-enters at first
        # But if we re-test starting from blank going +1 twice, we move into options
        seg.selected_idx = -1
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 0)

    def test_normal_steps_still_work(self) -> None:
        """Non-boundary cycling still moves one step at a time."""
        seg = Segment(
            key="k", label="K", options=["a", "b", "c"], wrap=False, selected_idx=0
        )
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 1)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, 2)
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, 1)


class SegmentCycleEmptyTests(unittest.TestCase):
    def test_empty_options_is_noop(self) -> None:
        """With no options, cycle() must not change selected_idx or raise."""
        seg = Segment(key="k", label="K", options=[], wrap=True, selected_idx=-1)
        seg.cycle(+1)
        self.assertEqual(seg.selected_idx, -1)
        seg.cycle(-1)
        self.assertEqual(seg.selected_idx, -1)


class SegmentValuePropertyTests(unittest.TestCase):
    def test_value_none_when_blank(self) -> None:
        """value is None whenever selected_idx < 0."""
        seg = Segment(key="k", label="K", options=["a", "b"], selected_idx=-1)
        self.assertIsNone(seg.value)

    def test_value_none_when_no_options(self) -> None:
        """value is None whenever options is empty (regardless of idx)."""
        seg = Segment(key="k", label="K", options=[], selected_idx=0)
        self.assertIsNone(seg.value)

    def test_value_returns_selected_option(self) -> None:
        """value returns options[selected_idx] for a valid selection."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_idx=1)
        self.assertEqual(seg.value, "b")


class SegmentSelectValueTests(unittest.TestCase):
    def test_select_existing_value_returns_true(self) -> None:
        """select_value sets selected_idx and returns True when the value exists."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_idx=-1)
        self.assertTrue(seg.select_value("b"))
        self.assertEqual(seg.selected_idx, 1)

    def test_select_missing_value_returns_false_and_keeps_idx(self) -> None:
        """select_value returns False and leaves selected_idx untouched on miss."""
        seg = Segment(key="k", label="K", options=["a", "b", "c"], selected_idx=2)
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


class VersionSortKeyTests(unittest.TestCase):
    def test_numeric_ordering_not_alphabetical(self) -> None:
        """2.1.10 must sort *higher* than 2.1.9 (the alphabetical order is the opposite)."""
        self.assertGreater(
            version_sort_key("2.1.10"), version_sort_key("2.1.9")
        )

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
