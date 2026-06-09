"""Tests for claudewheel.session — session lookup and cwd extraction."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claudewheel.session import (
    MAX_CWD_SCAN_LINES,
    SessionInfo,
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

    def _write_jsonl(self, name: str, lines: list[dict | str]) -> Path:
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
        p = self._write_jsonl("cli.jsonl", [
            {"type": "permission-mode", "mode": "default"},
            {"type": "file-history-snapshot", "files": []},
            {"type": "user", "cwd": "/home/m/Projects/foo", "message": "hello"},
        ])
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/foo")

    # -- SDK entrypoint layout (two queue-operation lines, then assistant with cwd)
    def test_get_session_cwd_sdk_entrypoint(self) -> None:
        """cwd on line 3 after two queue-operation lines."""
        p = self._write_jsonl("sdk.jsonl", [
            {"type": "queue-operation", "op": "enqueue"},
            {"type": "queue-operation", "op": "dequeue"},
            {"type": "assistant", "cwd": "/home/m/Projects/bar", "text": "hi"},
        ])
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/bar")

    # -- Subagent layout (cwd on first line)
    def test_get_session_cwd_line_1(self) -> None:
        """cwd on the very first line (subagent format)."""
        p = self._write_jsonl("subagent.jsonl", [
            {"type": "system", "cwd": "/home/m/Projects/baz"},
        ])
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/baz")

    def test_get_session_cwd_empty_file(self) -> None:
        """An empty file returns None."""
        p = self.tmp_path / "empty.jsonl"
        p.write_text("")
        self.assertIsNone(get_session_cwd(p))

    def test_get_session_cwd_no_cwd_field(self) -> None:
        """Lines exist but none carry a cwd field."""
        p = self._write_jsonl("no_cwd.jsonl", [
            {"type": "permission-mode", "mode": "default"},
            {"type": "file-history-snapshot", "files": []},
            {"type": "metadata", "version": "1.0"},
        ])
        self.assertIsNone(get_session_cwd(p))

    def test_get_session_cwd_invalid_json_lines(self) -> None:
        """Corrupt lines are skipped; cwd is still found on a valid line."""
        p = self._write_jsonl("corrupt.jsonl", [
            "NOT VALID JSON {{{",
            "also broken",
            {"type": "user", "cwd": "/home/m/Projects/ok", "message": "works"},
        ])
        self.assertEqual(get_session_cwd(p), "/home/m/Projects/ok")

    def test_get_session_cwd_file_not_found(self) -> None:
        """A missing file returns None without raising."""
        self.assertIsNone(get_session_cwd(self.tmp_path / "nonexistent.jsonl"))

    def test_get_session_cwd_beyond_scan_limit(self) -> None:
        """cwd exists but only on line 15 — beyond the default scan limit."""
        lines: list[dict | str] = [
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
        self, encoded_cwd: str, session_id: str, lines: list[dict] | None = None,
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
        self.assertEqual(result.jsonl_path, self.projects_dir / encoded / f"{sid}.jsonl")
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
            encoded, sid,
            lines=[{"type": "user", "cwd": cwd_value, "message": "test"}],
        )

        result = find_session(sid, shared_projects_dir=self.projects_dir)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.cwd, cwd_value)
        self.assertEqual(result.encoded_cwd, encoded)


if __name__ == "__main__":
    unittest.main()
