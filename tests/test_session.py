"""Tests for claudewheel.session — session lookup and cwd extraction."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.session import (
    MAX_CWD_SCAN_LINES,
    find_orphaned_project_dirs,
    find_session,
    get_session_cwd,
)


# ---------------------------------------------------------------------------
# get_session_cwd
# ---------------------------------------------------------------------------


class GetSessionCwdTests(unittest.TestCase):
    """Extract the cwd field from the first few lines of a session JSONL."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_jsonl(self, name: str, lines: list[dict[str, object] | str]) -> Path:
        """Write *lines* to a JSONL file.  Dicts are JSON-encoded; strings
        are written verbatim (useful for corrupt-line tests)."""
        p = self.tmp_path / name
        with p.open("w") as fh:
            for line in lines:
                if isinstance(line, dict):
                    fh.write(json.dumps(line) + "\n")
                else:
                    fh.write(line + "\n")
        return p

    # -- CLI entrypoint layout (permission-mode, file-history-snapshot, then user with cwd)
    def test_get_session_cwd_cli_entrypoint(self) -> None:
        """cwd on line 3 after permission-mode and file-history-snapshot."""
        p = self._write_jsonl(
            "cli.jsonl",
            [
                {"type": "permission-mode", "mode": "default"},
                {"type": "file-history-snapshot", "files": []},
                {"type": "user", "cwd": "/home/m/Projects/foo", "message": "hello"},
            ],
        )
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/foo")

    # -- SDK entrypoint layout (two queue-operation lines, then assistant with cwd)
    def test_get_session_cwd_sdk_entrypoint(self) -> None:
        """cwd on line 3 after two queue-operation lines."""
        p = self._write_jsonl(
            "sdk.jsonl",
            [
                {"type": "queue-operation", "op": "enqueue"},
                {"type": "queue-operation", "op": "dequeue"},
                {"type": "assistant", "cwd": "/home/m/Projects/bar", "text": "hi"},
            ],
        )
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/bar")

    # -- Subagent layout (cwd on first line)
    def test_get_session_cwd_line_1(self) -> None:
        """cwd on the very first line (subagent format)."""
        p = self._write_jsonl(
            "subagent.jsonl",
            [
                {"type": "system", "cwd": "/home/m/Projects/baz"},
            ],
        )
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/baz")

    def test_get_session_cwd_empty_file(self) -> None:
        """An empty file returns None."""
        p = self.tmp_path / "empty.jsonl"
        p.write_text("")
        self.assertIsNone(get_session_cwd(p))

    def test_get_session_cwd_no_cwd_field(self) -> None:
        """Lines exist but none carry a cwd field."""
        p = self._write_jsonl(
            "no_cwd.jsonl",
            [
                {"type": "permission-mode", "mode": "default"},
                {"type": "file-history-snapshot", "files": []},
                {"type": "metadata", "version": "1.0"},
            ],
        )
        self.assertIsNone(get_session_cwd(p))

    def test_get_session_cwd_invalid_json_lines(self) -> None:
        """Corrupt lines are skipped; cwd is still found on a valid line."""
        p = self._write_jsonl(
            "corrupt.jsonl",
            [
                "NOT VALID JSON {{{",
                "also broken",
                {"type": "user", "cwd": "/home/m/Projects/ok", "message": "works"},
            ],
        )
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/ok")

    def test_get_session_cwd_file_not_found(self) -> None:
        """A missing file returns None without raising."""
        self.assertIsNone(get_session_cwd(self.tmp_path / "nonexistent.jsonl"))

    def test_get_session_cwd_beyond_scan_limit(self) -> None:
        """cwd exists but only on line 15 — beyond the default scan limit."""
        lines: list[dict[str, object] | str] = [
            {"type": "metadata", "line": i} for i in range(14)
        ]
        lines.append({"type": "user", "cwd": "/too/late"})
        p = self._write_jsonl("late_cwd.jsonl", lines)
        self.assertIsNone(get_session_cwd(p))
        # Confirm MAX_CWD_SCAN_LINES is the expected value.
        self.assertEqual(MAX_CWD_SCAN_LINES, 10)


# ---------------------------------------------------------------------------
# find_session
# ---------------------------------------------------------------------------


class FindSessionTests(unittest.TestCase):
    """Locate a session by UUID in the shared projects store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.projects_dir = Path(self._tmp.name) / "projects"
        self.projects_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create_session(
        self,
        encoded_cwd: str,
        session_id: str,
        lines: list[dict[str, object]] | None = None,
    ) -> Path:
        """Create a session JSONL file under the given encoded_cwd directory."""
        cwd_dir = self.projects_dir / encoded_cwd
        cwd_dir.mkdir(parents=True, exist_ok=True)
        p = cwd_dir / f"{session_id}.jsonl"
        if lines is None:
            lines = [{"type": "user", "cwd": "/home/m/Projects/demo", "message": "hi"}]
        with p.open("w") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        return p

    def test_find_session_found(self) -> None:
        """A session that exists is returned with correct metadata."""
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        encoded = "-home-m-Projects-demo"
        self._create_session(encoded, sid)

        result = find_session(sid, shared_projects_dir=self.projects_dir)

        self.assertIsNotNone(result)
        assert result is not None  # for type narrowing
        self.assertEqual(result.session_id, sid)
        self.assertEqual(result.encoded_cwd, encoded)
        self.assertEqual(
            result.jsonl_path, self.projects_dir / encoded / f"{sid}.jsonl"
        )
        self.assertEqual(result.cwd, "/home/m/Projects/demo")

    def test_find_session_not_found(self) -> None:
        """A non-existent session returns None."""
        result = find_session(
            "00000000-0000-0000-0000-000000000000",
            shared_projects_dir=self.projects_dir,
        )
        self.assertIsNone(result)

    def test_find_session_custom_dir(self) -> None:
        """The explicit shared_projects_dir parameter is used instead of the default."""
        sid = "11111111-2222-3333-4444-555555555555"
        encoded = "-home-m-Projects-custom"
        cwd_value = "/home/m/Projects/custom"
        self._create_session(
            encoded,
            sid,
            lines=[{"type": "user", "cwd": cwd_value, "message": "test"}],
        )

        result = find_session(sid, shared_projects_dir=self.projects_dir)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.cwd, cwd_value)
        self.assertEqual(result.encoded_cwd, encoded)


# ---------------------------------------------------------------------------
# find_orphaned_project_dirs
# ---------------------------------------------------------------------------


class FindOrphanedProjectDirsTests(unittest.TestCase):
    """Locate project dirs whose original cwd no longer exists on disk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.projects_dir = Path(self._tmp.name) / "projects"
        self.projects_dir.mkdir()

    def _create_project(
        self,
        encoded_cwd: str,
        cwd: str,
        session_count: int = 1,
    ) -> Path:
        """Create a fake project dir with session files."""
        project_dir = self.projects_dir / encoded_cwd
        project_dir.mkdir(parents=True, exist_ok=True)
        for i in range(session_count):
            p = project_dir / f"session-{i}.jsonl"
            p.write_text(
                json.dumps({"type": "user", "cwd": cwd, "message": "hi"}) + "\n"
            )
        return project_dir

    def test_find_orphaned_one_match(self) -> None:
        """One orphaned dir is returned."""
        self._create_project(
            "-home-user-old-project",
            "/home/user/old-project",
            session_count=3,
        )
        _real_isdir = os.path.isdir

        def _fake_isdir(path: str) -> bool:
            # The cwd extracted from JSONL should appear non-existent;
            # all other paths (temp dirs) use the real check.
            if path == "/home/user/old-project":
                return False
            return _real_isdir(path)

        with mock.patch("os.path.isdir", autospec=True, side_effect=_fake_isdir):
            results = find_orphaned_project_dirs(
                shared_projects_dir=self.projects_dir,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].cwd, "/home/user/old-project")
        self.assertEqual(results[0].session_count, 3)
        self.assertGreater(results[0].total_size_bytes, 0)

    def test_find_orphaned_no_match(self) -> None:
        """No orphaned dirs when all cwds still exist on disk."""
        self._create_project(
            "-home-other-project",
            "/home/other/project",
            session_count=1,
        )
        with mock.patch("os.path.isdir", autospec=True, return_value=True):
            results = find_orphaned_project_dirs(
                shared_projects_dir=self.projects_dir,
            )
        self.assertEqual(len(results), 0)

    def test_find_orphaned_dir_still_exists(self) -> None:
        """Dirs whose cwd still exists on disk are excluded."""
        self._create_project(
            "-home-user-still-here",
            "/home/user/still-here",
            session_count=2,
        )
        with mock.patch("os.path.isdir", autospec=True, return_value=True):
            results = find_orphaned_project_dirs(
                shared_projects_dir=self.projects_dir,
            )
        self.assertEqual(len(results), 0)

    def test_find_orphaned_cross_parent(self) -> None:
        """Orphan with a different parent dir than the 'current' dir is still found."""
        # Simulate: current dir is /home/user/Work/foo but orphan cwd
        # is /home/user/Projects/foo (different parent). The full-scan
        # approach should still find it.
        self._create_project(
            "-home-user-Projects-foo",
            "/home/user/Projects/foo",
            session_count=2,
        )
        _real_isdir = os.path.isdir

        def _fake_isdir(path: str) -> bool:
            if path == "/home/user/Projects/foo":
                return False
            return _real_isdir(path)

        with mock.patch("os.path.isdir", autospec=True, side_effect=_fake_isdir):
            results = find_orphaned_project_dirs(
                shared_projects_dir=self.projects_dir,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].cwd, "/home/user/Projects/foo")
        self.assertEqual(results[0].session_count, 2)


if __name__ == "__main__":
    unittest.main()
