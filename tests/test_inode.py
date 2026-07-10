"""Tests for inode tracking and rename detection."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class InodeTestCase(unittest.TestCase):
    """Base class providing a temp directory and patched INODES_FILE."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._inodes_file = self.tmp_path / "inodes.json"
        # state.record_inode derives the inodes file from SharedStore's shared_dir
        # (inodes.json belongs to the shared store), so patch state.SHARED_DIR to
        # the temp dir -> store.inodes_file == self._inodes_file. health.py still
        # reads its own INODES_FILE constant this wave, so patch that directly.
        self._patches = [
            patch("claudewheel.state.SHARED_DIR", self.tmp_path),
            patch("claudewheel.health.INODES_FILE", self._inodes_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# record_inode
# ---------------------------------------------------------------------------


class RecordInodeTests(InodeTestCase):
    """Tests for state.record_inode()."""

    def test_new_dir(self) -> None:
        """Recording a directory creates an entry in inodes.json."""
        from claudewheel.state import record_inode
        d = self.tmp_path / "project_a"
        d.mkdir()
        record_inode(str(d))
        data = json.loads(self._inodes_file.read_text())
        abspath = str(d.resolve())
        self.assertIn(abspath, data)
        self.assertEqual(data[abspath], os.stat(str(d)).st_ino)

    def test_same_dir_twice(self) -> None:
        """Recording the same directory twice does not change the file."""
        from claudewheel.state import record_inode
        d = self.tmp_path / "project_b"
        d.mkdir()
        record_inode(str(d))
        mtime1 = self._inodes_file.stat().st_mtime_ns
        record_inode(str(d))
        # File should not have been rewritten
        mtime2 = self._inodes_file.stat().st_mtime_ns
        self.assertEqual(mtime1, mtime2)

    def test_renamed_dir(self) -> None:
        """After renaming, recording the new path adds a second entry with the same inode."""
        from claudewheel.state import record_inode
        d = self.tmp_path / "old_name"
        d.mkdir()
        record_inode(str(d))
        inode = os.stat(str(d)).st_ino
        # Rename the directory
        new = self.tmp_path / "new_name"
        d.rename(new)
        record_inode(str(new))
        data = json.loads(self._inodes_file.read_text())
        _ = str(d.resolve()) if d.exists() else os.path.abspath(str(d))  # verify no crash
        new_abs = str(new.resolve())
        # Both entries should exist with the same inode
        self.assertEqual(data[new_abs], inode)
        # The old entry should still be in the file (old_name path)
        # Since old_name was recorded before rename, it should still be there
        old_key = os.path.abspath(str(self.tmp_path / "old_name"))
        self.assertIn(old_key, data)
        self.assertEqual(data[old_key], inode)

    def test_nonexistent_dir(self) -> None:
        """Recording a nonexistent directory is a no-op."""
        from claudewheel.state import record_inode
        record_inode(str(self.tmp_path / "does_not_exist"))
        self.assertFalse(self._inodes_file.exists())


# ---------------------------------------------------------------------------
# check_inode_renames
# ---------------------------------------------------------------------------


class CheckInodeRenamesTests(InodeTestCase):
    """Tests for health.check_inode_renames()."""

    def test_no_data(self) -> None:
        """Returns OK when inodes.json does not exist."""
        from claudewheel.health import check_inode_renames
        result = check_inode_renames()
        self.assertTrue(result.ok)
        self.assertIn("no inode data", result.detail)

    def test_no_renames(self) -> None:
        """Returns OK when all recorded paths still exist with correct inodes."""
        from claudewheel.health import check_inode_renames
        d = self.tmp_path / "still_here"
        d.mkdir()
        inode = os.stat(str(d)).st_ino
        self._inodes_file.write_text(json.dumps({str(d.resolve()): inode}))
        result = check_inode_renames()
        self.assertTrue(result.ok)
        self.assertIn("no renames detected", result.detail)

    def test_rename_detected(self) -> None:
        """Returns WARN when one path is gone and another has the same inode."""
        from claudewheel.health import check_inode_renames
        # Create a directory and get its inode
        d = self.tmp_path / "original"
        d.mkdir()
        inode = os.stat(str(d)).st_ino
        # Rename it
        new = self.tmp_path / "renamed"
        d.rename(new)
        old_abs = os.path.abspath(str(self.tmp_path / "original"))
        new_abs = str(new.resolve())
        # Write both entries (simulating record_inode from old + new paths)
        self._inodes_file.write_text(json.dumps({old_abs: inode, new_abs: inode}))
        result = check_inode_renames()
        self.assertFalse(result.ok)
        self.assertIn("original", result.detail)
        self.assertIn("renamed", result.detail)
        self.assertIn("claudewheel mv", result.detail)

    def test_stale_entry_cleaned(self) -> None:
        """Stale entries (deleted dirs, no matching inode) are removed from inodes.json."""
        from claudewheel.health import check_inode_renames
        gone_path = str(self.tmp_path / "gone_forever")
        # Write an entry for a path that doesn't exist with a unique inode
        self._inodes_file.write_text(json.dumps({gone_path: 999999999}))
        result = check_inode_renames()
        self.assertTrue(result.ok)
        self.assertIn("cleaned", result.detail)
        # Verify the entry was removed
        data = json.loads(self._inodes_file.read_text())
        self.assertNotIn(gone_path, data)

    def test_multiple_renames(self) -> None:
        """Multiple rename detections are all reported."""
        from claudewheel.health import check_inode_renames
        # Create two directories, rename both
        d1 = self.tmp_path / "proj1"
        d1.mkdir()
        inode1 = os.stat(str(d1)).st_ino
        new1 = self.tmp_path / "proj1_new"
        d1.rename(new1)

        d2 = self.tmp_path / "proj2"
        d2.mkdir()
        inode2 = os.stat(str(d2)).st_ino
        new2 = self.tmp_path / "proj2_new"
        d2.rename(new2)

        old1_abs = os.path.abspath(str(self.tmp_path / "proj1"))
        new1_abs = str(new1.resolve())
        old2_abs = os.path.abspath(str(self.tmp_path / "proj2"))
        new2_abs = str(new2.resolve())
        self._inodes_file.write_text(json.dumps({
            old1_abs: inode1, new1_abs: inode1,
            old2_abs: inode2, new2_abs: inode2,
        }))
        result = check_inode_renames()
        self.assertFalse(result.ok)
        # Both renames should be mentioned
        self.assertIn("proj1", result.detail)
        self.assertIn("proj2", result.detail)


if __name__ == "__main__":
    unittest.main()
