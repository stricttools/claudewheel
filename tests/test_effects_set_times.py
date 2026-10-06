"""``effects.set_times``: put a file's access and modification times back.

A command that rewrites a file it means to leave looking untouched (the
session move keeps a transcript's times) sets them through this primitive, so
a ``--dry-run`` records the change instead of making it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claudewheel import effects


class _RecordingHandle:
    """Records every call a preview makes on strictcli's effects handle."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def run(self, argv: list[str], **_kwargs: Any) -> None:
        self.calls.append(("run", tuple(argv)))


class _PreviewCtx:
    def __init__(self, handle: _RecordingHandle) -> None:
        self.dry_run = True
        self.effects = handle


# Distinct, sub-second times, so a primitive that dropped either value or its
# nanoseconds is caught.
ATIME_NS = 1_700_000_000_123_456_789
MTIME_NS = 1_600_000_000_987_654_321


class SetTimesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "transcript.jsonl"
        self.path.write_text("{}\n")

    def test_live_mode_sets_both_times_to_the_nanosecond(self) -> None:
        effects.set_times(self.path, atime_ns=ATIME_NS, mtime_ns=MTIME_NS)
        st = os.stat(self.path)
        self.assertEqual(st.st_atime_ns, ATIME_NS)
        self.assertEqual(st.st_mtime_ns, MTIME_NS)

    def test_preview_records_the_touch_commands_and_changes_nothing(self) -> None:
        before = os.stat(self.path)
        handle = _RecordingHandle()
        with effects.bound(_PreviewCtx(handle)):
            effects.set_times(self.path, atime_ns=ATIME_NS, mtime_ns=MTIME_NS)
        after = os.stat(self.path)
        self.assertEqual(
            (after.st_atime_ns, after.st_mtime_ns),
            (before.st_atime_ns, before.st_mtime_ns),
        )
        self.assertEqual(
            handle.calls,
            [
                (
                    "run",
                    (
                        "touch",
                        "--no-create",
                        "-a",
                        "-d",
                        "@1700000000.123456789",
                        str(self.path),
                    ),
                ),
                (
                    "run",
                    (
                        "touch",
                        "--no-create",
                        "-m",
                        "-d",
                        "@1600000000.987654321",
                        str(self.path),
                    ),
                ),
            ],
        )

    def test_the_recorded_commands_set_the_same_times(self) -> None:
        """What a preview records is a command that does what live mode does."""
        import subprocess

        handle = _RecordingHandle()
        with effects.bound(_PreviewCtx(handle)):
            effects.set_times(self.path, atime_ns=ATIME_NS, mtime_ns=MTIME_NS)
        for _name, argv in handle.calls:
            subprocess.run(list(argv), check=True)
        st = os.stat(self.path)
        self.assertEqual(st.st_atime_ns, ATIME_NS)
        self.assertEqual(st.st_mtime_ns, MTIME_NS)

    def test_a_missing_file_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            effects.set_times(
                self.path.with_name("absent"), atime_ns=ATIME_NS, mtime_ns=MTIME_NS
            )


if __name__ == "__main__":
    unittest.main()
