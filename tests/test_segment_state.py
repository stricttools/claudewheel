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
        _ = st.options  # prime the cache
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

    def test_has_installed_false_on_empty(self) -> None:
        st = SegmentState()
        self.assertFalse(st.has_installed)

    def test_has_installed_true_after_set(self) -> None:
        st = SegmentState()
        st.set_installed({"1.0"})
        self.assertTrue(st.has_installed)

    def test_mark_installed_adds_value(self) -> None:
        st = SegmentState()
        st.mark_installed("3.0")
        self.assertTrue(st.is_installed("3.0"))
        self.assertTrue(st.has_installed)

    def test_mark_installed_idempotent(self) -> None:
        st = SegmentState()
        st.mark_installed("3.0")
        st.mark_installed("3.0")
        self.assertTrue(st.is_installed("3.0"))


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

    def test_pinned_profile_appears_in_options(self) -> None:
        """Profile segment: pinned values appear alongside discovered ones.

        Mirrors the profile segment's collection_order=["pinned", "discovered"].
        After creating a profile (adding it as pinned), it must show up in options
        even if the discovery scan hasn't re-run yet.
        """
        st = SegmentState(collection_order=["pinned", "discovered"])
        st.set_discovered(["existing-profile"])
        st.add_pinned("new-profile")
        self.assertIn("new-profile", st.options)
        # Pinned appears before discovered
        self.assertEqual(st.options.index("new-profile"), 0)
        self.assertIn("existing-profile", st.options)

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


class StalenessVerifyTests(unittest.TestCase):
    """Tests for set_discovered staleness policy (verify_fn parameter)."""

    def test_immediate_policy_removes_old_values(self) -> None:
        """Without verify_fn, old values not in the new list are dropped."""
        st = SegmentState()
        st.set_discovered(["a", "b", "c"])
        st.set_discovered(["b", "d"])
        self.assertEqual(st._discovered, ["b", "d"])

    def test_verify_policy_keeps_passing_values(self) -> None:
        """Old values that pass verify_fn are kept even if absent from new list."""
        st = SegmentState()
        st.set_discovered(["a", "b", "c"])
        # "a" and "c" are removed from new list; verify keeps "a" but not "c"
        st.set_discovered(["b"], verify_fn=lambda v: v == "a")
        self.assertIn("a", st._discovered)
        self.assertIn("b", st._discovered)
        self.assertNotIn("c", st._discovered)

    def test_verify_policy_removes_failing_values(self) -> None:
        """Old values that fail verify_fn are removed."""
        st = SegmentState()
        st.set_discovered(["a", "b"])
        st.set_discovered(["b"], verify_fn=lambda v: False)
        self.assertEqual(st._discovered, ["b"])

    def test_new_values_always_added(self) -> None:
        """New values are always included regardless of verify policy."""
        st = SegmentState()
        st.set_discovered(["a"])
        # verify_fn only applies to OLD values not in new list
        st.set_discovered(["b", "c"], verify_fn=lambda v: False)
        self.assertIn("b", st._discovered)
        self.assertIn("c", st._discovered)
        # "a" was old and failed verify, so removed
        self.assertNotIn("a", st._discovered)

    def test_verify_policy_new_values_first(self) -> None:
        """New list values appear before verified survivors."""
        st = SegmentState()
        st.set_discovered(["old1", "old2"])
        st.set_discovered(["new1"], verify_fn=lambda v: True)
        # new1 first, then old1 and old2 (both pass verify)
        self.assertEqual(st._discovered, ["new1", "old1", "old2"])

    def test_verify_policy_invalidates_cache(self) -> None:
        """set_discovered with verify_fn still invalidates the options cache."""
        st = SegmentState()
        st.set_discovered(["a"])
        _ = st.options  # populate cache
        st.set_discovered(["b"], verify_fn=lambda v: True)
        # Cache must be invalidated -- options should reflect new state
        self.assertEqual(st.options, ["b", "a"])

    def test_verify_policy_values_in_both_old_and_new(self) -> None:
        """Values present in both old and new lists are not double-verified or duplicated."""
        st = SegmentState()
        st.set_discovered(["a", "b", "c"])
        # "b" is in both old and new -- should appear once, "a" passes verify
        st.set_discovered(["b", "d"], verify_fn=lambda v: v == "a")
        self.assertEqual(st._discovered, ["b", "d", "a"])
        # No duplicates
        self.assertEqual(len(st._discovered), len(set(st._discovered)))


class AuthenticatedTests(unittest.TestCase):
    """Tests for auth-status tracking on SegmentState."""

    def test_has_auth_status_false_by_default(self) -> None:
        """Auth status tracking is inactive until explicitly set."""
        st = SegmentState()
        self.assertFalse(st.has_auth_status)

    def test_set_authenticated_activates_status(self) -> None:
        """Calling set_authenticated sets has_auth_status to True."""
        st = SegmentState()
        st.set_authenticated({"prof1", "prof2"})
        self.assertTrue(st.has_auth_status)

    def test_is_authenticated_correct_values(self) -> None:
        """is_authenticated returns True for values in the set, False otherwise."""
        st = SegmentState()
        st.set_authenticated({"alpha", "beta"})
        self.assertTrue(st.is_authenticated("alpha"))
        self.assertTrue(st.is_authenticated("beta"))
        self.assertFalse(st.is_authenticated("gamma"))

    def test_set_authenticated_empty_set_activates(self) -> None:
        """Empty set still activates auth tracking (all profiles unauthenticated)."""
        st = SegmentState()
        st.set_authenticated(set())
        self.assertTrue(st.has_auth_status)
        self.assertFalse(st.is_authenticated("anything"))

    def test_set_authenticated_invalidates_cache(self) -> None:
        """set_authenticated invalidates the options cache."""
        st = SegmentState()
        st.set_defaults(["a", "b"])
        first = st.options  # prime cache
        st.set_authenticated({"a"})
        second = st.options  # should rebuild
        # Content is the same, but it must be a new list object (cache was invalidated)
        self.assertIsNot(first, second)


class AuthFromMetadataTests(unittest.TestCase):
    """Tests for _update_auth_from_metadata computing auth status from metadata."""

    def test_auth_activated_when_metadata_has_auth_fields(self) -> None:
        """Auth tracking activates when metadata contains has_token/has_credentials."""
        from claudewheel.segment import Segment, _update_auth_from_metadata

        seg = Segment(key="profile", label="Profile")
        seg.state.set_metadata({
            "alice": {"config_dir": "/a", "has_token": True, "has_credentials": True},
            "bob": {"config_dir": "/b", "has_token": False, "has_credentials": False},
        })
        _update_auth_from_metadata(seg)
        self.assertTrue(seg.state.has_auth_status)
        self.assertTrue(seg.state.is_authenticated("alice"))
        self.assertFalse(seg.state.is_authenticated("bob"))

    def test_auth_not_activated_without_auth_fields(self) -> None:
        """Auth tracking stays inactive when metadata has no auth fields."""
        from claudewheel.segment import Segment, _update_auth_from_metadata

        seg = Segment(key="version", label="Version")
        seg.state.set_metadata({"1.0": {"path": "/opt"}})
        _update_auth_from_metadata(seg)
        self.assertFalse(seg.state.has_auth_status)

    def test_token_only_is_authenticated(self) -> None:
        """A profile with has_token=True but has_credentials=False is authenticated."""
        from claudewheel.segment import Segment, _update_auth_from_metadata

        seg = Segment(key="profile", label="Profile")
        seg.state.set_metadata({
            "tok": {"config_dir": "/t", "has_token": True, "has_credentials": False},
        })
        _update_auth_from_metadata(seg)
        self.assertTrue(seg.state.is_authenticated("tok"))

    def test_credentials_only_is_authenticated(self) -> None:
        """A profile with has_credentials=True but has_token=False is authenticated."""
        from claudewheel.segment import Segment, _update_auth_from_metadata

        seg = Segment(key="profile", label="Profile")
        seg.state.set_metadata({
            "cred": {"config_dir": "/c", "has_token": False, "has_credentials": True},
        })
        _update_auth_from_metadata(seg)
        self.assertTrue(seg.state.is_authenticated("cred"))

    def test_pinned_without_metadata_is_unauthenticated(self) -> None:
        """A pinned profile not in metadata is treated as unauthenticated."""
        from claudewheel.segment import Segment, _update_auth_from_metadata

        seg = Segment(key="profile", label="Profile")
        seg.state.set_metadata({
            "existing": {"config_dir": "/e", "has_token": True, "has_credentials": True},
        })
        seg.state.add_pinned("new-profile")
        _update_auth_from_metadata(seg)
        self.assertTrue(seg.state.has_auth_status)
        self.assertTrue(seg.state.is_authenticated("existing"))
        self.assertFalse(seg.state.is_authenticated("new-profile"))


class DiscoverProfilesMetadataTests(unittest.TestCase):
    """Tests that _discover_profiles includes auth fields in metadata."""

    def test_metadata_includes_auth_fields(self) -> None:
        """Metadata dicts from profile discovery include has_token and has_credentials."""
        from unittest.mock import MagicMock
        from claudewheel.profile_store import Profile
        from claudewheel.segment import _discover_profiles

        mock_profiles = [
            Profile(name="default", path="/home/.claude", has_credentials=True, has_token=True),
            Profile(name="work", path="/home/.claudewheel/profiles/work", has_credentials=True, has_token=False),
            Profile(name="new", path="/home/.claudewheel/profiles/new", has_credentials=False, has_token=False),
        ]
        mock_ws = MagicMock()
        mock_ws.profiles.enumerate.return_value = mock_profiles
        result = _discover_profiles({}, {}, mock_ws)

        self.assertIn("has_token", result.metadata["default"])
        self.assertIn("has_credentials", result.metadata["default"])
        self.assertTrue(result.metadata["default"]["has_token"])
        self.assertTrue(result.metadata["default"]["has_credentials"])

        self.assertTrue(result.metadata["work"]["has_credentials"])
        self.assertFalse(result.metadata["work"]["has_token"])

        self.assertFalse(result.metadata["new"]["has_credentials"])
        self.assertFalse(result.metadata["new"]["has_token"])


if __name__ == "__main__":
    unittest.main()
