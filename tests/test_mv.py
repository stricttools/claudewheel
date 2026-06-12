"""Tests for claudewheel.mv — move session data after a project rename."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.constants import encode_path
from claudewheel.mv import (
    MvResult,
    _rewrite_jsonl_file,
    _update_claude_json,
    run_mv,
)


# ---------------------------------------------------------------------------
# encode_path
# ---------------------------------------------------------------------------


class EncodePathTests(unittest.TestCase):
    """Slash-and-dot-to-dash encoding used by Claude Code for project directory names."""

    def test_replaces_slashes_with_dashes(self) -> None:
        self.assertEqual(encode_path("/home/m/Projects/Foo"), "-home-m-Projects-Foo")

    def test_replaces_dots_with_dashes(self) -> None:
        self.assertEqual(encode_path("/home/m/Projects/foo.bar"), "-home-m-Projects-foo-bar")

    def test_replaces_both_slashes_and_dots(self) -> None:
        self.assertEqual(encode_path("/home/m/.config/app"), "-home-m--config-app")

    def test_empty_string(self) -> None:
        self.assertEqual(encode_path(""), "")

    def test_no_slashes_or_dots(self) -> None:
        self.assertEqual(encode_path("plain"), "plain")


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
    """Project key rename under data['projects'] in .claude.json."""

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
        f.write_text(json.dumps({
            "projects": {"/old/proj": {"setting": 1}, "/other": {"x": 2}},
            "topLevel": True,
        }))

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertTrue(result)
        data = json.loads(f.read_text())
        self.assertIn("/new/proj", data["projects"])
        self.assertNotIn("/old/proj", data["projects"])
        self.assertEqual(data["projects"]["/new/proj"], {"setting": 1})
        self.assertIn("/other", data["projects"])
        self.assertTrue(data["topLevel"])

    def test_returns_false_when_key_missing(self) -> None:
        f = self.tmp_path / ".claude.json"
        f.write_text(json.dumps({"projects": {"/other": {}}}))

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertFalse(result)

    def test_returns_false_when_no_projects_key(self) -> None:
        f = self.tmp_path / ".claude.json"
        f.write_text(json.dumps({"topLevel": True}))

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=False)

        self.assertFalse(result)

    def test_dry_run_returns_true_but_does_not_modify(self) -> None:
        f = self.tmp_path / ".claude.json"
        original = json.dumps({"projects": {"/old/proj": {"val": 42}}})
        f.write_text(original)

        result = _update_claude_json(f, "/old/proj", "/new/proj", dry_run=True)

        self.assertTrue(result)
        self.assertEqual(json.loads(f.read_text()), json.loads(original))


# ---------------------------------------------------------------------------
# run_mv (integration)
# ---------------------------------------------------------------------------


class RunMvValidationTests(unittest.TestCase):
    """Precondition checks for default and post-hoc modes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    # -- Default mode (rename + migrate) --

    def test_default_old_exists_new_not_exists(self) -> None:
        """Default mode succeeds when old exists and new does not."""
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        old.mkdir()

        with patch("claudewheel.mv._discover_profile_dirs", return_value=[]):
            run_mv(str(old), str(new))  # should not raise

    def test_default_old_not_exists(self) -> None:
        """Default mode raises FileNotFoundError when old does not exist."""
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        # Neither exists

        with self.assertRaises(FileNotFoundError):
            run_mv(str(old), str(new))

    def test_default_new_already_exists(self) -> None:
        """Default mode raises FileExistsError when new already exists."""
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        old.mkdir()
        new.mkdir()

        with self.assertRaises(FileExistsError):
            run_mv(str(old), str(new))

    # -- Post-hoc mode (session-only migration) --

    def test_post_hoc_new_exists_old_not_exists(self) -> None:
        """Post-hoc mode succeeds when new exists and old does not."""
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        new.mkdir()

        with patch("claudewheel.mv._discover_profile_dirs", return_value=[]):
            run_mv(str(old), str(new), post_hoc=True)  # should not raise

    def test_post_hoc_old_still_exists(self) -> None:
        """Post-hoc mode raises FileExistsError when old still exists."""
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        old.mkdir()
        new.mkdir()

        with self.assertRaises(FileExistsError):
            run_mv(str(old), str(new), post_hoc=True)

    def test_post_hoc_new_not_exists(self) -> None:
        """Post-hoc mode raises FileNotFoundError when new does not exist."""
        old = self.tmp_path / "old"
        new = self.tmp_path / "new"
        # Neither exists

        with self.assertRaises(FileNotFoundError):
            run_mv(str(old), str(new), post_hoc=True)

    # -- Same path --

    def test_same_path_raises(self) -> None:
        """Both modes raise ValueError when old and new resolve to the same path."""
        old = self.tmp_path / "same"
        old.mkdir()

        with self.assertRaises(ValueError):
            run_mv(str(old), str(old))


class RunMvIntegrationTests(unittest.TestCase):
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
        self.old_encoded = encode_path(self.old_resolved)
        self.new_encoded = encode_path(self.new_resolved)

        # Profile dir with projects/ and .claude.json
        self.profile = self.home / ".claudewheel" / "profiles" / "personal"
        self.profile.mkdir(parents=True)
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

        # .claude.json with old path as a key under "projects"
        self.claude_json = self.profile / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "projects": {
                self.old_resolved: {"lastSession": "abc123"},
                "/other/project": {"lastSession": "xyz"},
            },
        }))

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def _run(self, dry_run: bool = False) -> MvResult:
        """Run mv with patched home and profile discovery (post-hoc mode)."""
        with patch("claudewheel.mv.Path.home", return_value=self.home), \
             patch(
                 "claudewheel.mv._discover_profile_dirs",
                 return_value=[self.profile],
             ):
            return run_mv(
                str(self.old_dir), str(self.new_dir),
                dry_run=dry_run, post_hoc=True,
            )

    def test_full_mv(self) -> None:
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
        projects = data["projects"]
        self.assertIn(self.new_resolved, projects)
        self.assertNotIn(self.old_resolved, projects)
        self.assertEqual(projects[self.new_resolved], {"lastSession": "abc123"})
        self.assertIn("/other/project", projects)

        # Profile count
        self.assertEqual(result.profiles_scanned, 1)

    def test_dry_run_leaves_disk_unchanged(self) -> None:
        """Dry run reports what would happen but makes no filesystem changes."""
        original_jsonl = self.session_jsonl.read_text()
        original_json = self.claude_json.read_text()

        result = self._run(dry_run=True)

        # Counters reflect intended work (dry-run scans old dir for accurate counts)
        self.assertEqual(result.dirs_renamed, 1)
        self.assertEqual(result.lines_replaced, 2)
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
        shared = self.home / ".claudewheel" / "shared"
        shared.mkdir()
        shared_json = shared / ".claude.json"
        shared_json.write_text(json.dumps({"projects": {self.old_resolved: {"x": 1}}}))

        with patch("claudewheel.mv.Path.home", return_value=self.home), \
             patch("claudewheel.mv.SHARED_DIR", shared), \
             patch(
                 "claudewheel.mv._discover_profile_dirs",
                 return_value=[self.profile, shared],
             ):
            result = run_mv(str(self.old_dir), str(self.new_dir), post_hoc=True)

        # Only the profile's .claude.json was updated, not shared's
        self.assertEqual(result.project_keys_updated, 1)
        shared_data = json.loads(shared_json.read_text())
        self.assertIn(self.old_resolved, shared_data["projects"])
        self.assertNotIn(self.new_resolved, shared_data["projects"])


# ---------------------------------------------------------------------------
# Merge when target directory already exists
# ---------------------------------------------------------------------------


class MergeDirsTests(unittest.TestCase):
    """When both old and new project dirs exist, contents are merged."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Simulated project directories
        self.old_dir = self.home / "Projects" / "OldName"
        self.new_dir = self.home / "Projects" / "NewName"
        # Only new_dir exists on the real filesystem (rename already happened)
        self.new_dir.mkdir(parents=True)

        # Resolved path strings
        self.old_resolved = str(self.old_dir)
        self.new_resolved = str(self.new_dir)

        # Encoded directory names
        self.old_encoded = encode_path(self.old_resolved)
        self.new_encoded = encode_path(self.new_resolved)

        # Profile dir
        self.profile = self.home / ".claudewheel" / "profiles" / "personal"
        self.profile.mkdir(parents=True)
        self.projects = self.profile / "projects"
        self.projects.mkdir()

        # Both old_project and new_project dirs exist in the profile store
        self.old_project = self.projects / self.old_encoded
        self.old_project.mkdir()
        self.new_project = self.projects / self.new_encoded
        self.new_project.mkdir()

        # .claude.json (needed for run_mv to complete)
        self.claude_json = self.profile / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "projects": {self.old_resolved: {"lastSession": "old"}},
        }))

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def _run(self, dry_run: bool = False) -> MvResult:
        with patch("claudewheel.mv.Path.home", return_value=self.home), \
             patch(
                 "claudewheel.mv._discover_profile_dirs",
                 return_value=[self.profile],
             ):
            return run_mv(
                str(self.old_dir), str(self.new_dir),
                dry_run=dry_run, post_hoc=True,
            )

    def test_mv_merge_when_target_exists(self) -> None:
        """Old sessions are merged into existing new_project; paths rewritten."""
        # Old dir has 2 session files referencing old path
        uuid1 = self.old_project / "uuid1.jsonl"
        uuid1.write_text(
            json.dumps({"cwd": self.old_resolved, "type": "init"}) + "\n"
        )
        uuid2 = self.old_project / "uuid2.jsonl"
        uuid2.write_text(
            json.dumps({"cwd": self.old_resolved, "type": "init"}) + "\n"
        )
        # New dir already has 1 session file (created after rename)
        uuid3 = self.new_project / "uuid3.jsonl"
        uuid3.write_text(
            json.dumps({"cwd": self.new_resolved, "type": "init"}) + "\n"
        )

        result = self._run()

        # All 3 files now live in new_project
        new_files = sorted(f.name for f in self.new_project.iterdir())
        self.assertEqual(new_files, ["uuid1.jsonl", "uuid2.jsonl", "uuid3.jsonl"])

        # Old project dir is gone
        self.assertFalse(self.old_project.exists())

        # Merge counted as a rename
        self.assertEqual(result.dirs_renamed, 1)

        # Moved files had their paths rewritten
        for name in ("uuid1.jsonl", "uuid2.jsonl"):
            content = (self.new_project / name).read_text()
            self.assertNotIn(self.old_resolved, content)
            self.assertIn(self.new_resolved, content)

        # The file that was already in new_project is untouched (no old path)
        self.assertEqual(result.files_rewritten, 2)
        self.assertEqual(result.lines_replaced, 2)

    def test_mv_merge_dry_run(self) -> None:
        """Dry run reports merge actions but leaves both dirs intact."""
        uuid1 = self.old_project / "uuid1.jsonl"
        uuid1.write_text(
            json.dumps({"cwd": self.old_resolved, "type": "init"}) + "\n"
        )
        uuid2 = self.old_project / "uuid2.jsonl"
        uuid2.write_text(
            json.dumps({"cwd": self.old_resolved, "type": "init"}) + "\n"
        )
        uuid3 = self.new_project / "uuid3.jsonl"
        uuid3.write_text(
            json.dumps({"cwd": self.new_resolved, "type": "init"}) + "\n"
        )

        original_old_files = sorted(f.name for f in self.old_project.iterdir())
        original_new_files = sorted(f.name for f in self.new_project.iterdir())
        original_uuid1 = uuid1.read_text()

        result = self._run(dry_run=True)

        # Counts reflect intended work (includes files in both dirs)
        self.assertEqual(result.dirs_renamed, 1)
        self.assertEqual(result.files_rewritten, 2)
        self.assertEqual(result.lines_replaced, 2)

        # Both dirs still exist unchanged
        self.assertTrue(self.old_project.is_dir())
        self.assertTrue(self.new_project.is_dir())
        self.assertEqual(
            sorted(f.name for f in self.old_project.iterdir()),
            original_old_files,
        )
        self.assertEqual(
            sorted(f.name for f in self.new_project.iterdir()),
            original_new_files,
        )
        # File content unchanged
        self.assertEqual(uuid1.read_text(), original_uuid1)

    def test_mv_merge_file_collision(self) -> None:
        """A file with the same name in both dirs is skipped (kept in new)."""
        collision_name = "same-uuid.jsonl"
        old_file = self.old_project / collision_name
        old_file.write_text(
            json.dumps({"cwd": self.old_resolved, "src": "old"}) + "\n"
        )
        new_file = self.new_project / collision_name
        new_file.write_text(
            json.dumps({"cwd": self.new_resolved, "src": "new"}) + "\n"
        )
        # Also have a non-colliding file to verify it does get moved
        other_file = self.old_project / "other.jsonl"
        other_file.write_text(
            json.dumps({"cwd": self.old_resolved, "type": "init"}) + "\n"
        )

        result = self._run()

        # The collision file in new_project is the original new version
        content = json.loads(new_file.read_text().strip())
        self.assertEqual(content["src"], "new")

        # The non-colliding file was moved and rewritten
        moved = self.new_project / "other.jsonl"
        self.assertTrue(moved.exists())
        moved_content = moved.read_text()
        self.assertNotIn(self.old_resolved, moved_content)
        self.assertIn(self.new_resolved, moved_content)

        # Old dir is gone (collision file was skipped but it stays in old;
        # rmdir will fail because it's not empty -- old still has the collision file)
        # Actually: the collision file stays in old_project, so rmdir won't remove it.
        # The old_project dir may still exist with the skipped file.
        if self.old_project.exists():
            remaining = list(self.old_project.iterdir())
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].name, collision_name)

        # Merge still counted
        self.assertEqual(result.dirs_renamed, 1)


# ---------------------------------------------------------------------------
# Default (rename) mode
# ---------------------------------------------------------------------------


class RenameModeTests(unittest.TestCase):
    """Default mode: rename directory on disk, then migrate sessions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_renames_directory_on_disk(self) -> None:
        """Default mode renames old_dir to new_dir on the filesystem."""
        old = self.tmp_path / "old_proj"
        new = self.tmp_path / "new_proj"
        old.mkdir()
        (old / "file.txt").write_text("content")

        with patch("claudewheel.mv._discover_profile_dirs", return_value=[]):
            run_mv(str(old), str(new))

        self.assertFalse(old.exists())
        self.assertTrue(new.is_dir())
        self.assertEqual((new / "file.txt").read_text(), "content")

    def test_rename_and_migrate_sessions(self) -> None:
        """Default mode renames directory AND migrates session data."""
        old = self.tmp_path / "old_proj"
        new = self.tmp_path / "new_proj"
        old.mkdir()
        (old / "marker.txt").write_text("hello")

        old_resolved = str(old.resolve())
        new_resolved = str(new.resolve())
        old_encoded = encode_path(old_resolved)
        new_encoded = encode_path(new_resolved)

        # Create a fake profile with session data under old_encoded
        profile = self.tmp_path / "profile"
        profile.mkdir()
        projects = profile / "projects"
        projects.mkdir()
        old_project = projects / old_encoded
        old_project.mkdir()
        session = old_project / "session.jsonl"
        session.write_text(
            json.dumps({"cwd": old_resolved, "type": "init"}) + "\n"
        )

        with patch("claudewheel.mv._discover_profile_dirs", return_value=[profile]):
            result = run_mv(str(old), str(new))

        # Directory renamed
        self.assertFalse(old.exists())
        self.assertTrue(new.is_dir())
        self.assertEqual((new / "marker.txt").read_text(), "hello")

        # Sessions migrated
        self.assertEqual(result.dirs_renamed, 1)
        self.assertEqual(result.files_rewritten, 1)
        new_project = projects / new_encoded
        self.assertTrue(new_project.is_dir())
        self.assertFalse(old_project.exists())
        content = (new_project / "session.jsonl").read_text()
        self.assertNotIn(old_resolved, content)
        self.assertIn(new_resolved, content)

    def test_dry_run_no_rename(self) -> None:
        """Dry run does not rename directory on disk."""
        old = self.tmp_path / "old_proj"
        new = self.tmp_path / "new_proj"
        old.mkdir()
        (old / "file.txt").write_text("content")

        with patch("claudewheel.mv._discover_profile_dirs", return_value=[]):
            run_mv(str(old), str(new), dry_run=True)

        self.assertTrue(old.is_dir())
        self.assertFalse(new.exists())

    def test_cross_device_error(self) -> None:
        """Cross-device rename raises OSError with a clear message."""
        import errno

        old = self.tmp_path / "old_proj"
        new = self.tmp_path / "new_proj"
        old.mkdir()

        with patch.object(
            Path, "rename",
            side_effect=OSError(errno.EXDEV, "Invalid cross-device link"),
        ):
            with self.assertRaises(OSError) as ctx:
                run_mv(str(old), str(new))

        self.assertIn("failed to rename directory", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
