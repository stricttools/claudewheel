"""The effects layer refuses a unittest.mock object handed to it as a path.

An unspecced ``MagicMock`` answers every attribute access with another mock,
and ``__fspath__`` on one of those returns a repr string -- so ``os.fspath``
succeeds and a filesystem effect quietly creates a real file or directory named
after the mock.  One such attribute access (``mock.scripts_dir`` forwarded into
``effects.mkdir``) materialized a directory tree in the repository root.  A mock
is not a path, so every path operand is refused at the boundary.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel import effects


class EffectsMockPathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _mock_path(self) -> object:
        """A mock attribute of the shape that materialized a real tree."""
        return mock.MagicMock(name="config")().scripts_dir

    def test_mkdir_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError) as cm:
            effects.mkdir(self._mock_path(), parents=True, exist_ok=True)
        self.assertIn("unittest.mock", str(cm.exception))
        # Nothing was created anywhere: the guard runs before the mkdir.
        self.assertEqual(sorted(p.name for p in self.tmp_path.iterdir()), [])

    def test_write_text_atomic_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.write_text_atomic(self._mock_path(), "content")

    def test_write_json_atomic_secret_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.write_json_atomic_secret(self._mock_path(), {"token": "s"})

    def test_write_text_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.write_text(self._mock_path(), "content")

    def test_remove_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.remove(self._mock_path(), missing_ok=True)

    def test_rename_refuses_a_mock_destination(self) -> None:
        src = self.tmp_path / "src.txt"
        src.write_text("x")
        with self.assertRaises(TypeError):
            effects.rename(src, self._mock_path())
        self.assertTrue(src.exists())

    def test_specced_path_mock_is_refused_too(self) -> None:
        # A spec'd mock still is not a path: passing the mock itself (rather
        # than a real path it returns) must fail loudly, not write somewhere.
        specced = mock.create_autospec(Path, instance=True)
        with self.assertRaises(TypeError):
            effects.mkdir(specced)

    def test_real_paths_still_work(self) -> None:
        target_dir = self.tmp_path / "real"
        effects.mkdir(target_dir)
        effects.write_text_atomic(target_dir / "a.txt", "content")
        effects.write_json_atomic_secret(target_dir / "b.json", {"token": "s"})
        self.assertEqual((target_dir / "a.txt").read_text(), "content")
        self.assertEqual((target_dir / "b.json").read_text(), '{\n  "token": "s"\n}\n')
        # A plain string operand is a path too, and must keep working.
        effects.write_text(str(target_dir / "c.txt"), "plain\n")
        self.assertEqual((target_dir / "c.txt").read_text(), "plain\n")


if __name__ == "__main__":
    unittest.main()
