"""Tests for claudewheel.migrate — session migration between profile dirs."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from claudewheel.migrate import (
    MigrateResult,
    _discover_uuids,
    _move_artifact,
    _shared_store,
    migrate_sessions,
)

# A few fixed UUIDs used across tests.
UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
UUID_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


# ---------------------------------------------------------------------------
# _discover_uuids
# ---------------------------------------------------------------------------


class DiscoverUuidsTests(unittest.TestCase):
    """UUID discovery from projects/, simple dirs, and todos/."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.src = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- projects/ --

    def test_finds_uuids_from_jsonl_files(self) -> None:
        """projects/<cwd>/<uuid>.jsonl files yield UUIDs."""
        cwd_dir = self.src / "projects" / "some-project"
        cwd_dir.mkdir(parents=True)
        (cwd_dir / f"{UUID_A}.jsonl").write_text("")

        result = _discover_uuids(self.src)
        self.assertEqual(result, {UUID_A})

    def test_finds_uuids_from_subdirs_in_projects(self) -> None:
        """projects/<cwd>/<uuid>/ subdirectories yield UUIDs."""
        sub = self.src / "projects" / "proj" / UUID_B
        sub.mkdir(parents=True)

        result = _discover_uuids(self.src)
        self.assertEqual(result, {UUID_B})

    # -- simple dirs --

    def test_finds_uuids_from_session_env(self) -> None:
        (self.src / "session-env" / UUID_A).mkdir(parents=True)
        self.assertIn(UUID_A, _discover_uuids(self.src))

    def test_finds_uuids_from_file_history(self) -> None:
        (self.src / "file-history" / UUID_B).mkdir(parents=True)
        self.assertIn(UUID_B, _discover_uuids(self.src))

    def test_finds_uuids_from_tasks(self) -> None:
        (self.src / "tasks" / UUID_C).mkdir(parents=True)
        self.assertIn(UUID_C, _discover_uuids(self.src))

    # -- todos/ --

    def test_finds_uuids_from_todos_prefix_keyed_files(self) -> None:
        """todos/<uuid>-agent-<rest>.json files yield UUIDs."""
        todos = self.src / "todos"
        todos.mkdir()
        (todos / f"{UUID_A}-agent-cleanup.json").write_text("{}")

        result = _discover_uuids(self.src)
        self.assertEqual(result, {UUID_A})

    # -- union / dedup --

    def test_union_across_all_dirs_no_duplicates(self) -> None:
        """The same UUID in multiple dirs appears only once."""
        (self.src / "projects" / "p").mkdir(parents=True)
        (self.src / "projects" / "p" / f"{UUID_A}.jsonl").write_text("")
        (self.src / "session-env" / UUID_A).mkdir(parents=True)
        (self.src / "todos").mkdir()
        (self.src / "todos" / f"{UUID_A}-agent-x.json").write_text("{}")

        result = _discover_uuids(self.src)
        self.assertEqual(result, {UUID_A})

    def test_multiple_distinct_uuids(self) -> None:
        """UUIDs from different dirs are all included."""
        (self.src / "projects" / "p").mkdir(parents=True)
        (self.src / "projects" / "p" / f"{UUID_A}.jsonl").write_text("")
        (self.src / "session-env" / UUID_B).mkdir(parents=True)
        (self.src / "tasks" / UUID_C).mkdir(parents=True)

        result = _discover_uuids(self.src)
        self.assertEqual(result, {UUID_A, UUID_B, UUID_C})

    # -- non-UUID filtering --

    def test_ignores_non_uuid_names(self) -> None:
        """Entries that don't match the UUID regex are silently skipped."""
        cwd_dir = self.src / "projects" / "p"
        cwd_dir.mkdir(parents=True)
        (cwd_dir / "not-a-uuid.jsonl").write_text("")
        (cwd_dir / "readme.md").write_text("")
        (self.src / "session-env").mkdir()
        (self.src / "session-env" / "just-text").mkdir()
        (self.src / "todos").mkdir()
        (self.src / "todos" / "bad-agent-x.json").write_text("{}")

        result = _discover_uuids(self.src)
        self.assertEqual(result, set())


# ---------------------------------------------------------------------------
# _move_artifact
# ---------------------------------------------------------------------------


class MoveArtifactTests(unittest.TestCase):
    """File/dir moving with collision detection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_moves_file_when_dst_does_not_exist(self) -> None:
        src = self.root / "src" / "file.txt"
        src.parent.mkdir()
        src.write_text("hello")
        dst = self.root / "dst" / "file.txt"
        result = MigrateResult()

        _move_artifact(src, dst, result, dry_run=False)

        self.assertEqual(result.moved, 1)
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())
        self.assertEqual(dst.read_text(), "hello")

    def test_collision_refuses_to_overwrite(self) -> None:
        """When dst already exists, the move is refused and collisions incremented."""
        src = self.root / "src.txt"
        src.write_text("source")
        dst = self.root / "dst.txt"
        dst.write_text("existing")
        result = MigrateResult()

        _move_artifact(src, dst, result, dry_run=False)

        self.assertEqual(result.collisions, 1)
        self.assertEqual(result.moved, 0)
        # Both files unchanged
        self.assertTrue(src.exists())
        self.assertEqual(dst.read_text(), "existing")

    def test_creates_parent_dirs_as_needed(self) -> None:
        src = self.root / "a.txt"
        src.write_text("content")
        dst = self.root / "deep" / "nested" / "dir" / "a.txt"
        result = MigrateResult()

        _move_artifact(src, dst, result, dry_run=False)

        self.assertEqual(result.moved, 1)
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_text(), "content")

    def test_dry_run_does_not_move(self) -> None:
        src = self.root / "f.txt"
        src.write_text("stay")
        dst = self.root / "out" / "f.txt"
        result = MigrateResult()

        _move_artifact(src, dst, result, dry_run=True)

        self.assertEqual(result.moved, 1)
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())


# ---------------------------------------------------------------------------
# _shared_store
# ---------------------------------------------------------------------------


class SharedStoreTests(unittest.TestCase):
    """Detection of shared backing store via symlinks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_true_when_both_symlink_to_same_target(self) -> None:
        target = self.root / "shared-projects"
        target.mkdir()
        src = self.root / "src"
        src.mkdir()
        dst = self.root / "dst"
        dst.mkdir()
        (src / "projects").symlink_to(target)
        (dst / "projects").symlink_to(target)

        self.assertTrue(_shared_store(src, dst))

    def test_returns_false_when_dirs_are_real(self) -> None:
        src = self.root / "src"
        dst = self.root / "dst"
        (src / "projects").mkdir(parents=True)
        (dst / "projects").mkdir(parents=True)

        self.assertFalse(_shared_store(src, dst))

    def test_returns_false_when_only_one_is_symlink(self) -> None:
        target = self.root / "shared-projects"
        target.mkdir()
        src = self.root / "src"
        src.mkdir()
        dst = self.root / "dst"
        (dst / "projects").mkdir(parents=True)
        (src / "projects").symlink_to(target)

        self.assertFalse(_shared_store(src, dst))


# ---------------------------------------------------------------------------
# migrate_sessions (full integration)
# ---------------------------------------------------------------------------


class MigrateSessionsTests(unittest.TestCase):
    """Full migrate_sessions() integration tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # Suppress migrate_sessions() print output
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()
        # Create profile dirs
        self.src_dir = self.home / ".claudewheel" / "profiles" / "alpha"
        self.dst_dir = self.home / ".claudewheel" / "profiles" / "beta"
        self.src_dir.mkdir(parents=True)
        self.dst_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def _populate_src(self) -> None:
        """Create a minimal set of artifacts in the source profile."""
        # projects/<cwd>/<uuid>.jsonl
        proj = self.src_dir / "projects" / "myproj"
        proj.mkdir(parents=True)
        (proj / f"{UUID_A}.jsonl").write_text('{"msg":"hello"}')
        # session-env/<uuid>/
        (self.src_dir / "session-env" / UUID_A).mkdir(parents=True)
        # todos/<uuid>-agent-<rest>.json
        todos = self.src_dir / "todos"
        todos.mkdir()
        (todos / f"{UUID_A}-agent-cleanup.json").write_text("{}")

    def _run_migrate(self, *args, **kwargs):
        """Run migrate_sessions against a sandbox workspace."""
        from claudewheel.workspace import Workspace

        ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )
        return migrate_sessions(ws, *args, **kwargs)

    def test_moves_non_shared(self) -> None:
        """In non-shared mode, artifacts are moved to the destination."""
        self._populate_src()
        # Also create required dirs in dst
        (self.dst_dir / "projects" / "myproj").mkdir(parents=True)

        result = self._run_migrate("alpha", "beta")

        self.assertEqual(result.uuids_found, 1)
        self.assertGreater(result.moved, 0)
        self.assertEqual(result.collisions, 0)
        # The jsonl should have been moved to dst
        moved_jsonl = self.dst_dir / "projects" / "myproj" / f"{UUID_A}.jsonl"
        self.assertTrue(moved_jsonl.exists())
        orig_jsonl = self.src_dir / "projects" / "myproj" / f"{UUID_A}.jsonl"
        self.assertFalse(orig_jsonl.exists())

    def test_skips_moves_shared_store(self) -> None:
        """When stores are shared, artifacts are not moved."""
        # Create a shared target for projects/
        shared = self.home / "shared-projects"
        shared.mkdir()
        # Point both profile dirs' projects/ at the shared target
        (shared / "myproj").mkdir()
        (shared / "myproj" / f"{UUID_A}.jsonl").write_text("{}")
        (self.src_dir / "projects").symlink_to(shared)
        (self.dst_dir / "projects").symlink_to(shared)

        result = self._run_migrate("alpha", "beta")

        self.assertEqual(result.uuids_found, 1)
        self.assertEqual(result.moved, 0)
        # File is still in place (not moved)
        self.assertTrue((shared / "myproj" / f"{UUID_A}.jsonl").exists())

    def test_uuid_filter_substring_match(self) -> None:
        """uuid_filter keeps only UUIDs containing the given substring."""
        # Create two UUIDs
        proj = self.src_dir / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / f"{UUID_A}.jsonl").write_text("")
        (proj / f"{UUID_B}.jsonl").write_text("")

        # Filter for UUID_A's prefix ("aaaa")
        result = self._run_migrate("alpha", "beta", uuid_filter="aaaa")

        self.assertEqual(result.uuids_found, 1)

    def test_dry_run_does_not_write(self) -> None:
        """Dry run reports counts but makes no filesystem changes."""
        self._populate_src()

        result = self._run_migrate("alpha", "beta", dry_run=True)

        self.assertEqual(result.uuids_found, 1)
        self.assertGreater(result.moved, 0)
        # But nothing actually moved
        orig_jsonl = self.src_dir / "projects" / "myproj" / f"{UUID_A}.jsonl"
        self.assertTrue(orig_jsonl.exists())

    def _run_migrate_with_default(self, *args, **kwargs):
        """Run migrate_sessions with the sandbox's ~/.claude as the default dir.

        The workspace's claude_dir is set to the sandbox home's ~/.claude so
        path_for("default") resolves there rather than the real home.
        """
        from claudewheel.workspace import Workspace

        ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )
        return migrate_sessions(ws, *args, **kwargs)

    def test_migrate_to_default(self) -> None:
        """Session data migrates INTO the ~/.claude default profile."""
        self._populate_src()  # alpha holds artifacts
        default_dir = self.home / ".claude"
        (default_dir / "projects" / "myproj").mkdir(parents=True)

        result = self._run_migrate_with_default("alpha", "default")

        self.assertEqual(result.uuids_found, 1)
        moved = default_dir / "projects" / "myproj" / f"{UUID_A}.jsonl"
        self.assertTrue(moved.exists())
        orig = self.src_dir / "projects" / "myproj" / f"{UUID_A}.jsonl"
        self.assertFalse(orig.exists())

    def test_migrate_from_default(self) -> None:
        """Session data migrates OUT of the ~/.claude default profile."""
        default_dir = self.home / ".claude"
        proj = default_dir / "projects" / "myproj"
        proj.mkdir(parents=True)
        (proj / f"{UUID_A}.jsonl").write_text('{"msg":"hi"}')
        (self.dst_dir / "projects" / "myproj").mkdir(parents=True)

        result = self._run_migrate_with_default("default", "beta")

        self.assertEqual(result.uuids_found, 1)
        moved = self.dst_dir / "projects" / "myproj" / f"{UUID_A}.jsonl"
        self.assertTrue(moved.exists())
        self.assertFalse((proj / f"{UUID_A}.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
