"""Tests for the scratchpad scanner (Phase 3.1).

Covers per-directory size and age computation, symlink safety (never followed
for size or mtime), and the staleness rule (fresh activity anywhere in a tree
makes the whole tree non-stale).
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from claudewheel.scratchpad import (
    SCRATCHPAD_STALE_DAYS,
    ScratchpadDir,
    scan_scratchpad_dirs,
    stale_scratchpad_dirs,
    tmp_claude_dir,
)

_DAY = 86400.0


def _expected_block_bytes(paths: list[Path]) -> int:
    """Sum st_blocks*512 over the given regular files (mirrors the scanner)."""
    total = 0
    for p in paths:
        st = os.lstat(p)
        if stat.S_ISREG(st.st_mode):
            total += st.st_blocks * 512
    return total


class ScratchpadScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, rel: str, content: bytes, mtime: float | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def _get(self, dirs: list[ScratchpadDir], name: str) -> ScratchpadDir:
        return next(d for d in dirs if d.name == name)

    # -- size -------------------------------------------------------------

    def test_per_dir_sizes_are_correct(self) -> None:
        f1 = self._write("projA/session1/a.bin", b"x" * 4096)
        f2 = self._write("projA/session1/b.bin", b"y" * 8192)
        f3 = self._write("projB/session1/c.bin", b"z" * 2048)

        dirs = scan_scratchpad_dirs(self.root)
        self.assertEqual({d.name for d in dirs}, {"projA", "projB"})
        self.assertEqual(
            self._get(dirs, "projA").size_bytes, _expected_block_bytes([f1, f2])
        )
        self.assertEqual(
            self._get(dirs, "projB").size_bytes, _expected_block_bytes([f3])
        )

    def test_missing_root_yields_empty(self) -> None:
        self.assertEqual(scan_scratchpad_dirs(self.root / "nope"), [])

    def test_files_at_top_level_are_ignored(self) -> None:
        # Only directories become ScratchpadDirs; loose files at the root are not.
        self._write("loose.txt", b"hi")
        self._write("projA/session1/a.bin", b"x" * 1024)
        dirs = scan_scratchpad_dirs(self.root)
        self.assertEqual({d.name for d in dirs}, {"projA"})

    # -- symlinks ---------------------------------------------------------

    def test_symlink_to_file_not_counted_in_size(self) -> None:
        real = self._write("projA/session1/real.bin", b"x" * 4096)
        # A large external target the symlink points at -- must NOT be counted.
        external = self.root / "external_big.bin"
        external.write_bytes(b"q" * (256 * 1024))
        link = self.root / "projA" / "session1" / "link.bin"
        link.symlink_to(external)

        dirs = scan_scratchpad_dirs(self.root)
        # Size equals only the real file's blocks (symlink + target excluded).
        self.assertEqual(
            self._get(dirs, "projA").size_bytes, _expected_block_bytes([real])
        )

    def test_symlinked_subdir_not_descended(self) -> None:
        self._write("projA/session1/real.bin", b"x" * 2048)
        external_dir = self.root / "external_dir"
        (external_dir).mkdir()
        big = external_dir / "big.bin"
        big.write_bytes(b"q" * (128 * 1024))
        (self.root / "projA" / "linkdir").symlink_to(external_dir)

        dirs = scan_scratchpad_dirs(self.root)
        real = self.root / "projA" / "session1" / "real.bin"
        self.assertEqual(
            self._get(dirs, "projA").size_bytes, _expected_block_bytes([real])
        )

    def test_top_level_symlink_dir_skipped(self) -> None:
        self._write("projA/session1/a.bin", b"x" * 1024)
        # A sibling temp dir OUTSIDE the scan root, so the only thing under root
        # pointing at it is the symlink (which must be skipped, not followed).
        with tempfile.TemporaryDirectory() as ext:
            (self.root / "projLink").symlink_to(ext)
            dirs = scan_scratchpad_dirs(self.root)
            self.assertEqual({d.name for d in dirs}, {"projA"})

    # -- age / staleness --------------------------------------------------

    def test_age_days_relative_to_now(self) -> None:
        now = 1_000_000.0
        d = ScratchpadDir(
            path=self.root, name="x", size_bytes=0, newest_mtime=now - 5 * _DAY
        )
        self.assertAlmostEqual(d.age_days(now), 5.0)

    def test_newest_mtime_is_max_over_tree(self) -> None:
        old = 1_000_000.0
        fresh = old + 100 * _DAY
        self._write("projA/session1/old.bin", b"x" * 512, mtime=old)
        self._write("projA/session2/fresh.bin", b"y" * 512, mtime=fresh)
        # Age is measured from the freshest entry anywhere in the tree.
        d = self._get(scan_scratchpad_dirs(self.root), "projA")
        self.assertGreaterEqual(d.newest_mtime, fresh)

    def test_fresh_activity_anywhere_makes_dir_non_stale(self) -> None:
        old = 1_000_000.0
        now = old + (SCRATCHPAD_STALE_DAYS + 100) * _DAY
        # Almost everything is ancient...
        self._write("projA/session1/ancient1.bin", b"x" * 512, mtime=old)
        self._write("projA/session1/ancient2.bin", b"x" * 512, mtime=old)
        # ...but one deeply-nested file was just touched.
        self._write(
            "projA/session9/deep/fresh.bin", b"y" * 512, mtime=now - 1 * _DAY
        )
        d = self._get(scan_scratchpad_dirs(self.root), "projA")
        self.assertFalse(d.is_stale(now))

    def test_all_old_dir_is_stale(self) -> None:
        old = 1_000_000.0
        now = old + (SCRATCHPAD_STALE_DAYS + 5) * _DAY
        self._write("projA/session1/a.bin", b"x" * 512, mtime=old)
        # The directory mtimes also need to be old, else they dominate newest.
        for sub in (self.root / "projA" / "session1", self.root / "projA"):
            os.utime(sub, (old, old))
        d = self._get(scan_scratchpad_dirs(self.root), "projA")
        self.assertTrue(d.is_stale(now))

    def test_stale_helper_filters(self) -> None:
        old = 1_000_000.0
        now = old + (SCRATCHPAD_STALE_DAYS + 5) * _DAY
        self._write("projStale/session1/a.bin", b"x" * 512, mtime=old)
        for sub in (
            self.root / "projStale" / "session1",
            self.root / "projStale",
        ):
            os.utime(sub, (old, old))
        self._write("projFresh/session1/b.bin", b"y" * 512, mtime=now - _DAY)
        stale = stale_scratchpad_dirs(self.root, now)
        self.assertEqual([d.name for d in stale], ["projStale"])


class TmpClaudeDirTests(unittest.TestCase):
    def test_points_at_uid_dir(self) -> None:
        self.assertEqual(str(tmp_claude_dir()), f"/tmp/claude-{os.getuid()}")


if __name__ == "__main__":
    unittest.main()
