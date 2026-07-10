"""Tests for individual discovery functions in the registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.segment import (
    DiscoveryResult,
    Segment,
    SegmentBar,
    merge_slow_results,
    _discover_directory_listing,
    _discover_directory_scan,
    _discover_gh_accounts,
    _discover_npm_and_local_cached,
    _discover_profiles,
    _discover_state_field,
    _parse_requires,
    _parse_static_values,
)



def _mock_ws(profiles):
    """A workspace stand-in whose profiles.enumerate() returns *profiles*."""
    from unittest.mock import MagicMock
    ws = MagicMock()
    ws.profiles.enumerate.return_value = profiles
    return ws


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
        result = _discover_directory_listing(config, state={}, ws=None)
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
        result = _discover_directory_listing(config, state={}, ws=None)
        self.assertEqual(result.values, [])

    def test_empty_directory_returns_empty(self) -> None:
        """An empty (but existing) directory yields empty list."""
        config = {
            "values": ["static-a"],
            "discovery": {"type": "directory_listing", "path": str(self.tmp)},
        }
        result = _discover_directory_listing(config, state={}, ws=None)
        self.assertEqual(result.values, [])

    def test_directories_inside_path_are_excluded(self) -> None:
        """Only files are listed; subdirectories are excluded."""
        (self.tmp / "1.0.0").write_text("")
        (self.tmp / "subdir").mkdir()

        config = {
            "values": [],
            "discovery": {"type": "directory_listing", "path": str(self.tmp)},
        }
        result = _discover_directory_listing(config, state={}, ws=None)
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
        result = _discover_directory_scan(config, state={}, ws=None)
        names = [Path(p).name for p in result.values]
        self.assertEqual(names, ["proj-a", "proj-b"])
        self.assertNotIn(".hidden", names)

    def test_validated_recent_dirs_first_then_scanned(self) -> None:
        """Validated recent dirs appear first, then parent-scan results."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "alpha").mkdir()
        # Create a real directory for the recent dir to validate against
        recent_dir = self.tmp / "recent-project"
        recent_dir.mkdir()

        config = {
            "values": ["/static/path"],  # static values are ignored (handled by defaults)
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                "state_field": "recent_dirs",
            },
        }
        state = {"recent_dirs": [str(recent_dir)]}
        result = _discover_directory_scan(config, state, ws=None)
        # recent first
        self.assertEqual(result.values[0], str(recent_dir))
        # static values are NOT in the result (handled by SegmentState.defaults)
        self.assertNotIn("/static/path", result.values)
        # Only recent + scanned
        self.assertEqual(len(result.values), 2)

    def test_invalid_recent_dirs_filtered_out(self) -> None:
        """Non-existent recent dirs are excluded from the result."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "alpha").mkdir()
        # Create one real and one fake recent dir
        real_dir = self.tmp / "real-project"
        real_dir.mkdir()

        config = {
            "values": [],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                "state_field": "recent_dirs",
            },
        }
        state = {"recent_dirs": [str(real_dir), "/nonexistent/path/abc123"]}
        result = _discover_directory_scan(config, state, ws=None)
        self.assertIn(str(real_dir), result.values)
        self.assertNotIn("/nonexistent/path/abc123", result.values)

    def test_stale_recent_dirs_pruned_from_state(self) -> None:
        """Stale (non-existent) recent dirs are removed from the state dict."""
        real_dir = self.tmp / "real-project"
        real_dir.mkdir()

        config = {
            "values": [],
            "discovery": {
                "type": "directory_scan",
                "parents": [],
                "state_field": "recent_dirs",
            },
        }
        state = {"recent_dirs": [str(real_dir), "/stale/gone/path"]}
        _discover_directory_scan(config, state, ws=None)
        # State should be pruned to only the valid dir
        self.assertEqual(state["recent_dirs"], [str(real_dir)])

    def test_dedup_between_recent_and_scanned(self) -> None:
        """Duplicate entries across recent and scanned are collapsed."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "dup").mkdir()
        discovered_path = str(parent / "dup")

        config = {
            "values": [],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                "state_field": "recent_dirs",
            },
        }
        state = {"recent_dirs": [discovered_path]}
        result = _discover_directory_scan(config, state, ws=None)
        self.assertEqual(result.values.count(discovered_path), 1)

    def test_no_state_field_means_no_recent_dirs(self) -> None:
        """When state_field is absent from discovery config, recent is empty."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "proj").mkdir()

        config = {
            "values": [],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                # no state_field key
            },
        }
        result = _discover_directory_scan(
            config, state={"recent_dirs": ["/should-not-appear"]}, ws=None,
        )
        self.assertNotIn("/should-not-appear", result.values)
        # Only scanned dirs
        self.assertEqual(len(result.values), 1)

    def test_empty_parents_and_no_recent_yields_empty(self) -> None:
        """If parents list is empty and no recent dirs, result is empty."""
        config = {
            "values": ["/fallback"],  # static values are ignored
            "discovery": {
                "type": "directory_scan",
                "parents": [],
                "state_field": "recent_dirs",
            },
        }
        result = _discover_directory_scan(config, state={}, ws=None)
        # Static values no longer appear in discovery result
        self.assertEqual(result.values, [])

    def test_static_values_not_included_in_result(self) -> None:
        """Static values from config are not in discovery result (handled by defaults)."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "alpha").mkdir()

        config = {
            "values": ["/my/static/dir", "/another/static"],
            "discovery": {
                "type": "directory_scan",
                "parents": [str(parent)],
                "state_field": "recent_dirs",
            },
        }
        result = _discover_directory_scan(config, state={}, ws=None)
        self.assertNotIn("/my/static/dir", result.values)
        self.assertNotIn("/another/static", result.values)


class StateFieldDiscoveryTests(unittest.TestCase):
    def test_state_first_then_static_deduplicated(self) -> None:
        """state values come first; static values appended; duplicates removed."""
        config = {
            "values": ["~/baz"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        state = {"recent_dirs": ["~/foo", "~/bar"]}
        result = _discover_state_field(config, state, ws=None)
        self.assertEqual(result.values, ["~/foo", "~/bar", "~/baz"])

    def test_dedup_when_static_overlaps_state(self) -> None:
        """An overlap between state and static is collapsed to a single occurrence."""
        config = {
            "values": ["~/foo", "~/baz"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        state = {"recent_dirs": ["~/foo", "~/bar"]}
        result = _discover_state_field(config, state, ws=None)
        self.assertEqual(result.values, ["~/foo", "~/bar", "~/baz"])

    def test_missing_state_field_uses_static_only(self) -> None:
        """If the state key is absent, only the static values come through."""
        config = {
            "values": ["~/baz"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        result = _discover_state_field(config, state={}, ws=None)
        self.assertEqual(result.values, ["~/baz"])

    def test_empty_state_and_empty_static_yields_empty(self) -> None:
        """When both state field and static values are empty, result is empty."""
        config = {
            "values": [],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        result = _discover_state_field(config, state={"recent_dirs": []}, ws=None)
        self.assertEqual(result.values, [])

    def test_state_values_come_before_static(self) -> None:
        """State values strictly precede static values in the result."""
        config = {
            "values": ["~/z-static"],
            "discovery": {"type": "state_field", "field": "recent_dirs"},
        }
        state = {"recent_dirs": ["~/a-state"]}
        result = _discover_state_field(config, state, ws=None)
        self.assertEqual(result.values, ["~/a-state", "~/z-static"])
        self.assertEqual(result.values[0], "~/a-state")


class ClaudeConfigScanDiscoveryTests(unittest.TestCase):
    """Tests for _discover_profiles."""

    def test_profiles_returned_when_found(self) -> None:
        """When the store enumerates profiles, their names are returned."""
        from unittest.mock import patch
        from claudewheel.profile_store import Profile

        mock_profiles = [
            Profile(name="work", path=Path("/fake/work"), has_credentials=True, has_token=False),
            Profile(name="personal", path=Path("/fake/personal"), has_credentials=True, has_token=False),
        ]

        config = {
            "values": ["stale-profile"],
            "discovery": {"type": "claude_config_scan"},
        }
        mock_ws = _mock_ws(mock_profiles)
        result = _discover_profiles(config, state={}, ws=mock_ws)
        self.assertEqual(result.values, ["work", "personal"])

    def test_empty_when_no_profiles_found(self) -> None:
        """When the store enumerates nothing, values are empty."""
        from unittest.mock import patch

        config = {
            "values": ["stale-profile"],
            "discovery": {"type": "claude_config_scan"},
        }
        mock_ws = _mock_ws([])
        result = _discover_profiles(config, state={}, ws=mock_ws)
        self.assertEqual(result.values, [])

    def test_metadata_carries_auth_fields_only(self) -> None:
        """Metadata carries has_token/has_credentials -- never a config_dir
        (profile identity comes from the store, not persisted metadata)."""
        from unittest.mock import patch
        from claudewheel.profile_store import Profile

        mock_profiles = [
            Profile(name="default", path=Path("/fake/default"), has_credentials=True, has_token=False),
            Profile(name="myprof", path=Path("/fake/myprof"), has_credentials=False, has_token=True),
        ]

        config = {
            "values": [],
            "discovery": {"type": "claude_config_scan"},
        }
        mock_ws = _mock_ws(mock_profiles)
        result = _discover_profiles(config, state={}, ws=mock_ws)
        self.assertNotIn("config_dir", result.metadata["default"])
        self.assertNotIn("config_dir", result.metadata["myprof"])
        self.assertEqual(result.metadata["default"]["has_credentials"], True)
        self.assertEqual(result.metadata["default"]["has_token"], False)
        self.assertEqual(result.metadata["myprof"]["has_credentials"], False)
        self.assertEqual(result.metadata["myprof"]["has_token"], True)


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
            result = _discover_gh_accounts(config, state={}, ws=None)
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
            result = _discover_gh_accounts(config, state={}, ws=None)
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
            result = _discover_gh_accounts(config, state={}, ws=None)
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
            result = _discover_gh_accounts(config, state={}, ws=None)
        self.assertEqual(result.values, ["fallback"])

    def test_gh_not_installed_preserves_static(self) -> None:
        """When gh CLI is not installed, static values survive."""
        from unittest.mock import patch

        config = {
            "values": ["preset"],
            "discovery": {"type": "gh_auth"},
        }
        with patch("claudewheel.segment.subprocess.run", side_effect=FileNotFoundError):
            result = _discover_gh_accounts(config, state={}, ws=None)
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
        result = _discover_npm_and_local_cached(config, state, ws=None)
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
        result = _discover_npm_and_local_cached(config, state, ws=None)
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
        result = _discover_npm_and_local_cached(config, state, ws=None)
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
        result = _discover_npm_and_local_cached(config, state, ws=None)
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
        result = _discover_npm_and_local_cached(config, state, ws=None)
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


class LastConfigSelectionOnlyTests(unittest.TestCase):
    """Verify last_config is selection-only: it never injects values into options."""

    def test_build_segment_bar_last_config_nonexistent_value(self) -> None:
        """select_value with a non-existent value does not inject it into options."""
        seg = Segment(key="directory", label="Dir")
        seg.state.set_discovered(["/existing/a", "/existing/b"])
        # Simulate last_config with a value that doesn't exist in options
        found = seg.select_value("/gone/deleted/path")
        self.assertFalse(found)
        # Value should NOT be injected
        self.assertNotIn("/gone/deleted/path", seg.options)
        self.assertEqual(seg.options, ["/existing/a", "/existing/b"])
        # Selection should remain unselected
        self.assertEqual(seg.selected_idx, -1)
        self.assertIsNone(seg.value)

    def test_merge_slow_results_last_config_nonexistent_value(self) -> None:
        """merge_slow_results with last_config pointing to absent value does not inject."""
        seg = Segment(key="directory", label="Dir")
        seg.state.set_discovered(["/old/path"])
        bar = SegmentBar(segments=[seg])
        # Slow results replace discovered, and last_config references a non-existent path
        results = {"directory": DiscoveryResult(values=["/new/a", "/new/b"])}
        state = {"last_config": {"directory": "/gone/stale/path"}}
        merge_slow_results(bar, results, state)
        self.assertNotIn("/gone/stale/path", seg.options)
        self.assertEqual(seg.options, ["/new/a", "/new/b"])

    def test_merge_slow_results_last_config_existing_value_selects(self) -> None:
        """merge_slow_results with last_config pointing to existing value selects it."""
        seg = Segment(key="directory", label="Dir")
        seg.state.set_discovered(["/old/path"])
        bar = SegmentBar(segments=[seg])
        results = {"directory": DiscoveryResult(values=["/new/a", "/new/b"])}
        state = {"last_config": {"directory": "/new/b"}}
        merge_slow_results(bar, results, state)
        self.assertEqual(seg.value, "/new/b")


class DetectBrowsersTests(unittest.TestCase):
    """Tests for detect_browsers() in claudewheel.discovery."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # Empty flatpak/snap dirs by default; individual tests populate them.
        self.flatpak_dir = self.tmp / "flatpak-exports"
        self.flatpak_dir.mkdir()
        self.snap_dir = self.tmp / "snap-bin"
        self.snap_dir.mkdir()
        self._patches = [
            patch(
                "claudewheel.discovery._FLATPAK_EXPORT_DIRS",
                [self.flatpak_dir],
            ),
            patch("claudewheel.discovery._SNAP_BIN_DIR", self.snap_dir),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_native_browser_found_via_which(self) -> None:
        """A native binary on PATH is detected with its display name."""
        from claudewheel.discovery import detect_browsers

        def fake_which(binary: str) -> str | None:
            return "/usr/bin/firefox" if binary == "firefox" else None

        with patch("claudewheel.discovery.shutil.which", side_effect=fake_which):
            result = detect_browsers()
        self.assertEqual(result, [("/usr/bin/firefox", "Firefox")])

    def test_flatpak_browser_found_in_export_dir(self) -> None:
        """A flatpak export symlink is detected with the full path."""
        from claudewheel.discovery import detect_browsers

        (self.flatpak_dir / "com.brave.Browser").write_text("")

        with patch("claudewheel.discovery.shutil.which", return_value=None):
            result = detect_browsers()
        self.assertEqual(
            result, [(str(self.flatpak_dir / "com.brave.Browser"), "Brave")],
        )

    def test_snap_browser_found_in_snap_dir(self) -> None:
        """A snap binary is detected with the full path."""
        from claudewheel.discovery import detect_browsers

        (self.snap_dir / "chromium").write_text("")

        with patch("claudewheel.discovery.shutil.which", return_value=None):
            result = detect_browsers()
        self.assertEqual(result, [(str(self.snap_dir / "chromium"), "Chromium")])

    def test_native_wins_over_flatpak_dedup(self) -> None:
        """A browser found natively is not duplicated from flatpak or snap."""
        from claudewheel.discovery import detect_browsers

        (self.flatpak_dir / "org.mozilla.firefox").write_text("")
        (self.snap_dir / "firefox").write_text("")

        def fake_which(binary: str) -> str | None:
            return "/usr/bin/firefox" if binary == "firefox" else None

        with patch("claudewheel.discovery.shutil.which", side_effect=fake_which):
            result = detect_browsers()
        self.assertEqual(result, [("/usr/bin/firefox", "Firefox")])

    def test_tuple_order_is_path_then_name(self) -> None:
        """Each result tuple is (binary_path, display_name)."""
        from claudewheel.discovery import detect_browsers

        def fake_which(binary: str) -> str | None:
            return "/usr/bin/qutebrowser" if binary == "qutebrowser" else None

        with patch("claudewheel.discovery.shutil.which", side_effect=fake_which):
            result = detect_browsers()
        self.assertEqual(len(result), 1)
        path, name = result[0]
        self.assertTrue(path.startswith("/"))
        self.assertEqual(name, "Qutebrowser")

    def test_empty_system_yields_empty_list(self) -> None:
        """No native, flatpak, or snap browsers -> empty list."""
        from claudewheel.discovery import detect_browsers

        with patch("claudewheel.discovery.shutil.which", return_value=None):
            result = detect_browsers()
        self.assertEqual(result, [])

    def test_source_ordering_native_then_flatpak_then_snap(self) -> None:
        """Results are ordered native first, then flatpak, then snap."""
        from claudewheel.discovery import detect_browsers

        (self.flatpak_dir / "com.vivaldi.Vivaldi").write_text("")
        (self.snap_dir / "opera").write_text("")

        def fake_which(binary: str) -> str | None:
            return "/usr/bin/firefox" if binary == "firefox" else None

        with patch("claudewheel.discovery.shutil.which", side_effect=fake_which):
            result = detect_browsers()
        self.assertEqual(
            result,
            [
                ("/usr/bin/firefox", "Firefox"),
                (str(self.flatpak_dir / "com.vivaldi.Vivaldi"), "Vivaldi"),
                (str(self.snap_dir / "opera"), "Opera"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
