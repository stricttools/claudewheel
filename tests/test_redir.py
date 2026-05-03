"""Tests for claude_launcher.redir — redirect session data after a project rename."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_launcher.redir import (
    RedirResult,
    _encode_path,
    _rewrite_jsonl_file,
    _update_claude_json,
    run_redir,
)


# ---------------------------------------------------------------------------
# _encode_path
# ---------------------------------------------------------------------------


class EncodePathTests(unittest.TestCase):
    """Slash-to-dash encoding used by Claude Code for project directory names."""

    def test_replaces_slashes_with_dashes(self) -> None:
        self.assertEqual(_encode_path("/home/m/Projects/Foo"), "-home-m-Projects-Foo")

    def test_empty_string(self) -> None:
        self.assertEqual(_encode_path(""), "")

    def test_no_slashes(self) -> None:
        self.assertEqual(_encode_path("plain"), "plain")


# ---------------------------------------------------------------------------
# _rewrite_jsonl_file
# ---------------------------------------------------------------------------


class RewriteJsonlFileTests(unittest.TestCase):
    """Line-by-line replacement inside JSONL files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_replaces_old_path_in_matching_lines(self) -> None:
        """Lines containing old_path are rewritten; others are left alone."""
        f = self.tmp_path / "session.jsonl"
        f.write_text(
            '{"cwd": "/old/proj", "msg": "hi"}\n'
            '{"cwd": "/other", "msg": "ok"}\n'
            '{"cwd": "/old/proj", "msg": "bye"}\n'
        )

        replaced = _rewrite_jsonl_file(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertEqual(replaced, 2)
        lines = f.read_text().splitlines()
        self.assertIn("/new/proj", lines[0])
        self.assertNotIn("/old/proj", lines[0])
        # Unchanged line
        self.assertEqual(lines[1], '{"cwd": "/other", "msg": "ok"}')
        self.assertIn("/new/proj", lines[2])

    def test_returns_zero_when_no_matches(self) -> None:
        """When no line contains old_path, returns 0 and file is untouched."""
        f = self.tmp_path / "clean.jsonl"
        original = '{"cwd": "/unrelated"}\n'
        f.write_text(original)

        replaced = _rewrite_jsonl_file(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertEqual(replaced, 0)
        self.assertEqual(f.read_text(), original)

    def test_dry_run_does_not_modify_file(self) -> None:
        """Dry run returns the count but leaves the file unchanged."""
        f = self.tmp_path / "data.jsonl"
        original = '{"cwd": "/old/proj"}\n'
        f.write_text(original)

        replaced = _rewrite_jsonl_file(f, "/old/proj", "/new/proj", dry_run=True)

        self.assertEqual(replaced, 1)
        self.assertEqual(f.read_text(), original)


# ---------------------------------------------------------------------------
# _update_claude_json
# ---------------------------------------------------------------------------


class UpdateClaudeJsonTests(unittest.TestCase):
    """Top-level key rename in .claude.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_renames_matching_key(self) -> None:
        f = self.tmp_path / ".claude.json"
        f.write_text(json.dumps({"/old/proj": {"setting": 1}, "/other": {"x": 2}}))

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertTrue(result)
        data = json.loads(f.read_text())
        self.assertIn("/new/proj", data)
        self.assertNotIn("/old/proj", data)
        # Value preserved
        self.assertEqual(data["/new/proj"], {"setting": 1})
        # Other key untouched
        self.assertIn("/other", data)

    def test_returns_false_when_key_missing(self) -> None:
        f = self.tmp_path / ".claude.json"
        f.write_text(json.dumps({"/other": {}}))

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertFalse(result)

    def test_dry_run_returns_true_but_does_not_modify(self) -> None:
        f = self.tmp_path / ".claude.json"
        original = json.dumps({"/old/proj": {"val": 42}})
        f.write_text(original)

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=True)

        self.assertTrue(result)
        # File unchanged
        self.assertEqual(json.loads(f.read_text()), json.loads(original))


# ---------------------------------------------------------------------------
# run_redir (integration)
# ---------------------------------------------------------------------------


class RunRedirValidationTests(unittest.TestCase):
    """Precondition checks: new_path must exist, old_path must not."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_raises_when_new_path_does_not_exist(self) -> None:
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        # Neither exists

        with self.assertRaises(FileNotFoundError):
            run_redir(str(old), str(new))

    def test_raises_when_old_path_still_exists(self) -> None:
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        old.mkdir()
        new.mkdir()

        with self.assertRaises(FileExistsError):
            run_redir(str(old), str(new))


class RunRedirIntegrationTests(unittest.TestCase):
    """Full integration: project dir rename, JSONL rewrite, .claude.json update."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Simulated project directories
        self.old_dir = self.home / "Projects" / "OldName"
        self.new_dir = self.home / "Projects" / "NewName"
        # Only new_dir exists (rename already happened)
        self.new_dir.mkdir(parents=True)

        # Resolved path strings (as they would appear in session data)
        self.old_resolved = str(self.old_dir)
        self.new_resolved = str(self.new_dir)

        # Encoded directory names
        self.old_encoded = self.old_resolved.replace("/", "-")
        self.new_encoded = self.new_resolved.replace("/", "-")

        # Profile dir with projects/ and .claude.json
        self.profile = self.home / ".claude-personal"
        self.profile.mkdir()
        self.projects = self.profile / "projects"
        self.projects.mkdir()

        # Old project dir with a session JSONL referencing old_resolved
        self.old_project = self.projects / self.old_encoded
        self.old_project.mkdir()
        self.session_jsonl = self.old_project / "session.jsonl"
        self.session_jsonl.write_text(
            json.dumps({"cwd": self.old_resolved, "type": "init"}) + "\n"
            + json.dumps({"msg": "hello"}) + "\n"
            + json.dumps({"cwd": self.old_resolved, "type": "resume"}) + "\n"
        )

        # .claude.json with old path as a key
        self.claude_json = self.profile / ".claude.json"
        self.claude_json.write_text(json.dumps({
            self.old_resolved: {"lastSession": "abc123"},
            "/other/project": {"lastSession": "xyz"},
        }))

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def _run(self, dry_run: bool = False) -> RedirResult:
        """Run redir with patched home and profile discovery."""
        with patch("claude_launcher.redir.Path.home", return_value=self.home), \
             patch(
                 "claude_launcher.redir._discover_profile_dirs",
                 return_value=[self.profile],
             ):
            return run_redir(str(self.old_dir), str(self.new_dir), dry_run=dry_run)

    def test_full_redir(self) -> None:
        """Dir renamed, JSONL rewritten, .claude.json key updated, counters correct."""
        result = self._run()

        # Project dir renamed
        self.assertEqual(result.dirs_renamed, 1)
        new_project = self.projects / self.new_encoded
        self.assertTrue(new_project.is_dir())
        self.assertFalse(self.old_project.exists())

        # JSONL rewritten
        self.assertEqual(result.files_rewritten, 1)
        self.assertEqual(result.lines_replaced, 2)
        rewritten = (new_project / "session.jsonl").read_text().splitlines()
        for line in rewritten:
            self.assertNotIn(self.old_resolved, line)
        self.assertIn(self.new_resolved, rewritten[0])
        # Unchanged line preserved
        self.assertEqual(json.loads(rewritten[1]), {"msg": "hello"})

        # .claude.json key updated
        self.assertEqual(result.project_keys_updated, 1)
        data = json.loads(self.claude_json.read_text())
        self.assertIn(self.new_resolved, data)
        self.assertNotIn(self.old_resolved, data)
        self.assertEqual(data[self.new_resolved], {"lastSession": "abc123"})
        self.assertIn("/other/project", data)

        # Profile count
        self.assertEqual(result.profiles_scanned, 1)

    def test_dry_run_leaves_disk_unchanged(self) -> None:
        """Dry run reports what would happen but makes no filesystem changes."""
        original_jsonl = self.session_jsonl.read_text()
        original_json = self.claude_json.read_text()

        result = self._run(dry_run=True)

        # Counters reflect intended work
        self.assertEqual(result.dirs_renamed, 1)
        self.assertEqual(result.files_rewritten, 0)  # file not rewritten in dry run
        self.assertEqual(result.project_keys_updated, 1)

        # Nothing changed on disk
        self.assertTrue(self.old_project.is_dir())
        self.assertFalse((self.projects / self.new_encoded).exists())
        self.assertEqual(self.session_jsonl.read_text(), original_jsonl)
        self.assertEqual(self.claude_json.read_text(), original_json)

    def test_skips_history_jsonl(self) -> None:
        """history.jsonl files are not rewritten even if they contain old_path."""
        # Pre-rename the project dir so the JSONL scan runs
        new_project = self.projects / self.new_encoded
        self.old_project.rename(new_project)

        history = new_project / "history.jsonl"
        history.write_text(json.dumps({"cwd": self.old_resolved}) + "\n")

        result = self._run()

        # history.jsonl should be untouched
        self.assertIn(self.old_resolved, history.read_text())
        # Only session.jsonl was rewritten (has 2 matching lines)
        self.assertEqual(result.files_rewritten, 1)

    def test_skips_shared_dir_for_claude_json(self) -> None:
        """The shared dir is scanned for projects/ but not for .claude.json."""
        shared = self.home / ".claude-shared"
        shared.mkdir()
        shared_json = shared / ".claude.json"
        shared_json.write_text(json.dumps({self.old_resolved: {"x": 1}}))

        with patch("claude_launcher.redir.Path.home", return_value=self.home), \
             patch(
                 "claude_launcher.redir._discover_profile_dirs",
                 return_value=[self.profile, shared],
             ):
            result = run_redir(str(self.old_dir), str(self.new_dir))

        # Only the profile's .claude.json was updated, not shared's
        self.assertEqual(result.project_keys_updated, 1)
        shared_data = json.loads(shared_json.read_text())
        self.assertIn(self.old_resolved, shared_data)
        self.assertNotIn(self.new_resolved, shared_data)


if __name__ == "__main__":
    unittest.main()
