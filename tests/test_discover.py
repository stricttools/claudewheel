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
