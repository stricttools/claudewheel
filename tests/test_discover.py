"""Tests for individual discovery functions in the registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claudewheel.segment import (
    DiscoveryResult,
    _discover_directory_listing,
    _discover_directory_scan,
    _discover_gh_accounts,
    _discover_npm_and_local_cached,
    _discover_profiles,
    _discover_state_field,
    _parse_requires,
    _parse_static_values,
)


class DirectoryListingDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_files_sorted_by_version_descending(self) -> None:
        """directory_listing returns file names sorted newest-first by numeric version."""
        for name in ("1.0.0", "1.0.10", "1.0.2"):
            (self.tmp / name).write_text("")

        config = {
            "values": [],
            "discovery": {"type": "directory_listing", "path": str(self.tmp)},
        }
        result = _discover_directory_listing(config, state={})
        # 1.0.10 must come before 1.0.2 (numeric, not alphabetical)
        self.assertEqual(result.values, ["1.0.10", "1.0.2", "1.0.0"])

    def test_missing_directory_returns_empty(self) -> None:
        """If the directory does not exist, the result is empty."""
        config = {
            "values": ["fallback"],
            "discovery": {
                "type": "directory_listing",
                "path": str(self.tmp / "nope"),
            },
        }
        result = _discover_directory_listing(config, state={})
        self.assertEqual(result.values, [])

    def test_empty_directory_returns_empty(self) -> None:
        """An empty (but existing) directory yields empty list."""
        config = {
            "values": ["static-a"],
            "discovery": {"type": "directory_listing", "path": str(self.tmp)},
        }
        result = _discover_directory_listing(config, state={})
        self.assertEqual(result.values, [])

    def test_directories_inside_path_are_excluded(self) -> None:
        """Only files are listed; subdirectories are excluded."""
        (self.tmp / "1.0.0").write_text("")
        (self.tmp / "subdir").mkdir()

        config = {
            "values": [],
            "discovery": {"type": "directory_listing", "path": str(self.tmp)},
        }
        result = _discover_directory_listing(config, state={})
        self.assertEqual(result.values, ["1.0.0"])
        self.assertNotIn("subdir", result.values)


class DirectoryScanDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # Create parent/proj-a, parent/proj-b, parent/.hidden
        self.parent = self.tmp / "parent"
        self.parent.mkdir()
        (self.parent / "proj-a").mkdir()
        (self.parent / "proj-b").mkdir()
        (self.parent / ".hidden").mkdir()

    def test_excludes_hidden_directories(self) -> None:
        """Subdirs starting with '.' are skipped."""
        config = {
            "values": [],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(self.parent)],
            },
        }
        result = _discover_directory_scan(config, state={})
        names = [Path(p).name for p in result.values]
        self.assertEqual(names, ["proj-a", "proj-b"])
        self.assertNotIn(".hidden", names)

    def test_merge_order_recent_then_discovered_then_static(self) -> None:
        """Order is: state recent_dirs, then discovered dirs, then static values."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "alpha").mkdir()

        config = {
            "values": ["/static/path"],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                "state_field": "recent_dirs",
            },
        }
        state = {"recent_dirs": ["/recent/one"]}
        result = _discover_directory_scan(config, state)
        # recent first
        self.assertEqual(result.values[0], "/recent/one")
        # static last
        self.assertEqual(result.values[-1], "/static/path")
        # discovered in the middle
        self.assertEqual(len(result.values), 3)

    def test_dedup_across_all_three_sources(self) -> None:
        """Duplicate entries across recent, discovered, and static are collapsed."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "dup").mkdir()
        discovered_path = str(parent / "dup")

        config = {
            "values": [discovered_path],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                "state_field": "recent_dirs",
            },
        }
        state = {"recent_dirs": [discovered_path]}
        result = _discover_directory_scan(config, state)
        self.assertEqual(result.values.count(discovered_path), 1)

    def test_no_state_field_means_no_recent_dirs(self) -> None:
        """When state_field is absent from discovery config, recent is empty."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "proj").mkdir()

        config = {
            "values": ["/static"],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                # no state_field key
            },
        }
        result = _discover_directory_scan(
            config, state={"recent_dirs": ["/should-not-appear"]},
        )
        self.assertNotIn("/should-not-appear", result.values)
        self.assertEqual(result.values[-1], "/static")

    def test_empty_parents_uses_static_values(self) -> None:
        """If parents list is empty, discovered is empty, but static values survive."""
        config = {
            "values": ["/fallback"],
            "discovery": {
                "type": "directory_scan",
                "parents": [],
                "state_field": "recent_dirs",
            },
        }
        result = _discover_directory_scan(config, state={})
        self.assertEqual(result.values, ["/fallback"])


class StateFieldDiscoveryTests(unittest.TestCase):
    def test_state_first_then_static_deduplicated(self) -> None:
        """state values come first; static values appended; duplicates removed."""
        config = {
            "values": ["~/baz"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        state = {"recent_dirs": ["~/foo", "~/bar"]}
        result = _discover_state_field(config, state)
        self.assertEqual(result.values, ["~/foo", "~/bar", "~/baz"])

    def test_dedup_when_static_overlaps_state(self) -> None:
        """An overlap between state and static is collapsed to a single occurrence."""
        config = {
            "values": ["~/foo", "~/baz"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        state = {"recent_dirs": ["~/foo", "~/bar"]}
        result = _discover_state_field(config, state)
        self.assertEqual(result.values, ["~/foo", "~/bar", "~/baz"])

    def test_missing_state_field_uses_static_only(self) -> None:
        """If the state key is absent, only the static values come through."""
        config = {
            "values": ["~/baz"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        result = _discover_state_field(config, state={})
        self.assertEqual(result.values, ["~/baz"])

    def test_empty_state_and_empty_static_yields_empty(self) -> None:
        """When both state field and static values are empty, result is empty."""
        config = {
            "values": [],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        result = _discover_state_field(config, state={"recent_dirs": []})
        self.assertEqual(result.values, [])

    def test_state_values_come_before_static(self) -> None:
        """State values strictly precede static values in the result."""
        config = {
            "values": ["~/z-static"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        state = {"recent_dirs": ["~/a-state"]}
        result = _discover_state_field(config, state)
        self.assertEqual(result.values, ["~/a-state", "~/z-static"])
        self.assertEqual(result.values[0], "~/a-state")


class ClaudeConfigScanDiscoveryTests(unittest.TestCase):
    """Tests for _discover_profiles."""

    def test_profiles_returned_when_found(self) -> None:
        """When discover_profiles() finds profiles, they are returned."""
        from unittest.mock import patch
        from claudewheel.discovery import ProfileInfo

        mock_profiles = [
            ProfileInfo(name="work", path=Path("/fake"), has_credentials=True, has_token=False),
            ProfileInfo(name="personal", path=Path("/fake"), has_credentials=True, has_token=False),
        ]

        config = {
            "values": ["stale-profile"],
            "discovery": {"type": "claude_config_scan"},
        }
        with patch("claudewheel.segment.discover_profiles", return_value=mock_profiles):
            result = _discover_profiles(config, state={})
        self.assertEqual(result.values, ["work", "personal"])

    def test_empty_when_no_profiles_found(self) -> None:
        """When discover_profiles() returns empty, values are empty."""
        from unittest.mock import patch

        config = {
            "values": ["stale-profile"],
            "discovery": {"type": "claude_config_scan"},
        }
        with patch("claudewheel.segment.discover_profiles", return_value=[]):
            result = _discover_profiles(config, state={})
        self.assertEqual(result.values, [])

    def test_metadata_returned(self) -> None:
        """Discovery returns metadata mapping profile names to config dirs."""
        from unittest.mock import patch
        from claudewheel.discovery import ProfileInfo

        mock_profiles = [
            ProfileInfo(name="default", path=Path("/fake"), has_credentials=True, has_token=False),
            ProfileInfo(name="myprof", path=Path("/fake"), has_credentials=True, has_token=False),
        ]

        config = {
            "values": [],
            "discovery": {"type": "claude_config_scan"},
        }
        with patch("claudewheel.segment.discover_profiles", return_value=mock_profiles):
            result = _discover_profiles(config, state={})
        self.assertEqual(result.metadata["default"]["config_dir"], "~/.claude")
        self.assertEqual(result.metadata["myprof"]["config_dir"], "~/.claudewheel/profiles/myprof")


class GhAuthDiscoveryTests(unittest.TestCase):
    """Tests for _discover_gh_accounts."""

    def test_discovered_accounts_appended_to_static(self) -> None:
        """Static values come first, discovered accounts are appended."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Logged in to github.com account ghuser1 (token)\n"
        mock_result.stderr = ""

        config = {
            "values": ["static-acct"],
            "discovery": {"type": "gh_auth"},
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            result = _discover_gh_accounts(config, state={})
        self.assertEqual(result.values, ["static-acct", "ghuser1"])

    def test_multiple_accounts_appended(self) -> None:
        """Multiple gh accounts are all appended after static values."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "Logged in to github.com account alice (token)\n"
            "Logged in to github.com account bob (token)\n"
        )
        mock_result.stderr = ""

        config = {
            "values": ["preset"],
            "discovery": {"type": "gh_auth"},
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            result = _discover_gh_accounts(config, state={})
        self.assertEqual(result.values, ["preset", "alice", "bob"])

    def test_duplicate_accounts_deduplicated(self) -> None:
        """Duplicate gh accounts are collapsed (only first occurrence kept)."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = (
            "Logged in to github.com account alice (token)\n"
            "Logged in to github.com account alice (token)\n"
        )
        mock_result.stderr = ""

        config = {
            "values": [],
            "discovery": {"type": "gh_auth"},
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            result = _discover_gh_accounts(config, state={})
        self.assertEqual(result.values, ["alice"])

    def test_no_accounts_preserves_static(self) -> None:
        """When gh auth finds nothing, static values remain unchanged."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not logged in"

        config = {
            "values": ["fallback"],
            "discovery": {"type": "gh_auth"},
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            result = _discover_gh_accounts(config, state={})
        self.assertEqual(result.values, ["fallback"])

    def test_gh_not_installed_preserves_static(self) -> None:
        """When gh CLI is not installed, static values survive."""
        from unittest.mock import patch

        config = {
            "values": ["preset"],
            "discovery": {"type": "gh_auth"},
        }
        with patch("claudewheel.segment.subprocess.run", side_effect=FileNotFoundError):
            result = _discover_gh_accounts(config, state={})
        self.assertEqual(result.values, ["preset"])


class NpmAndLocalCachedDiscoveryTests(unittest.TestCase):
    """Tests for _discover_npm_and_local_cached (fast-path with cache)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_cached_npm_with_local_merge(self) -> None:
        """Cached npm versions are merged with local-only versions, sorted."""
        (self.tmp / "1.0.1").write_text("")
        (self.tmp / "1.0.3").write_text("")

        config = {
            "values": [],
            "discovery": {
                "type": "npm_and_local",
                "path": str(self.tmp),
                "count": 5,
            },
        }
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time(),
                "versions": ["1.0.0", "1.0.1", "1.0.2"],
            }
        }
        result = _discover_npm_and_local_cached(config, state)
        self.assertIn("1.0.3", result.values)
        self.assertIn("1.0.2", result.values)
        self.assertIn("1.0.1", result.values)
        self.assertIn("1.0.0", result.values)
        # Sorted descending
        self.assertEqual(
            result.values,
            sorted(result.values, key=lambda v: [int(p) for p in v.split(".")], reverse=True),
        )

    def test_local_only_versions_included(self) -> None:
        """Versions that exist locally but not in npm are included."""
        (self.tmp / "99.0.0").write_text("")

        config = {
            "values": [],
            "discovery": {
                "type": "npm_and_local",
                "path": str(self.tmp),
                "count": 3,
            },
        }
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time(),
                "versions": ["1.0.0", "1.0.1", "1.0.2"],
            }
        }
        result = _discover_npm_and_local_cached(config, state)
        self.assertEqual(result.values[0], "99.0.0")

    def test_installed_set_populated(self) -> None:
        """The installed set is populated from local files."""
        (self.tmp / "1.0.0").write_text("")

        config = {
            "values": [],
            "discovery": {
                "type": "npm_and_local",
                "path": str(self.tmp),
                "count": 3,
            },
        }
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time(),
                "versions": ["1.0.0"],
            }
        }
        result = _discover_npm_and_local_cached(config, state)
        self.assertEqual(result.installed, {"1.0.0"})

    def test_empty_npm_and_empty_local_yields_empty(self) -> None:
        """When both npm and local are empty, result is empty."""
        empty_dir = self.tmp / "empty"
        empty_dir.mkdir()

        config = {
            "values": [],
            "discovery": {
                "type": "npm_and_local",
                "path": str(empty_dir),
                "count": 3,
            },
        }
        state = {}
        result = _discover_npm_and_local_cached(config, state)
        self.assertEqual(result.values, [])

    def test_stale_cache_uses_empty_npm_list(self) -> None:
        """When cache is stale, npm list is empty but local files still appear."""
        (self.tmp / "1.0.0").write_text("")

        config = {
            "values": [],
            "discovery": {
                "type": "npm_and_local",
                "path": str(self.tmp),
                "count": 3,
            },
        }
        # Cache from 2 hours ago (stale, TTL is 1 hour)
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time() - 7200,
                "versions": ["2.0.0"],
            }
        }
        result = _discover_npm_and_local_cached(config, state)
        # Only local version should appear (stale npm cache is ignored)
        self.assertEqual(result.values, ["1.0.0"])
        self.assertEqual(result.installed, {"1.0.0"})


class ParseStaticValuesTests(unittest.TestCase):
    def test_plain_strings(self) -> None:
        config = {"values": ["a", "b", "c"]}
        self.assertEqual(_parse_static_values(config), ["a", "b", "c"])

    def test_dict_values_unwrapped(self) -> None:
        config = {"values": ["a", {"value": "b", "requires": {"ver": ">=1"}}]}
        self.assertEqual(_parse_static_values(config), ["a", "b"])

    def test_empty_values(self) -> None:
        config = {"values": []}
        self.assertEqual(_parse_static_values(config), [])

    def test_missing_values_key(self) -> None:
        config = {}
        self.assertEqual(_parse_static_values(config), [])


class ParseRequiresTests(unittest.TestCase):
    def test_dict_values_with_requires(self) -> None:
        config = {
            "values": ["a", {"value": "b", "requires": {"ver": ">=1"}}],
        }
        self.assertEqual(_parse_requires(config), {"b": {"ver": ">=1"}})

    def test_no_requires(self) -> None:
        config = {"values": ["a", "b"]}
        self.assertEqual(_parse_requires(config), {})

    def test_dict_value_without_requires(self) -> None:
        config = {"values": [{"value": "a"}, "b"]}
        self.assertEqual(_parse_requires(config), {})


class DiscoveryResultTests(unittest.TestCase):
    def test_defaults(self) -> None:
        """DiscoveryResult has sensible defaults for all fields."""
        dr = DiscoveryResult()
        self.assertEqual(dr.values, [])
        self.assertEqual(dr.installed, set())
        self.assertEqual(dr.requires, {})
        self.assertEqual(dr.metadata, {})

    def test_fields_populated(self) -> None:
        """All fields can be populated."""
        dr = DiscoveryResult(
            values=["a", "b"],
            installed={"a"},
            requires={"b": {"ver": ">=1"}},
            metadata={"a": {"key": "val"}},
        )
        self.assertEqual(dr.values, ["a", "b"])
        self.assertEqual(dr.installed, {"a"})
        self.assertEqual(dr.requires, {"b": {"ver": ">=1"}})
        self.assertEqual(dr.metadata, {"a": {"key": "val"}})


if __name__ == "__main__":
    unittest.main()
