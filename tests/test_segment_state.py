"""Tests for SegmentState dataclass: collection management, merge order, caching."""

from __future__ import annotations

import unittest

from claudewheel.segment import SegmentState


class MergeOrderTests(unittest.TestCase):
    """Options are computed as: pinned + discovered + defaults + ephemeral, deduped."""

    def test_pinned_before_discovered(self) -> None:
        """Pinned values appear before discovered values in the merged list."""
        st = SegmentState()
        st.set_discovered(["d1", "d2"])
        st.add_pinned("p1")
        self.assertEqual(st.options, ["p1", "d1", "d2"])

    def test_discovered_before_defaults(self) -> None:
        """Discovered values appear before default values."""
        st = SegmentState()
        st.set_defaults(["def1", "def2"])
        st.set_discovered(["disc1"])
        self.assertEqual(st.options, ["disc1", "def1", "def2"])

    def test_defaults_before_ephemeral(self) -> None:
        """Default values appear before ephemeral values."""
        st = SegmentState()
        st.set_defaults(["def1"])
        st.add_ephemeral("+")
        self.assertEqual(st.options, ["def1", "+"])

    def test_full_order(self) -> None:
        """All four collections merge in correct order: pinned, discovered, defaults, ephemeral."""
        st = SegmentState()
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        st.add_ephemeral("eph")
        self.assertEqual(st.options, ["pin", "disc", "def", "eph"])


class DeduplicationTests(unittest.TestCase):
    """Values appearing in multiple collections are deduped to first occurrence."""

    def test_pinned_wins_over_discovered(self) -> None:
        """A value in both pinned and discovered appears once, at the pinned position."""
        st = SegmentState()
        st.add_pinned("shared")
        st.set_discovered(["shared", "other"])
        result = st.options
        self.assertEqual(result.count("shared"), 1)
        self.assertEqual(result.index("shared"), 0)  # from pinned

    def test_discovered_wins_over_defaults(self) -> None:
        """A value in both discovered and defaults appears once, from discovered."""
        st = SegmentState()
        st.set_discovered(["both"])
        st.set_defaults(["both", "only_def"])
        result = st.options
        self.assertEqual(result.count("both"), 1)
        self.assertEqual(result, ["both", "only_def"])

    def test_all_sources_deduped(self) -> None:
        """A value in all four sources appears exactly once."""
        st = SegmentState()
        st.add_pinned("x")
        st.set_discovered(["x"])
        st.set_defaults(["x"])
        st.add_ephemeral("x")
        self.assertEqual(st.options, ["x"])


class SourceOfTests(unittest.TestCase):
    """source_of returns the collection name for a value."""

    def test_pinned_source(self) -> None:
        st = SegmentState()
        st.add_pinned("val")
        self.assertEqual(st.source_of("val"), "pinned")

    def test_discovered_source(self) -> None:
        st = SegmentState()
        st.set_discovered(["val"])
        self.assertEqual(st.source_of("val"), "discovered")

    def test_defaults_source(self) -> None:
        st = SegmentState()
        st.set_defaults(["val"])
        self.assertEqual(st.source_of("val"), "defaults")

    def test_ephemeral_source(self) -> None:
        st = SegmentState()
        st.add_ephemeral("val")
        self.assertEqual(st.source_of("val"), "ephemeral")

    def test_pinned_takes_priority_over_discovered(self) -> None:
        """When a value is in both pinned and discovered, source_of returns pinned."""
        st = SegmentState()
        st.add_pinned("val")
        st.set_discovered(["val"])
        self.assertEqual(st.source_of("val"), "pinned")

    def test_not_found_returns_none(self) -> None:
        st = SegmentState()
        self.assertIsNone(st.source_of("missing"))


class CacheInvalidationTests(unittest.TestCase):
    """Mutation methods invalidate the cached options list."""

    def test_set_discovered_invalidates(self) -> None:
        st = SegmentState()
        st.set_defaults(["a"])
        first = st.options
        st.set_discovered(["b"])
        second = st.options
        self.assertNotEqual(first, second)
        self.assertEqual(second, ["b", "a"])

    def test_add_pinned_invalidates(self) -> None:
        st = SegmentState()
        st.set_defaults(["a"])
        first = st.options
        st.add_pinned("p")
        second = st.options
        self.assertEqual(second, ["p", "a"])

    def test_remove_pinned_invalidates(self) -> None:
        st = SegmentState()
        st.add_pinned("p")
        st.set_defaults(["a"])
        first = st.options
        self.assertIn("p", first)
        st.remove_pinned("p")
        second = st.options
        self.assertEqual(second, ["a"])

    def test_set_defaults_invalidates(self) -> None:
        st = SegmentState()
        st.set_defaults(["old"])
        _ = st.options
        st.set_defaults(["new"])
        self.assertEqual(st.options, ["new"])

    def test_add_ephemeral_invalidates(self) -> None:
        st = SegmentState()
        _ = st.options
        st.add_ephemeral("+")
        self.assertEqual(st.options, ["+"])

    def test_cached_reused_without_mutation(self) -> None:
        """Without mutation, the same list object is returned (cache hit)."""
        st = SegmentState()
        st.set_defaults(["a", "b"])
        first = st.options
        second = st.options
        self.assertIs(first, second)


class InstalledTests(unittest.TestCase):
    def test_is_installed_true(self) -> None:
        st = SegmentState()
        st.set_installed({"1.0", "2.0"})
        self.assertTrue(st.is_installed("1.0"))

    def test_is_installed_false(self) -> None:
        st = SegmentState()
        st.set_installed({"1.0"})
        self.assertFalse(st.is_installed("2.0"))

    def test_empty_installed(self) -> None:
        st = SegmentState()
        self.assertFalse(st.is_installed("anything"))


class PinnedMutationTests(unittest.TestCase):
    def test_add_pinned_dedup(self) -> None:
        """Adding the same value twice does not create a duplicate."""
        st = SegmentState()
        st.add_pinned("x")
        st.add_pinned("x")
        self.assertEqual(st.options.count("x"), 1)

    def test_remove_pinned_nonexistent_is_noop(self) -> None:
        """Removing a value not in pinned does not raise."""
        st = SegmentState()
        st.remove_pinned("missing")  # should not raise
        self.assertEqual(st.options, [])

    def test_add_ephemeral_dedup(self) -> None:
        """Adding the same ephemeral value twice does not duplicate."""
        st = SegmentState()
        st.add_ephemeral("+")
        st.add_ephemeral("+")
        self.assertEqual(st.options.count("+"), 1)


class EmptyStateTests(unittest.TestCase):
    def test_empty_state_returns_empty_options(self) -> None:
        st = SegmentState()
        self.assertEqual(st.options, [])

    def test_source_of_on_empty_returns_none(self) -> None:
        st = SegmentState()
        self.assertIsNone(st.source_of("anything"))


class MetadataTests(unittest.TestCase):
    def test_set_metadata(self) -> None:
        st = SegmentState()
        st.set_metadata({"a": {"key": "val"}})
        self.assertEqual(st.metadata, {"a": {"key": "val"}})

    def test_update_metadata_merges(self) -> None:
        st = SegmentState()
        st.set_metadata({"a": {"k1": "v1"}})
        st.update_metadata({"b": {"k2": "v2"}})
        self.assertEqual(st.metadata, {"a": {"k1": "v1"}, "b": {"k2": "v2"}})

    def test_update_metadata_overwrites_existing_key(self) -> None:
        st = SegmentState()
        st.set_metadata({"a": {"old": True}})
        st.update_metadata({"a": {"new": True}})
        self.assertEqual(st.metadata["a"], {"new": True})


class CollectionOrderTests(unittest.TestCase):
    """Test custom collection_order on SegmentState."""

    def test_discovered_only(self) -> None:
        """collection_order=["discovered"] ignores pinned and defaults."""
        st = SegmentState(collection_order=["discovered"])
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        self.assertEqual(st.options, ["disc"])

    def test_pinned_and_defaults_only(self) -> None:
        """collection_order=["pinned", "defaults"] ignores discovered."""
        st = SegmentState(collection_order=["pinned", "defaults"])
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        self.assertEqual(st.options, ["pin", "def"])

    def test_pinned_and_discovered(self) -> None:
        """collection_order=["pinned", "discovered"] includes both, no defaults."""
        st = SegmentState(collection_order=["pinned", "discovered"])
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        self.assertEqual(st.options, ["pin", "disc"])

    def test_ephemeral_always_at_end(self) -> None:
        """Ephemeral values are always appended after the ordered collections."""
        st = SegmentState(collection_order=["discovered"])
        st.set_discovered(["disc"])
        st.add_ephemeral("+")
        self.assertEqual(st.options, ["disc", "+"])

    def test_reversed_order(self) -> None:
        """collection_order=["defaults", "discovered", "pinned"] reverses priority."""
        st = SegmentState(collection_order=["defaults", "discovered", "pinned"])
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        self.assertEqual(st.options, ["def", "disc", "pin"])

    def test_empty_collection_order(self) -> None:
        """collection_order=[] yields only ephemeral values."""
        st = SegmentState(collection_order=[])
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        st.add_ephemeral("eph")
        self.assertEqual(st.options, ["eph"])

    def test_default_order_unchanged(self) -> None:
        """Default collection_order preserves original behavior."""
        st = SegmentState()
        st.add_pinned("pin")
        st.set_discovered(["disc"])
        st.set_defaults(["def"])
        st.add_ephemeral("eph")
        self.assertEqual(st.options, ["pin", "disc", "def", "eph"])


class SemverSortTests(unittest.TestCase):
    """Test sort="semver_desc" on SegmentState."""

    def test_semver_desc_sorts_versions(self) -> None:
        """sort="semver_desc" sorts version strings newest-first."""
        st = SegmentState(sort="semver_desc")
        st.set_discovered(["1.0.0", "1.0.10", "1.0.2"])
        self.assertEqual(st.options, ["1.0.10", "1.0.2", "1.0.0"])

    def test_semver_desc_across_collections(self) -> None:
        """Versions from multiple collections are merged then sorted."""
        st = SegmentState(sort="semver_desc")
        st.add_pinned("2.0.0")
        st.set_discovered(["1.0.0", "3.0.0"])
        st.set_defaults(["1.5.0"])
        # All four values sorted descending, then ephemeral at end
        self.assertEqual(st.options, ["3.0.0", "2.0.0", "1.5.0", "1.0.0"])

    def test_semver_desc_with_ephemeral_at_end(self) -> None:
        """Ephemeral values go after the sorted versions."""
        st = SegmentState(sort="semver_desc")
        st.set_discovered(["2.0.0", "1.0.0"])
        st.add_ephemeral("+")
        self.assertEqual(st.options, ["2.0.0", "1.0.0", "+"])

    def test_no_sort_preserves_insertion_order(self) -> None:
        """Without sort, values appear in collection_order insertion order."""
        st = SegmentState()  # sort=None
        st.set_discovered(["1.0.0", "1.0.10", "1.0.2"])
        self.assertEqual(st.options, ["1.0.0", "1.0.10", "1.0.2"])

    def test_semver_desc_invalidates_cache(self) -> None:
        """Changing discovered values recalculates with sort applied."""
        st = SegmentState(sort="semver_desc")
        st.set_discovered(["1.0.0", "2.0.0"])
        first = st.options
        self.assertEqual(first, ["2.0.0", "1.0.0"])
        st.set_discovered(["3.0.0", "1.0.0"])
        second = st.options
        self.assertEqual(second, ["3.0.0", "1.0.0"])

    def test_semver_desc_deduplicates(self) -> None:
        """Deduplication still works with sorting enabled."""
        st = SegmentState(sort="semver_desc")
        st.add_pinned("2.0.0")
        st.set_discovered(["2.0.0", "1.0.0"])
        # "2.0.0" appears in both but should be deduplicated
        self.assertEqual(st.options, ["2.0.0", "1.0.0"])


if __name__ == "__main__":
    unittest.main()
