"""Tests for claudewheel.gc — garbage collection for shared infrastructure."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel import gc
from claudewheel.gc import run_gc


# ---------------------------------------------------------------------------
# run_gc
# ---------------------------------------------------------------------------


class RunGcTests(unittest.TestCase):
    """run_gc: transitional sentinel cleanup + shared-store stats."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.shared = Path(self._tmp.name) / "shared"
        self.shared.mkdir()
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_removes_legacy_sentinels_dir(self) -> None:
        sentinels = self.shared / "sentinels"
        sentinels.mkdir()
        (sentinels / "some-file").write_text("")

        with patch.object(gc, "SHARED_DIR", self.shared):
            run_gc(dry_run=False)

        self.assertFalse(sentinels.exists())

    def test_dry_run_preserves_sentinels_dir(self) -> None:
        sentinels = self.shared / "sentinels"
        sentinels.mkdir()
        (sentinels / "some-file").write_text("")

        with patch.object(gc, "SHARED_DIR", self.shared):
            run_gc(dry_run=True)

        self.assertTrue(sentinels.exists())
        self.assertTrue((sentinels / "some-file").exists())

    def test_idempotent_when_no_sentinels_dir(self) -> None:
        # No sentinels directory at all -- should not raise.
        with patch.object(gc, "SHARED_DIR", self.shared):
            run_gc(dry_run=False)

    def test_reports_shared_stats(self) -> None:
        # Create a subdirectory with a file so _report_shared_stats has output.
        subdir = self.shared / "sessions"
        subdir.mkdir()
        (subdir / "data.json").write_text("{}")

        buf = io.StringIO()
        with patch.object(gc, "SHARED_DIR", self.shared), \
             contextlib.redirect_stdout(buf):
            run_gc(dry_run=False)

        output = buf.getvalue()
        self.assertIn("sessions", output)
        self.assertIn("TOTAL", output)
        self.assertIn("done", output)


if __name__ == "__main__":
    unittest.main()
