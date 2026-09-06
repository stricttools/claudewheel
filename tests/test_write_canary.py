"""Self-tests for the ``claude_dir`` write canary (tests/wheelhelpers.py).

The canary is a standing guard that proves the "cw never writes ~/.claude"
invariant at the fsutil write chokepoint. These tests prove the canary itself
works -- that it trips on a real write under ``claude_dir`` and stays out of the
way for writes elsewhere -- so it can never rot into a silent no-op that passes
every integration test regardless of what production code does.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from claudewheel.effects import (
    write_json_atomic,
    write_json_atomic_secret,
    write_text_atomic,
)
from tests.wheelhelpers import (
    ClaudeDirWriteViolation,
    claude_dir_write_canary,
    snapshot_tree,
)


class WriteCanarySelfTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.claude_dir = self.home / ".claude"
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        self.outside = self.home / ".claudewheel"
        self.outside.mkdir(parents=True, exist_ok=True)

    def test_direct_rename_under_claude_dir_trips(self) -> None:
        """A raw ``Path.rename`` whose target is under claude_dir trips."""
        src = self.home / "staging.tmp"
        src.write_text("x")
        dest = self.claude_dir / "settings.json"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                src.rename(dest)
        self.assertEqual(cm.exception.offending_path, dest)
        # The commit never happened: no file was published to claude_dir.
        self.assertFalse(dest.exists())

    def test_direct_replace_under_claude_dir_trips(self) -> None:
        """A raw ``os.replace`` whose destination is under claude_dir trips."""
        src = self.home / "staging.tmp"
        src.write_text("x")
        dest = self.claude_dir / "settings.json"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                os.replace(src, dest)
        self.assertEqual(cm.exception.offending_path, dest)
        self.assertFalse(dest.exists())

    def test_replace_restored_after_context(self) -> None:
        """os.replace is restored to real behavior after the context exits."""
        with claude_dir_write_canary(self.claude_dir):
            pass
        src = self.home / "after.tmp"
        src.write_text("y")
        dest = self.claude_dir / "after.txt"
        os.replace(src, dest)  # would trip if the patch leaked; it must not
        self.assertTrue(dest.exists())

    def test_fsutil_write_json_under_claude_dir_trips(self) -> None:
        """The real ``write_json_atomic`` writer trips at its commit seam."""
        target = self.claude_dir / "settings.json"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                write_json_atomic(target, {"hooks": {}})
        self.assertEqual(cm.exception.offending_path, target)
        self.assertFalse(target.exists())

    def test_fsutil_write_text_under_claude_dir_trips(self) -> None:
        target = self.claude_dir / "notes.txt"
        with self.assertRaises(ClaudeDirWriteViolation):
            with claude_dir_write_canary(self.claude_dir):
                write_text_atomic(target, "content")
        self.assertFalse(target.exists())

    def test_fsutil_secret_write_under_claude_dir_trips(self) -> None:
        target = self.claude_dir / ".credentials.json"
        with self.assertRaises(ClaudeDirWriteViolation):
            with claude_dir_write_canary(self.claude_dir):
                write_json_atomic_secret(target, {"token": "s3cret"})
        self.assertFalse(target.exists())

    def test_write_outside_claude_dir_passes_through(self) -> None:
        """Writes outside claude_dir delegate to the real writer untouched."""
        target = self.outside / "state.json"
        with claude_dir_write_canary(self.claude_dir):
            write_json_atomic(target, {"ok": True})
        # The real write happened with real behavior (content + trailing NL).
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), '{\n  "ok": true\n}\n')

    def test_not_swallowed_by_except_exception(self) -> None:
        """The violation escapes a broad ``except Exception`` (BaseException)."""
        src = self.home / "staging.tmp"
        src.write_text("x")
        dest = self.claude_dir / "settings.json"
        with self.assertRaises(ClaudeDirWriteViolation):
            with claude_dir_write_canary(self.claude_dir):
                try:
                    src.rename(dest)
                except Exception:  # noqa: BLE001 - proving it is NOT caught here
                    self.fail("canary was swallowed by except Exception")

    def test_rename_restored_after_context(self) -> None:
        """Path.rename is restored to real behavior after the context exits."""
        with claude_dir_write_canary(self.claude_dir):
            pass
        src = self.home / "after.tmp"
        src.write_text("y")
        dest = self.claude_dir / "after.txt"
        src.rename(dest)  # would trip if the patch leaked; it must not
        self.assertTrue(dest.exists())

    # -- Byte-level writer seam (Path.write_text / Path.write_bytes) ----------

    def test_write_text_under_claude_dir_trips(self) -> None:
        """A direct ``Path.write_text`` under claude_dir (no rename) trips.

        Mirrors ``hook_scripts.deploy_scripts``, which writes ``dest.write_text``
        straight to its final destination without a committing rename -- a
        writer class the rename seam is blind to.
        """
        dest = self.claude_dir / "hook.sh"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                dest.write_text("#!/bin/sh\n")
        self.assertEqual(cm.exception.offending_path, dest)
        self.assertFalse(dest.exists())

    def test_write_bytes_under_claude_dir_trips(self) -> None:
        """A direct ``Path.write_bytes`` under claude_dir (no rename) trips."""
        dest = self.claude_dir / "blob.bin"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                dest.write_bytes(b"\x00\x01\x02")
        self.assertEqual(cm.exception.offending_path, dest)
        self.assertFalse(dest.exists())

    def test_write_text_outside_claude_dir_passes_through(self) -> None:
        """A direct ``Path.write_text`` outside claude_dir delegates through."""
        dest = self.outside / "note.txt"
        with claude_dir_write_canary(self.claude_dir):
            dest.write_text("ok\n")
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(), "ok\n")

    def test_write_bytes_outside_claude_dir_passes_through(self) -> None:
        """A direct ``Path.write_bytes`` outside claude_dir delegates through."""
        dest = self.outside / "blob.bin"
        with claude_dir_write_canary(self.claude_dir):
            dest.write_bytes(b"\xff\xfe")
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"\xff\xfe")

    def test_fsutil_staging_write_text_passes_through_to_rename(self) -> None:
        """The ``.tmp`` staging write is not itself the trip point.

        ``write_text_atomic`` stages a unique ``*.tmp`` file and commits it with
        ``os.replace``. Neither the staging write nor the staging path may be
        the trip point, so the violation reports the real target -- otherwise
        the commit seam's ``offending_path`` contract would silently change.
        """
        target = self.claude_dir / "settings.json"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                write_text_atomic(target, "content")
        self.assertEqual(cm.exception.offending_path, target)

    # -- Stray-.tmp cleanup on exit ------------------------------------------

    def test_stray_tmp_after_trip_leaves_tree_identical(self) -> None:
        """After a tripped atomic write, claude_dir is byte-identical to entry.

        ``write_json_atomic`` stages a unique ``*.tmp`` file under claude_dir
        BEFORE the ``os.replace`` that trips.  The writer unlinks its own
        staging file when the commit raises, and the canary's exit cleanup
        sweeps anything a writer could still leave behind, so no new file
        (target OR staging) survives the context either way.
        """
        # Pre-existing content proves cleanup deletes ONLY files that appeared
        # during the context, never files present at entry.
        keeper = self.claude_dir / "pre-existing.json"
        keeper.write_text("{}\n")
        before = snapshot_tree(self.claude_dir)

        target = self.claude_dir / "settings.json"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                write_json_atomic(target, {"hooks": {}})

        # The staging file existed transiently and is gone: the writer cleaned
        # up after its own failed commit, so the canary had no stray to report.
        self.assertEqual(cm.exception.stray_tmp_files, [])
        self.assertFalse(target.exists())
        # The whole tree is exactly what it was before the context.
        self.assertEqual(snapshot_tree(self.claude_dir), before)
        self.assertTrue(keeper.exists())

    def test_clean_trip_reports_no_strays(self) -> None:
        """A direct-writer trip (no staging) reports an empty stray list."""
        dest = self.claude_dir / "hook.sh"
        with self.assertRaises(ClaudeDirWriteViolation) as cm:
            with claude_dir_write_canary(self.claude_dir):
                dest.write_text("#!/bin/sh\n")
        self.assertEqual(cm.exception.stray_tmp_files, [])
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
