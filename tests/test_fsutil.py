"""Tests for the atomic-write helpers in claudewheel.fsutil."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.fsutil import (
    write_json_atomic,
    write_json_atomic_secret,
    write_text_atomic,
)


class FsutilTestCase(unittest.TestCase):
    """Base class providing a temp directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.target = self.tmp_path / "target.json"

    def _mode(self, path: Path) -> int:
        return path.stat().st_mode & 0o777

    def _assert_no_tmp_left(self) -> None:
        leftovers = [p for p in self.tmp_path.iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# write_text_atomic / write_json_atomic (preserve policy)
# ---------------------------------------------------------------------------


class WriteTextAtomicTests(FsutilTestCase):
    """Tests for fsutil.write_text_atomic()."""

    def test_fresh_file_gets_umask_default(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        write_text_atomic(self.target, "hello\n")
        self.assertEqual(self.target.read_text(), "hello\n")
        self.assertEqual(self._mode(self.target), 0o644)
        self._assert_no_tmp_left()

    def test_preserves_existing_0600(self) -> None:
        self.target.write_text("old")
        self.target.chmod(0o600)
        write_text_atomic(self.target, "new")
        self.assertEqual(self.target.read_text(), "new")
        self.assertEqual(self._mode(self.target), 0o600)
        self._assert_no_tmp_left()

    def test_preserves_existing_0640(self) -> None:
        self.target.write_text("old")
        self.target.chmod(0o640)
        write_text_atomic(self.target, "new")
        self.assertEqual(self.target.read_text(), "new")
        self.assertEqual(self._mode(self.target), 0o640)
        self._assert_no_tmp_left()

    def test_stat_race_falls_back_to_fresh_file(self) -> None:
        # Target exists when the write starts but vanishes before the
        # mode-preserving stat: the helper must fall back to fresh-file
        # behavior instead of crashing.
        self.target.write_text("old")
        self.target.chmod(0o640)
        with patch.object(Path, "stat", side_effect=FileNotFoundError):
            write_text_atomic(self.target, "new")
        self.assertEqual(self.target.read_text(), "new")
        self._assert_no_tmp_left()


class WriteJsonAtomicTests(FsutilTestCase):
    """Tests for fsutil.write_json_atomic()."""

    def test_writes_indented_json_with_trailing_newline(self) -> None:
        write_json_atomic(self.target, {"a": 1})
        self.assertEqual(self.target.read_text(), '{\n  "a": 1\n}\n')
        self._assert_no_tmp_left()

    def test_preserves_existing_0600(self) -> None:
        self.target.write_text("{}")
        self.target.chmod(0o600)
        write_json_atomic(self.target, {"a": 1})
        self.assertEqual(json.loads(self.target.read_text()), {"a": 1})
        self.assertEqual(self._mode(self.target), 0o600)

    def test_preserves_existing_0640(self) -> None:
        self.target.write_text("{}")
        self.target.chmod(0o640)
        write_json_atomic(self.target, {"a": 1})
        self.assertEqual(json.loads(self.target.read_text()), {"a": 1})
        self.assertEqual(self._mode(self.target), 0o640)

    def test_fresh_file_gets_umask_default(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        write_json_atomic(self.target, [1, 2])
        self.assertEqual(json.loads(self.target.read_text()), [1, 2])
        self.assertEqual(self._mode(self.target), 0o644)

    def test_stat_race_falls_back_to_fresh_file(self) -> None:
        self.target.write_text("{}")
        with patch.object(Path, "stat", side_effect=FileNotFoundError):
            write_json_atomic(self.target, {"a": 1})
        self.assertEqual(json.loads(self.target.read_text()), {"a": 1})
        self._assert_no_tmp_left()


# ---------------------------------------------------------------------------
# write_json_atomic_secret (secret policy)
# ---------------------------------------------------------------------------


class WriteJsonAtomicSecretTests(FsutilTestCase):
    """Tests for fsutil.write_json_atomic_secret()."""

    def test_fresh_file_is_0600(self) -> None:
        write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(self._mode(self.target), 0o600)
        self.assertEqual(self.target.read_text(), '{\n  "token": "s"\n}\n')
        self._assert_no_tmp_left()

    def test_existing_0600_stays_0600(self) -> None:
        self.target.write_text("{}")
        self.target.chmod(0o600)
        write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(self._mode(self.target), 0o600)

    def test_existing_loose_perms_forced_to_0600(self) -> None:
        # A previously world-readable secrets file must be tightened, not
        # preserved -- the secret policy always enforces 0600.
        self.target.write_text("{}")
        self.target.chmod(0o644)
        write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(self._mode(self.target), 0o600)
        self._assert_no_tmp_left()

    def test_tmp_file_never_umask_readable(self) -> None:
        # The tmp file must be created 0600 from the start: intercept the
        # rename to inspect the tmp file's mode mid-write.
        seen_modes: list[int] = []
        real_rename = Path.rename

        def spy_rename(src: Path, dst: Path):
            seen_modes.append(src.stat().st_mode & 0o777)
            return real_rename(src, dst)

        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        with patch.object(Path, "rename", spy_rename):
            write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(seen_modes, [0o600])


if __name__ == "__main__":
    unittest.main()
