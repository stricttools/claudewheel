"""Smoke tests for discover_options across every supported discovery type."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claudewheel.segment import discover_options


class DirectoryListingDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_files_sorted_by_version_descending(self) -> None:
        """directory_listing returns file names sorted newest-first by numeric version."""
        for name in ("1.0.0", "1.0.10", "1.0.2"):
            (self.tmp / name).write_text("")

        options_def = {
            "version": {
                "values": [],
                "discovery": {"type": "directory_listing", "path": str(self.tmp)},
            }
        }
        resolved = discover_options(options_def, state={})
        # 1.0.10 must come before 1.0.2 (numeric, not alphabetical)
        self.assertEqual(resolved["version"], ["1.0.10", "1.0.2", "1.0.0"])

    def test_missing_directory_falls_back_to_static_values(self) -> None:
        """If the directory does not exist, the static values list is preserved."""
        options_def = {
            "version": {
                "values": ["fallback"],
                "discovery": {
                    "type": "directory_listing",
                    "path": str(self.tmp / "nope"),
                },
            }
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["version"], ["fallback"])


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
        options_def = {
            "directory": {
                "values": [],
                "discovery": {
                    "type": "directory_scan",
                    "parents": [str(self.parent)],
                },
            }
        }
        resolved = discover_options(options_def, state={})
        result = resolved["directory"]
        # The result is sorted alphabetically by the iterdir scan.
        # Each entry is either a "~/..." path (if under HOME) or an absolute path.
        # We only check the names match, not the prefix style.
        names = [Path(p).name for p in result]
        self.assertEqual(names, ["proj-a", "proj-b"])
        self.assertNotIn(".hidden", names)


class StateFieldDiscoveryTests(unittest.TestCase):
    def test_state_first_then_static_deduplicated(self) -> None:
        """state values come first; static values appended; duplicates removed."""
        options_def = {
            "directory": {
                "values": ["~/baz"],
                "discovery": {"type": "state_field", "field": "recent_dirs"},
            }
        }
        state = {"recent_dirs": ["~/foo", "~/bar"]}
        resolved = discover_options(options_def, state)
        # Recent first, then static. No duplicates.
        self.assertEqual(resolved["directory"], ["~/foo", "~/bar", "~/baz"])

    def test_dedup_when_static_overlaps_state(self) -> None:
        """An overlap between state and static is collapsed to a single occurrence."""
        options_def = {
            "directory": {
                "values": ["~/foo", "~/baz"],
                "discovery": {"type": "state_field", "field": "recent_dirs"},
            }
        }
        state = {"recent_dirs": ["~/foo", "~/bar"]}
        resolved = discover_options(options_def, state)
        # ~/foo appears in both -- it should appear once, in its state position.
        self.assertEqual(resolved["directory"], ["~/foo", "~/bar", "~/baz"])

    def test_missing_state_field_uses_static_only(self) -> None:
        """If the state key is absent, only the static values come through."""
        options_def = {
            "directory": {
                "values": ["~/baz"],
                "discovery": {"type": "state_field", "field": "recent_dirs"},
            }
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["directory"], ["~/baz"])


class DirectoryListingMergeBehaviorTests(unittest.TestCase):
    """Regression tests: directory_listing REPLACES static values entirely."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_static_values_replaced_when_dir_has_entries(self) -> None:
        """Static values are discarded when the directory contains files."""
        (self.tmp / "2.0.0").write_text("")
        (self.tmp / "1.0.0").write_text("")

        options_def = {
            "version": {
                "values": ["static-a", "static-b"],
                "discovery": {"type": "directory_listing", "path": str(self.tmp)},
            }
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["version"], ["2.0.0", "1.0.0"])
        self.assertNotIn("static-a", resolved["version"])
        self.assertNotIn("static-b", resolved["version"])

    def test_static_values_survive_when_dir_is_empty(self) -> None:
        """An empty (but existing) directory still replaces -- yields empty list."""
        options_def = {
            "version": {
                "values": ["static-a"],
                "discovery": {"type": "directory_listing", "path": str(self.tmp)},
            }
        }
        resolved = discover_options(options_def, state={})
        # The directory exists but has no files -- values are replaced with []
        self.assertEqual(resolved["version"], [])

    def test_directories_inside_path_are_excluded(self) -> None:
        """Only files are listed; subdirectories are excluded."""
        (self.tmp / "1.0.0").write_text("")
        (self.tmp / "subdir").mkdir()

        options_def = {
            "version": {
                "values": [],
                "discovery": {"type": "directory_listing", "path": str(self.tmp)},
            }
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["version"], ["1.0.0"])
        self.assertNotIn("subdir", resolved["version"])


class NpmAndLocalMergeBehaviorTests(unittest.TestCase):
    """Regression tests: npm_and_local REPLACES static values with merged npm + local."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_static_values_replaced_by_npm_and_local(self) -> None:
        """Static values are discarded; result is npm + local-only, sorted."""
        (self.tmp / "1.0.1").write_text("")
        (self.tmp / "1.0.3").write_text("")

        options_def = {
            "version": {
                "values": ["static-should-vanish"],
                "discovery": {
                    "type": "npm_and_local",
                    "path": str(self.tmp),
                    "count": 5,
                },
            }
        }
        # Use skip_slow with a warm cache to avoid hitting real npm
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time(),
                "versions": ["1.0.0", "1.0.1", "1.0.2"],
            }
        }
        resolved = discover_options(options_def, state, skip_slow=True)
        result = resolved["version"]
        # Static values must be gone
        self.assertNotIn("static-should-vanish", result)
        # npm versions + local-only (1.0.3 not in npm list)
        self.assertIn("1.0.3", result)
        self.assertIn("1.0.2", result)
        self.assertIn("1.0.1", result)
        self.assertIn("1.0.0", result)
        # Sorted descending by version
        self.assertEqual(result, sorted(result, key=lambda v: [int(p) for p in v.split(".")], reverse=True))

    def test_local_only_versions_appended_before_sort(self) -> None:
        """Versions that exist locally but not in npm are included."""
        (self.tmp / "99.0.0").write_text("")

        options_def = {
            "version": {
                "values": [],
                "discovery": {
                    "type": "npm_and_local",
                    "path": str(self.tmp),
                    "count": 3,
                },
            }
        }
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time(),
                "versions": ["1.0.0", "1.0.1", "1.0.2"],
            }
        }
        resolved = discover_options(options_def, state, skip_slow=True)
        result = resolved["version"]
        # 99.0.0 is local-only, should appear first (highest version)
        self.assertEqual(result[0], "99.0.0")

    def test_installed_set_stored_in_resolved(self) -> None:
        """The _installed_<key> set is populated for build_segment_bar."""
        (self.tmp / "1.0.0").write_text("")

        options_def = {
            "version": {
                "values": [],
                "discovery": {
                    "type": "npm_and_local",
                    "path": str(self.tmp),
                    "count": 3,
                },
            }
        }
        state = {
            "npm_versions_cache": {
                "fetched_at": __import__("time").time(),
                "versions": ["1.0.0"],
            }
        }
        resolved = discover_options(options_def, state, skip_slow=True)
        self.assertIn("_installed_version", resolved)
        self.assertEqual(resolved["_installed_version"], {"1.0.0"})

    def test_empty_npm_and_empty_local_yields_empty(self) -> None:
        """When both npm and local are empty, result is empty (static values replaced)."""
        empty_dir = self.tmp / "empty"
        empty_dir.mkdir()

        options_def = {
            "version": {
                "values": ["should-be-gone"],
                "discovery": {
                    "type": "npm_and_local",
                    "path": str(empty_dir),
                    "count": 3,
                },
            }
        }
        # skip_slow with stale/no cache -> empty npm list
        state = {}
        resolved = discover_options(options_def, state, skip_slow=True)
        self.assertEqual(resolved["version"], [])


class DirectoryScanMergeBehaviorTests(unittest.TestCase):
    """Regression tests: directory_scan MERGES recent + discovered + static (deduped)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_merge_order_recent_then_discovered_then_static(self) -> None:
        """Order is: state recent_dirs, then discovered dirs, then static values."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "alpha").mkdir()

        # Use an absolute path NOT under $HOME so we get absolute paths back,
        # making assertions deterministic.
        options_def = {
            "directory": {
                "values": ["/static/path"],
                "discovery": {
                    "type": "directory_scan",
                    "parents": [str(parent)],
                    "state_field": "recent_dirs",
                },
            }
        }
        state = {"recent_dirs": ["/recent/one"]}
        resolved = discover_options(options_def, state)
        result = resolved["directory"]
        # recent first
        self.assertEqual(result[0], "/recent/one")
        # static last
        self.assertEqual(result[-1], "/static/path")
        # discovered in the middle (alpha is under a tmp path, not HOME)
        self.assertEqual(len(result), 3)

    def test_dedup_across_all_three_sources(self) -> None:
        """Duplicate entries across recent, discovered, and static are collapsed."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "dup").mkdir()
        # Compute the discovered path the same way the code does
        discovered_path = str(parent / "dup")

        options_def = {
            "directory": {
                "values": [discovered_path],
                "discovery": {
                    "type": "directory_scan",
                    "parents": [str(parent)],
                    "state_field": "recent_dirs",
                },
            }
        }
        state = {"recent_dirs": [discovered_path]}
        resolved = discover_options(options_def, state)
        result = resolved["directory"]
        # The path appears in all three sources but should appear exactly once
        self.assertEqual(result.count(discovered_path), 1)

    def test_no_state_field_means_no_recent_dirs(self) -> None:
        """When state_field is absent from discovery config, recent is empty."""
        parent = self.tmp / "projects"
        parent.mkdir()
        (parent / "proj").mkdir()

        options_def = {
            "directory": {
                "values": ["/static"],
                "discovery": {
                    "type": "directory_scan",
                    "parents": [str(parent)],
                    # no state_field key
                },
            }
        }
        resolved = discover_options(options_def, state={"recent_dirs": ["/should-not-appear"]})
        result = resolved["directory"]
        self.assertNotIn("/should-not-appear", result)
        self.assertEqual(result[-1], "/static")

    def test_empty_parents_uses_static_values(self) -> None:
        """If parents list is empty, discovered is empty, but static values survive."""
        options_def = {
            "directory": {
                "values": ["/fallback"],
                "discovery": {
                    "type": "directory_scan",
                    "parents": [],
                    "state_field": "recent_dirs",
                },
            }
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["directory"], ["/fallback"])


class ClaudeConfigScanMergeBehaviorTests(unittest.TestCase):
    """Regression tests: claude_config_scan conditionally REPLACES values."""

    def test_static_values_replaced_when_profiles_found(self) -> None:
        """When discover_profiles() finds profiles, static values are discarded."""
        from unittest.mock import patch
        from claudewheel.discovery import ProfileInfo

        mock_profiles = [
            ProfileInfo(name="work", path=Path("/fake"), has_credentials=True, has_token=False),
            ProfileInfo(name="personal", path=Path("/fake"), has_credentials=True, has_token=False),
        ]

        options_def = {
            "profile": {
                "values": ["stale-profile"],
                "discovery": {"type": "claude_config_scan"},
            }
        }
        with patch("claudewheel.segment.discover_profiles", return_value=mock_profiles):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["profile"], ["work", "personal"])
        self.assertNotIn("stale-profile", resolved["profile"])

    def test_static_values_kept_when_no_profiles_found(self) -> None:
        """When discover_profiles() returns empty, static values survive."""
        from unittest.mock import patch

        options_def = {
            "profile": {
                "values": ["stale-profile"],
                "discovery": {"type": "claude_config_scan"},
            }
        }
        with patch("claudewheel.segment.discover_profiles", return_value=[]):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["profile"], ["stale-profile"])

    def test_metadata_set_on_options_def(self) -> None:
        """Discovery writes metadata mapping profile names to config dirs."""
        from unittest.mock import patch
        from claudewheel.discovery import ProfileInfo

        mock_profiles = [
            ProfileInfo(name="default", path=Path("/fake"), has_credentials=True, has_token=False),
            ProfileInfo(name="myprof", path=Path("/fake"), has_credentials=True, has_token=False),
        ]

        options_def = {
            "profile": {
                "values": [],
                "discovery": {"type": "claude_config_scan"},
            }
        }
        with patch("claudewheel.segment.discover_profiles", return_value=mock_profiles):
            discover_options(options_def, state={})
        metadata = options_def["profile"]["metadata"]
        self.assertEqual(metadata["default"]["config_dir"], "~/.claude")
        self.assertEqual(metadata["myprof"]["config_dir"], "~/.claudewheel/profiles/myprof")


class GhAuthMergeBehaviorTests(unittest.TestCase):
    """Regression tests: gh_auth APPENDS discovered accounts to static values."""

    def test_discovered_accounts_appended_to_static(self) -> None:
        """Static values come first, discovered accounts are appended."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Logged in to github.com account ghuser1 (token)\n"
        mock_result.stderr = ""

        options_def = {
            "gh_account": {
                "values": ["static-acct"],
                "discovery": {"type": "gh_auth"},
            }
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["gh_account"], ["static-acct", "ghuser1"])

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

        options_def = {
            "gh_account": {
                "values": ["preset"],
                "discovery": {"type": "gh_auth"},
            }
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["gh_account"], ["preset", "alice", "bob"])

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

        options_def = {
            "gh_account": {
                "values": [],
                "discovery": {"type": "gh_auth"},
            }
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["gh_account"], ["alice"])

    def test_no_accounts_preserves_static(self) -> None:
        """When gh auth finds nothing, static values remain unchanged."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not logged in"

        options_def = {
            "gh_account": {
                "values": ["fallback"],
                "discovery": {"type": "gh_auth"},
            }
        }
        with patch("claudewheel.segment.subprocess.run", return_value=mock_result):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["gh_account"], ["fallback"])

    def test_skipped_when_slow_skip_enabled(self) -> None:
        """gh_auth is skipped entirely when skip_slow=True, preserving static values."""
        options_def = {
            "gh_account": {
                "values": ["static-only"],
                "discovery": {"type": "gh_auth"},
            }
        }
        # No mock needed -- subprocess.run should never be called
        resolved = discover_options(options_def, state={}, skip_slow=True)
        self.assertEqual(resolved["gh_account"], ["static-only"])

    def test_gh_not_installed_preserves_static(self) -> None:
        """When gh CLI is not installed, static values survive."""
        from unittest.mock import patch

        options_def = {
            "gh_account": {
                "values": ["preset"],
                "discovery": {"type": "gh_auth"},
            }
        }
        with patch("claudewheel.segment.subprocess.run", side_effect=FileNotFoundError):
            resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["gh_account"], ["preset"])


class StateFieldMergeBehaviorTests(unittest.TestCase):
    """Regression tests for additional state_field edge cases (supplements existing tests)."""

    def test_empty_state_and_empty_static_yields_empty(self) -> None:
        """When both state field and static values are empty, result is empty."""
        options_def = {
            "directory": {
                "values": [],
                "discovery": {"type": "state_field", "field": "recent_dirs"},
            }
        }
        resolved = discover_options(options_def, state={"recent_dirs": []})
        self.assertEqual(resolved["directory"], [])

    def test_state_values_come_before_static(self) -> None:
        """State values strictly precede static values in the result."""
        options_def = {
            "directory": {
                "values": ["~/z-static"],
                "discovery": {"type": "state_field", "field": "recent_dirs"},
            }
        }
        state = {"recent_dirs": ["~/a-state"]}
        resolved = discover_options(options_def, state)
        self.assertEqual(resolved["directory"], ["~/a-state", "~/z-static"])
        # Confirm ordering -- state value is at index 0
        self.assertEqual(resolved["directory"][0], "~/a-state")


class OptionRequiresParsingTests(unittest.TestCase):
    def test_dict_values_split_into_value_and_requires(self) -> None:
        """Object values are unpacked: plain string into options, requires into _requires_<key>."""
        options_def = {
            "permissions": {
                "values": ["a", {"value": "b", "requires": {"ver": ">=1"}}],
            }
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["permissions"], ["a", "b"])
        self.assertIn("_requires_permissions", resolved)
        self.assertEqual(
            resolved["_requires_permissions"], {"b": {"ver": ">=1"}}
        )

    def test_no_requires_key_when_no_dict_values(self) -> None:
        """If no value carries 'requires', the _requires_<key> entry is omitted."""
        options_def = {"permissions": {"values": ["a", "b"]}}
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["permissions"], ["a", "b"])
        self.assertNotIn("_requires_permissions", resolved)

    def test_dict_value_without_requires_still_unpacked(self) -> None:
        """A {value: ...} object without 'requires' contributes only to the option list."""
        options_def = {
            "permissions": {"values": [{"value": "a"}, "b"]},
        }
        resolved = discover_options(options_def, state={})
        self.assertEqual(resolved["permissions"], ["a", "b"])
        self.assertNotIn("_requires_permissions", resolved)


if __name__ == "__main__":
    unittest.main()
