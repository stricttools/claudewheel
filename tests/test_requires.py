"""Smoke tests for cross-segment requirement evaluation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.defaults import DEFAULT_CONFIG, DEFAULT_OPTIONS
from claudewheel.segment import (
    CONTEXT_1M_SUFFIX,
    Segment,
    SegmentBar,
    _satisfies_constraint,
    build_segment_bar,
    evaluate_requires,
    model_option_requires,
)
from claudewheel.workspace import Workspace
from tests.wheelhelpers import setup_temp_config_dir


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


_FLOOR = "2.1.219"
_RESTRICTED = "model-with-a-floor"
_UNRESTRICTED = "model-without-a-floor"


class ModelMinVersionDimmingTests(unittest.TestCase):
    """The model picker's dimming is derived from MODEL_MIN_CLI_VERSION.

    That table is the one place a model's minimum Claude Code version is
    declared; the pre-launch guard and the picker both read it, so they cannot
    disagree.
    """

    def _derived(self) -> dict[str, dict[str, str]]:
        """The requirements derived from a stand-in table."""
        with mock.patch.dict(
            "claudewheel.defaults.MODEL_MIN_CLI_VERSION",
            {_RESTRICTED: _FLOOR},
            clear=True,
        ):
            return model_option_requires()

    def _bar(self, version_value: str | None) -> SegmentBar:
        version = Segment(
            key="version",
            label="Version",
            _init_options=["2.1.218", _FLOOR],
            selected_value=version_value,
        )
        model = Segment(
            key="model",
            label="Model",
            _init_options=[
                _RESTRICTED,
                _RESTRICTED + CONTEXT_1M_SUFFIX,
                _UNRESTRICTED,
            ],
            option_requires=self._derived(),
        )
        return SegmentBar(segments=[version, model])

    def test_derivation_covers_the_base_and_its_1m_variant(self) -> None:
        """Each table entry yields a floor for the id and for its [1m] spelling."""
        self.assertEqual(
            self._derived(),
            {
                _RESTRICTED: {"version": f">={_FLOOR}"},
                _RESTRICTED + CONTEXT_1M_SUFFIX: {"version": f">={_FLOOR}"},
            },
        )

    def test_too_old_a_version_dims_the_restricted_model(self) -> None:
        """A binary below the floor dims the model; an unrestricted one stays lit."""
        bar = self._bar("2.1.218")
        evaluate_requires(bar)
        model = bar.segments[1]
        self.assertIn(_RESTRICTED, model.unavailable)
        self.assertNotIn(_UNRESTRICTED, model.unavailable)

    def test_the_1m_variant_is_dimmed_exactly_when_its_base_is(self) -> None:
        """The [1m] spelling inherits the base model's floor, both ways."""
        for version_value, dimmed in (("2.1.218", True), (_FLOOR, False)):
            with self.subTest(version=version_value):
                bar = self._bar(version_value)
                evaluate_requires(bar)
                model = bar.segments[1]
                self.assertEqual(_RESTRICTED in model.unavailable, dimmed)
                self.assertEqual(
                    _RESTRICTED + CONTEXT_1M_SUFFIX in model.unavailable, dimmed
                )

    def test_a_version_at_the_floor_dims_nothing(self) -> None:
        """'>=' is inclusive, so the exact minimum is enough."""
        bar = self._bar(_FLOOR)
        evaluate_requires(bar)
        self.assertEqual(bar.segments[1].unavailable, set())

    def test_no_version_selected_dims_the_restricted_model(self) -> None:
        """With no version chosen there is nothing to satisfy the floor with.

        This is the generic cross-segment rule: a constraint on a segment
        carrying no selection cannot be satisfied.
        """
        bar = self._bar(None)
        evaluate_requires(bar)
        self.assertIn(_RESTRICTED, bar.segments[1].unavailable)

    def test_build_segment_bar_wires_the_model_segment(self) -> None:
        """The bar the app builds carries the derived requirements."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = setup_temp_config_dir(
                Path(tmp),
                config={**DEFAULT_CONFIG, "enabled_segments": ["model"]},
                options={
                    **DEFAULT_OPTIONS,
                    "model": {"values": [_UNRESTRICTED], "pinned": []},
                },
            )
            cfg = Workspace.open(paths["CONFIG_DIR"]).appconfig()
            bar = build_segment_bar(cfg, skip_slow=True)

        self.assertEqual(bar.segments[0].key, "model")
        self.assertEqual(bar.segments[0].option_requires, model_option_requires())


if __name__ == "__main__":
    unittest.main()
