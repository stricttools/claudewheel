"""Smoke tests for cross-segment requirement evaluation."""

from __future__ import annotations

import unittest

from claudewheel.segment import (
    Segment,
    SegmentBar,
    _satisfies_constraint,
    evaluate_requires,
)


def _make_bar(version_idx: int) -> SegmentBar:
    """Build a two-segment bar: a version segment and a permissions segment.

    ``version_idx`` selects the option in the version segment; -1 means blank.
    ``auto`` requires version >= 2.1.110.
    """
    version = Segment(
        key="version",
        label="Version",
        options=["2.1.108", "2.1.110", "2.1.115"],
        selected_idx=version_idx,
    )
    permissions = Segment(
        key="permissions",
        label="Permissions",
        options=["bypass", "auto"],
        selected_idx=0,
        option_requires={"auto": {"version": ">=2.1.110"}},
    )
    return SegmentBar(segments=[version, permissions])


class EvaluateRequiresTests(unittest.TestCase):
    def test_old_version_marks_auto_unavailable(self) -> None:
        """version=2.1.108 < 2.1.110, so 'auto' is unavailable."""
        bar = _make_bar(version_idx=0)  # 2.1.108
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertIn("auto", permissions.unavailable)

    def test_exact_version_satisfies_constraint(self) -> None:
        """version=2.1.110 satisfies '>=2.1.110', so 'auto' is available."""
        bar = _make_bar(version_idx=1)  # 2.1.110
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertNotIn("auto", permissions.unavailable)

    def test_newer_version_satisfies_constraint(self) -> None:
        """version=2.1.115 > 2.1.110 satisfies '>=2.1.110', so 'auto' is available."""
        bar = _make_bar(version_idx=2)  # 2.1.115
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertNotIn("auto", permissions.unavailable)

    def test_blank_version_marks_auto_unavailable(self) -> None:
        """No version selected (idx=-1, value=None) cannot satisfy any constraint."""
        bar = _make_bar(version_idx=-1)
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertIn("auto", permissions.unavailable)

    def test_unconstrained_option_never_unavailable(self) -> None:
        """'bypass' has no requirement, so it is never in unavailable regardless of state."""
        for idx in (-1, 0, 1, 2):
            with self.subTest(version_idx=idx):
                bar = _make_bar(version_idx=idx)
                evaluate_requires(bar)
                self.assertNotIn("bypass", bar.segments[1].unavailable)

    def test_unavailable_set_is_recomputed_each_call(self) -> None:
        """Calling evaluate_requires again after raising the version clears 'auto'."""
        bar = _make_bar(version_idx=0)  # 2.1.108
        evaluate_requires(bar)
        self.assertIn("auto", bar.segments[1].unavailable)
        # Now bump the version to one that satisfies the constraint
        bar.segments[0].selected_idx = 2  # 2.1.115
        evaluate_requires(bar)
        self.assertNotIn("auto", bar.segments[1].unavailable)


class SatisfiesConstraintTests(unittest.TestCase):
    def test_gte(self) -> None:
        """'>=' is inclusive on the lower bound."""
        self.assertTrue(_satisfies_constraint("2.1.110", ">=2.1.110"))
        self.assertTrue(_satisfies_constraint("2.1.111", ">=2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.109", ">=2.1.110"))

    def test_lte(self) -> None:
        """'<=' is inclusive on the upper bound."""
        self.assertTrue(_satisfies_constraint("2.1.110", "<=2.1.110"))
        self.assertTrue(_satisfies_constraint("2.1.109", "<=2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.111", "<=2.1.110"))

    def test_gt(self) -> None:
        """'>' is strictly greater than."""
        self.assertTrue(_satisfies_constraint("2.1.111", ">2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.110", ">2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.109", ">2.1.110"))

    def test_lt(self) -> None:
        """'<' is strictly less than."""
        self.assertTrue(_satisfies_constraint("2.1.109", "<2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.110", "<2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.111", "<2.1.110"))

    def test_exact(self) -> None:
        """No operator means an exact string-equality match."""
        self.assertTrue(_satisfies_constraint("2.1.110", "2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.111", "2.1.110"))
        self.assertFalse(_satisfies_constraint("2.1.109", "2.1.110"))

    def test_none_value_never_satisfies(self) -> None:
        """A None value can never satisfy any constraint, regardless of operator."""
        for constraint in (
            ">=2.1.110",
            "<=2.1.110",
            ">2.1.110",
            "<2.1.110",
            "2.1.110",
        ):
            with self.subTest(constraint=constraint):
                self.assertFalse(_satisfies_constraint(None, constraint))

    def test_numeric_not_alphabetical_comparison(self) -> None:
        """Comparison must be numeric: 2.1.10 is greater than 2.1.9."""
        self.assertTrue(_satisfies_constraint("2.1.10", ">=2.1.9"))
        self.assertTrue(_satisfies_constraint("2.1.10", ">2.1.9"))
        self.assertFalse(_satisfies_constraint("2.1.9", ">=2.1.10"))


if __name__ == "__main__":
    unittest.main()
