"""Tests for the atomic-write helpers in claudewheel.effects."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claudewheel.effects import (
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

    def _assert_no_tmp_left(self, *expected: Path) -> None:
        """Assert the directory holds nothing but the expected target files.

        Stronger than a ``*.tmp`` filter on purpose: the writers now stage
        through ``tempfile.mkstemp`` names, so a filter keyed on any particular
        staging spelling could stop asserting anything the moment the naming
        changes again.  Anything that is not an expected target is a leftover.
        """
        keep = {p.name for p in (expected or (self.target,))}
        leftovers = sorted(
            p.name for p in self.tmp_path.iterdir() if p.name not in keep
        )
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# write_text_atomic / write_json_atomic (preserve policy)
# ---------------------------------------------------------------------------


class WriteTextAtomicTests(FsutilTestCase):
    """Tests for effects.write_text_atomic()."""

    def test_fresh_file_gets_umask_default(self) -> None:
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        write_text_atomic(self.target, "hello\n")
        self.assertEqual(self.target.read_text(), "hello\n")
        self.assertEqual(self._mode(self.target), 0o644)
        self._assert_no_tmp_left()

    def test_fresh_file_is_0644_under_a_tight_umask(self) -> None:
        # The staging file is created 0600 by mkstemp, so a fresh target's mode
        # is set explicitly rather than inherited from the umask.
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        write_text_atomic(self.target, "hello\n")
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
        with patch.object(Path, "stat", autospec=True, side_effect=FileNotFoundError):
            write_text_atomic(self.target, "new")
        self.assertEqual(self.target.read_text(), "new")
        self._assert_no_tmp_left()


class WriteJsonAtomicTests(FsutilTestCase):
    """Tests for effects.write_json_atomic()."""

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
        with patch.object(Path, "stat", autospec=True, side_effect=FileNotFoundError):
            write_json_atomic(self.target, {"a": 1})
        self.assertEqual(json.loads(self.target.read_text()), {"a": 1})
        self._assert_no_tmp_left()


# ---------------------------------------------------------------------------
# write_json_atomic_secret (secret policy)
# ---------------------------------------------------------------------------


class WriteJsonAtomicSecretTests(FsutilTestCase):
    """Tests for effects.write_json_atomic_secret()."""

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
        # The staging file must be 0600 from creation to commit: nothing in the
        # directory is ever observable wider than 0600 while the token bytes
        # are on disk. Spying on both ends of the staging file's life is
        # enough, because those are the only two moments its mode can change.
        creation_modes: list[int] = []
        commit_modes: list[int] = []
        real_mkstemp = tempfile.mkstemp
        real_replace = os.replace

        def spy_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            fd, name = real_mkstemp(*args, **kwargs)
            creation_modes.append(os.stat(name).st_mode & 0o777)
            return fd, name

        def spy_replace(src: Any, dst: Any, **kwargs: Any) -> None:
            commit_modes.append(os.stat(src).st_mode & 0o777)
            real_replace(src, dst, **kwargs)

        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        with (
            patch.object(tempfile, "mkstemp", new=spy_mkstemp),
            patch.object(os, "replace", new=spy_replace),
        ):
            write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(creation_modes, [0o600])
        self.assertEqual(commit_modes, [0o600])
        self.assertEqual(self._mode(self.target), 0o600)

    def test_staging_file_is_0600_even_over_a_loose_target(self) -> None:
        # An existing 0644 secrets file must not widen the staging file.
        self.target.write_text("{}")
        self.target.chmod(0o644)
        commit_modes: list[int] = []
        real_replace = os.replace

        def spy_replace(src: Any, dst: Any, **kwargs: Any) -> None:
            commit_modes.append(os.stat(src).st_mode & 0o777)
            real_replace(src, dst, **kwargs)

        with patch.object(os, "replace", new=spy_replace):
            write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(commit_modes, [0o600])
        self.assertEqual(self._mode(self.target), 0o600)

    def test_fresh_file_is_0600_under_a_loose_umask(self) -> None:
        old_umask = os.umask(0o000)
        self.addCleanup(os.umask, old_umask)
        write_json_atomic_secret(self.target, {"token": "s"})
        self.assertEqual(self._mode(self.target), 0o600)
        self._assert_no_tmp_left()


if __name__ == "__main__":
    unittest.main()
