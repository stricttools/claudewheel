"""Tests for claudewheel.gc — garbage collection for shared infrastructure."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel import gc
from claudewheel.gc import (
    _clean_sentinels,
    _compact_origins,
    _known_profiles,
    run_gc,
)

# A few fixed UUIDs used across tests.
UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# _clean_sentinels
# ---------------------------------------------------------------------------


class CleanSentinelsTests(unittest.TestCase):
    """Sentinel file cleanup based on age."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.shared = Path(self._tmp.name) / "shared"
        self.shared.mkdir()
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    @patch.object(gc, "SHARED_DIR")
    def test_removes_sentinels_older_than_30_days(self, mock_dir) -> None:
        mock_dir.__class__ = Path
        # Reassign to actual Path so .is_dir() / .iterdir() work
        gc.SHARED_DIR = self.shared
        old_sentinel = self.shared / ".stamped-old-file"
        old_sentinel.write_text("")
        # Set mtime to 31 days ago
        old_time = time.time() - 31 * 24 * 3600
        os.utime(old_sentinel, (old_time, old_time))

        with patch.object(gc, "SHARED_DIR", self.shared):
            removed = _clean_sentinels(dry_run=False)

        self.assertEqual(removed, 1)
        self.assertFalse(old_sentinel.exists())

    def test_keeps_sentinels_newer_than_30_days(self) -> None:
        new_sentinel = self.shared / ".stamped-recent"
        new_sentinel.write_text("")
        # mtime is "now" by default, well within 30 days

        with patch.object(gc, "SHARED_DIR", self.shared):
            removed = _clean_sentinels(dry_run=False)

        self.assertEqual(removed, 0)
        self.assertTrue(new_sentinel.exists())

    def test_ignores_non_sentinel_files(self) -> None:
        other = self.shared / "some-other-file"
        other.write_text("")
        old_time = time.time() - 60 * 24 * 3600
        os.utime(other, (old_time, old_time))

        with patch.object(gc, "SHARED_DIR", self.shared):
            removed = _clean_sentinels(dry_run=False)

        self.assertEqual(removed, 0)
        self.assertTrue(other.exists())

    def test_dry_run_counts_but_does_not_delete(self) -> None:
        old_sentinel = self.shared / ".stamped-stale"
        old_sentinel.write_text("")
        old_time = time.time() - 31 * 24 * 3600
        os.utime(old_sentinel, (old_time, old_time))

        with patch.object(gc, "SHARED_DIR", self.shared):
            removed = _clean_sentinels(dry_run=True)

        self.assertEqual(removed, 1)
        self.assertTrue(old_sentinel.exists())


# ---------------------------------------------------------------------------
# _compact_origins
# ---------------------------------------------------------------------------


class CompactOriginsTests(unittest.TestCase):
    """Profile-origins.jsonl compaction: unknown profiles, dedup, locking."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.origins = self.tmp_path / "profile-origins.jsonl"
        self.options = self.tmp_path / "options.json"
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def _patch(self):
        """Return a combined context manager patching gc module globals."""
        return contextlib.ExitStack()

    def _run(self, dry_run: bool = False) -> tuple[int, int]:
        with patch.object(gc, "ORIGINS_FILE", self.origins), \
             patch.object(gc, "OPTIONS_FILE", self.options):
            return _compact_origins(dry_run)

    def test_removes_entries_for_unknown_profiles(self) -> None:
        lines = [
            json.dumps({"profile": "personal", "path": f"/x/{UUID_A}"}),
            json.dumps({"profile": "unknown_profile_xyz", "path": f"/x/{UUID_B}"}),
        ]
        self.origins.write_text("\n".join(lines) + "\n")

        kept, removed = self._run()

        self.assertEqual(removed, 1)
        self.assertEqual(kept, 1)
        remaining = self.origins.read_text().strip().splitlines()
        self.assertEqual(len(remaining), 1)
        self.assertIn("personal", remaining[0])

    def test_deduplicates_entries_with_same_uuid_keeps_latest(self) -> None:
        # Two entries with the same UUID -- the LAST one in the file is "latest"
        line_early = json.dumps({"profile": "work", "path": f"/old/{UUID_A}", "ts": "early"})
        line_late = json.dumps({"profile": "work", "path": f"/new/{UUID_A}", "ts": "late"})
        self.origins.write_text(line_early + "\n" + line_late + "\n")

        kept, removed = self._run()

        self.assertEqual(removed, 1)
        self.assertEqual(kept, 1)
        remaining = self.origins.read_text().strip().splitlines()
        entry = json.loads(remaining[0])
        self.assertEqual(entry["ts"], "late")

    def test_keeps_entries_for_known_profiles(self) -> None:
        lines = [
            json.dumps({"profile": "personal", "path": f"/a/{UUID_A}"}),
            json.dumps({"profile": "work", "path": f"/b/{UUID_B}"}),
        ]
        self.origins.write_text("\n".join(lines) + "\n")

        kept, removed = self._run()

        self.assertEqual(kept, 2)
        self.assertEqual(removed, 0)

    def test_handles_malformed_json_lines(self) -> None:
        good = json.dumps({"profile": "g<>F", "path": f"/x/{UUID_A}"})
        bad = "this is not json {{"
        self.origins.write_text(good + "\n" + bad + "\n")

        kept, removed = self._run()

        # Both kept: good line is valid, bad line is kept as-is
        self.assertEqual(kept, 2)
        self.assertEqual(removed, 0)
        remaining = self.origins.read_text().strip().splitlines()
        self.assertIn(bad, remaining)

    def test_lock_file_is_created(self) -> None:
        self.origins.write_text(
            json.dumps({"profile": "personal", "path": f"/x/{UUID_A}"}) + "\n"
        )

        self._run()

        lock_path = Path(str(self.origins) + ".lock")
        self.assertTrue(lock_path.exists())

    def test_dry_run_counts_but_does_not_rewrite(self) -> None:
        lines = [
            json.dumps({"profile": "personal", "path": f"/x/{UUID_A}"}),
            json.dumps({"profile": "nobody", "path": f"/x/{UUID_B}"}),
        ]
        original_content = "\n".join(lines) + "\n"
        self.origins.write_text(original_content)

        kept, removed = self._run(dry_run=True)

        self.assertEqual(removed, 1)
        self.assertEqual(kept, 1)
        # File should be unchanged
        self.assertEqual(self.origins.read_text(), original_content)


# ---------------------------------------------------------------------------
# _known_profiles
# ---------------------------------------------------------------------------


class KnownProfilesTests(unittest.TestCase):
    """Profile discovery from hardcoded defaults and options.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.options = self.tmp_path / "options.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_includes_hardcoded_profiles(self) -> None:
        with patch.object(gc, "OPTIONS_FILE", self.options):
            known = _known_profiles()

        self.assertIn("personal", known)
        self.assertIn("work", known)
        self.assertIn(",n"X", known)

    def test_includes_profiles_from_options_json(self) -> None:
        self.options.write_text(json.dumps({
            "profile": {"values": ["custom-alpha", "custom-beta"]}
        }))

        with patch.object(gc, "OPTIONS_FILE", self.options):
            known = _known_profiles()

        self.assertIn("custom-alpha", known)
        self.assertIn("custom-beta", known)
        # Hardcoded still present
        self.assertIn("personal", known)

    def test_missing_options_file_returns_hardcoded_only(self) -> None:
        # options file does not exist
        with patch.object(gc, "OPTIONS_FILE", self.tmp_path / "nonexistent.json"):
            known = _known_profiles()

        self.assertEqual(known, {"personal", "work", "26n+"})


# ---------------------------------------------------------------------------
# run_gc (integration)
# ---------------------------------------------------------------------------


class RunGcTests(unittest.TestCase):
    """Integration: run_gc executes all steps without error."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.shared = self.tmp_path / "shared"
        self.shared.mkdir()
        self.origins = self.tmp_path / "profile-origins.jsonl"
        self.options = self.tmp_path / "options.json"
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_runs_all_steps_without_error(self) -> None:
        # Set up minimal data for each step
        old_sentinel = self.shared / ".stamped-old"
        old_sentinel.write_text("")
        old_time = time.time() - 31 * 24 * 3600
        os.utime(old_sentinel, (old_time, old_time))

        self.origins.write_text(
            json.dumps({"profile": "personal", "path": f"/x/{UUID_A}"}) + "\n"
        )

        with patch.object(gc, "SHARED_DIR", self.shared), \
             patch.object(gc, "ORIGINS_FILE", self.origins), \
             patch.object(gc, "OPTIONS_FILE", self.options):
            # Should complete without raising
            run_gc(dry_run=False)

        # Sentinel should be removed
        self.assertFalse(old_sentinel.exists())
        # Origins should still have the valid entry
        remaining = self.origins.read_text().strip().splitlines()
        self.assertEqual(len(remaining), 1)

    def test_dry_run_makes_no_changes(self) -> None:
        old_sentinel = self.shared / ".stamped-old"
        old_sentinel.write_text("")
        old_time = time.time() - 31 * 24 * 3600
        os.utime(old_sentinel, (old_time, old_time))

        unknown_line = json.dumps({"profile": "nonexistent", "path": f"/x/{UUID_A}"})
        self.origins.write_text(unknown_line + "\n")
        original_content = self.origins.read_text()

        with patch.object(gc, "SHARED_DIR", self.shared), \
             patch.object(gc, "ORIGINS_FILE", self.origins), \
             patch.object(gc, "OPTIONS_FILE", self.options):
            run_gc(dry_run=True)

        # Nothing should have changed
        self.assertTrue(old_sentinel.exists())
        self.assertEqual(self.origins.read_text(), original_content)


if __name__ == "__main__":
    unittest.main()
