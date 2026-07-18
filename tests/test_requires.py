"""Smoke tests for cross-segment requirement evaluation."""

from __future__ import annotations

import unittest

from claudewheel.segment import (
    Segment,
    SegmentBar,
    _satisfies_constraint,
    evaluate_requires,
)


_VERSION_OPTIONS = ["2.1.108", "2.1.110", "2.1.115"]


def _make_bar(version_value: str | None) -> SegmentBar:
    """Build a two-segment bar: a version segment and a permissions segment.

    ``version_value`` selects the option in the version segment; None means blank.
    ``auto`` requires version >= 2.1.110.
    """
    version = Segment(
        key="version",
        label="Version",
        _init_options=_VERSION_OPTIONS,
        selected_value=version_value,
    )
    permissions = Segment(
        key="permissions",
        label="Permissions",
        _init_options=["bypass", "auto"],
        selected_value="bypass",
        option_requires={"auto": {"version": ">=2.1.110"}},
    )
    return SegmentBar(segments=[version, permissions])


class EvaluateRequiresTests(unittest.TestCase):
    def test_old_version_marks_auto_unavailable(self) -> None:
        """version=2.1.108 < 2.1.110, so 'auto' is unavailable."""
        bar = _make_bar(version_value="2.1.108")
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertIn("auto", permissions.unavailable)

    def test_exact_version_satisfies_constraint(self) -> None:
        """version=2.1.110 satisfies '>=2.1.110', so 'auto' is available."""
        bar = _make_bar(version_value="2.1.110")
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertNotIn("auto", permissions.unavailable)

    def test_newer_version_satisfies_constraint(self) -> None:
        """version=2.1.115 > 2.1.110 satisfies '>=2.1.110', so 'auto' is available."""
        bar = _make_bar(version_value="2.1.115")
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertNotIn("auto", permissions.unavailable)

    def test_blank_version_marks_auto_unavailable(self) -> None:
        """No version selected (value=None) cannot satisfy any constraint."""
        bar = _make_bar(version_value=None)
        evaluate_requires(bar)
        permissions = bar.segments[1]
        self.assertIn("auto", permissions.unavailable)

    def test_unconstrained_option_never_unavailable(self) -> None:
        """'bypass' has no requirement, so it is never in unavailable regardless of state."""
        for ver in (None, "2.1.108", "2.1.110", "2.1.115"):
            with self.subTest(version_value=ver):
                bar = _make_bar(version_value=ver)
                evaluate_requires(bar)
                self.assertNotIn("bypass", bar.segments[1].unavailable)

    def test_unavailable_set_is_recomputed_each_call(self) -> None:
        """Calling evaluate_requires again after raising the version clears 'auto'."""
        bar = _make_bar(version_value="2.1.108")
        evaluate_requires(bar)
        self.assertIn("auto", bar.segments[1].unavailable)
        # Now bump the version to one that satisfies the constraint
        bar.segments[0].select_value("2.1.115")
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
