"""Tests for claudewheel.import_ -- external session data import."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.constants import encode_path
from claudewheel.import_ import (
    ImportResult,
    _apply_rewrites,
    _build_rewriters,
    _normalize_cwd,
    _rewrite_jsonl,
    _scan_source,
    run_import,
)

UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
UUID_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_jsonl_line(**fields: object) -> str:
    """Build a single JSONL line with proper JSON escaping."""
    return json.dumps(fields)


def _make_session_jsonl(cwd: str, session_id: str) -> str:
    """Build a minimal two-line session JSONL blob.

    Uses forward-slash cwds (safe for JSON serialization and matched by
    the forward-slash rewriter pattern).
    """
    lines = [
        _make_jsonl_line(
            cwd=cwd, sessionId=session_id, type="user",
            uuid="u1", message={"content": "hi"},
        ),
        _make_jsonl_line(
            cwd=cwd, sessionId=session_id, type="assistant",
            uuid="u2", parentUuid="u1",
        ),
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# _normalize_cwd
# ---------------------------------------------------------------------------


class NormalizeCwdTests(unittest.TestCase):
    """Normalize cwds: case-fold drive letter, strip trailing separators."""

    def test_uppercase_drive_letter_folded(self) -> None:
        """C:\\ becomes c:\\."""
        self.assertEqual(_normalize_cwd("C:\\Users"), "c:\\Users")

    def test_trailing_backslash_stripped(self) -> None:
        """Trailing \\ is removed."""
        self.assertEqual(_normalize_cwd("c:\\Users\\"), "c:\\Users")

    def test_trailing_forward_slash_stripped(self) -> None:
        """Trailing / is removed."""
        self.assertEqual(_normalize_cwd("/home/m/"), "/home/m")

    def test_linux_path_unchanged(self) -> None:
        """A well-formed Linux path passes through unmodified."""
        self.assertEqual(_normalize_cwd("/home/m/projects"), "/home/m/projects")

    def test_drive_letter_only_at_position_zero(self) -> None:
        """A colon at position 1 mid-path is not case-folded."""
        self.assertEqual(
            _normalize_cwd("/some/C:/path"), "/some/C:/path",
        )

    def test_empty_string(self) -> None:
        """Empty input returns empty output."""
        self.assertEqual(_normalize_cwd(""), "")

    def test_windows_forward_slash_path(self) -> None:
        """Windows path using forward slashes gets drive letter folded."""
        self.assertEqual(_normalize_cwd("C:/Users/m/"), "c:/Users/m")


# ---------------------------------------------------------------------------
# _scan_source
# ---------------------------------------------------------------------------


class ScanSourceTests(unittest.TestCase):
    """Walking a source directory to discover session bundles."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discovers_session_bundle(self) -> None:
        """A valid <encoded>/<uuid>.jsonl is collected as a bundle."""
        enc = self.projects / "some-project"
        enc.mkdir()
        jsonl = enc / f"{UUID_A}.jsonl"
        jsonl.write_text(_make_jsonl_line(cwd="/home/m/test") + "\n")

        with patch("claudewheel.import_.get_session_cwd", return_value="/home/m/test"):
            bundles = _scan_source(self.root)

        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].uuid, UUID_A)
        self.assertEqual(bundles[0].cwd, "/home/m/test")
        self.assertEqual(bundles[0].jsonl_path, jsonl)
        self.assertIsNone(bundles[0].companion_dir)

    def test_detects_companion_directory(self) -> None:
        """A <uuid>/ sibling directory is recorded as companion_dir."""
        enc = self.projects / "proj"
        enc.mkdir()
        (enc / f"{UUID_A}.jsonl").write_text(
            _make_jsonl_line(cwd="/home/m/test") + "\n"
        )
        companion = enc / UUID_A
        companion.mkdir()

        with patch("claudewheel.import_.get_session_cwd", return_value="/home/m/test"):
            bundles = _scan_source(self.root)

        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].companion_dir, companion)

    def test_ignores_non_uuid_files(self) -> None:
        """Files not matching the UUID pattern are skipped."""
        enc = self.projects / "proj"
        enc.mkdir()
        (enc / "readme.md").write_text("hello")
        (enc / "not-a-uuid.jsonl").write_text("{}\n")

        with patch("claudewheel.import_.get_session_cwd"):
            bundles = _scan_source(self.root)

        self.assertEqual(len(bundles), 0)

    def test_raises_on_empty_jsonl(self) -> None:
        """A JSONL with no cwd field raises ValueError."""
        enc = self.projects / "proj"
        enc.mkdir()
        (enc / f"{UUID_A}.jsonl").write_text("{}\n")

        with patch("claudewheel.import_.get_session_cwd", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                _scan_source(self.root)

        self.assertIn(str(UUID_A), str(ctx.exception))

    def test_raises_on_missing_projects_dir(self) -> None:
        """_scan_source raises when projects/ does not exist."""
        empty = self.root / "noprojects"
        empty.mkdir()
        with self.assertRaises((FileNotFoundError, OSError)):
            _scan_source(empty)

    def test_multiple_encoded_dirs(self) -> None:
        """Bundles from multiple encoded project dirs are all collected."""
        for name, uuid in [("proj-a", UUID_A), ("proj-b", UUID_B)]:
            enc = self.projects / name
            enc.mkdir()
            (enc / f"{uuid}.jsonl").write_text(
                _make_jsonl_line(cwd="/test") + "\n"
            )

        with patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            bundles = _scan_source(self.root)

        self.assertEqual(len(bundles), 2)
        uuids = {b.uuid for b in bundles}
        self.assertEqual(uuids, {UUID_A, UUID_B})

    def test_records_source_encoded_dir(self) -> None:
        """The source_encoded_dir field captures the encoded project dir name."""
        enc = self.projects / "my-encoded-proj"
        enc.mkdir()
        (enc / f"{UUID_A}.jsonl").write_text(
            _make_jsonl_line(cwd="/test") + "\n"
        )

        with patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            bundles = _scan_source(self.root)

        self.assertEqual(bundles[0].source_encoded_dir, "my-encoded-proj")


# ---------------------------------------------------------------------------
# _build_rewriters
# ---------------------------------------------------------------------------


class BuildRewritersTests(unittest.TestCase):
    """Compiling path-rewrite regex patterns from mappings."""

    def test_single_mapping_produces_two_patterns(self) -> None:
        """Each mapping produces a backslash pattern and a forward-slash pattern."""
        rewriters = _build_rewriters([("c:\\Users\\m\\test", "/home/m/test")])
        self.assertEqual(len(rewriters), 2)

    def test_multiple_mappings_sorted_longest_first(self) -> None:
        """Longer from_paths appear before shorter ones."""
        rewriters = _build_rewriters([
            ("c:\\a", "/x"),
            ("c:\\a\\b\\c", "/x/b/c"),
        ])
        # 4 patterns total: 2 per mapping
        self.assertEqual(len(rewriters), 4)
        # The first two should be for the longer path.
        # The forward-slash pattern (index 1) should match the longer path.
        fs_pattern = rewriters[1][0]
        self.assertIsNotNone(fs_pattern.search("c:/a/b/c"))

    def test_drive_letter_case_insensitive(self) -> None:
        """Both C: and c: match the generated forward-slash pattern."""
        rewriters = _build_rewriters([("c:\\Users", "/home")])
        fs_pattern = rewriters[1][0]
        self.assertIsNotNone(fs_pattern.search("c:/Users"))
        self.assertIsNotNone(fs_pattern.search("C:/Users"))

    def test_linux_mapping_produces_patterns(self) -> None:
        """A Linux-to-Linux mapping works (no drive letter)."""
        rewriters = _build_rewriters([("/old/path", "/new/path")])
        self.assertEqual(len(rewriters), 2)

    def test_backslash_pattern_matches_json_escaped_text(self) -> None:
        """The backslash regex pattern matches JSON-escaped double-backslash text."""
        rewriters = _build_rewriters([("c:\\Users\\m", "/home/m")])
        bs_pattern = rewriters[0][0]
        # In JSON, c:\Users\m is stored as c:\\Users\\m (double backslashes).
        # The regex must match this JSON-escaped form.
        self.assertIsNotNone(bs_pattern.search("c:\\\\Users\\\\m"))


# ---------------------------------------------------------------------------
# _apply_rewrites
# ---------------------------------------------------------------------------


class ApplyRewritesTests(unittest.TestCase):
    """Path rewriting on individual lines of JSON text."""

    def _rewrite(
        self,
        line: str,
        mappings: list[tuple[str, str]],
    ) -> tuple[str, bool]:
        rewriters = _build_rewriters(mappings)
        return _apply_rewrites(line, rewriters)

    def test_forward_slash_windows_to_linux(self) -> None:
        """A forward-slash Windows path in JSON is rewritten to a Linux path."""
        line = json.dumps({"cwd": "c:/Users/m/test"})

        result, changed = self._rewrite(
            line, [("c:\\Users\\m\\test", "/home/m/test")],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        self.assertEqual(parsed["cwd"], "/home/m/test")

    def test_deeper_path_beyond_mapped_root_forward_slash(self) -> None:
        """A deeper forward-slash path rewrites including the suffix."""
        line = json.dumps({"file_path": "c:/Users/m/test/src/app.js"})

        result, changed = self._rewrite(
            line, [("c:\\Users\\m\\test", "/home/m/test")],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        self.assertEqual(parsed["file_path"], "/home/m/test/src/app.js")

    def test_backslash_pattern_rewrites_json_escaped_text(self) -> None:
        r"""The backslash regex matches JSON-escaped c:\\Users\\m (double backslashes)."""
        # json.dumps produces double backslashes for Windows paths:
        # {"cwd": "c:\\Users\\m\\test"} in the JSON text.
        line = json.dumps({"cwd": "c:\\Users\\m\\test"})

        result, changed = self._rewrite(
            line, [("c:\\Users\\m\\test", "/home/m/test")],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        self.assertEqual(parsed["cwd"], "/home/m/test")

    def test_backslash_pattern_rewrites_deeper_path(self) -> None:
        r"""Backslash pattern rewrites deeper paths with suffix preserved."""
        # c:\Users\m\test\src\app.js in JSON becomes c:\\Users\\m\\test\\src\\app.js
        line = json.dumps({"file_path": "c:\\Users\\m\\test\\src\\app.js"})

        result, changed = self._rewrite(
            line, [("c:\\Users\\m\\test", "/home/m/test")],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        self.assertEqual(parsed["file_path"], "/home/m/test/src/app.js")

    def test_case_insensitive_drive_letter(self) -> None:
        """Both C:/ and c:/ are rewritten via the forward-slash pattern."""
        for drive in ("c", "C"):
            line = json.dumps({"cwd": f"{drive}:/Users/m/test"})

            result, changed = self._rewrite(
                line, [("c:\\Users\\m\\test", "/home/m/test")],
            )

            self.assertTrue(changed, f"failed for drive letter '{drive}'")
            parsed = json.loads(result)
            self.assertEqual(parsed["cwd"], "/home/m/test")

    def test_longest_prefix_first(self) -> None:
        """Longer prefix matches before shorter one can partially consume it."""
        line = json.dumps({"cwd": "c:/a/b/c/file.txt"})

        result, changed = self._rewrite(
            line,
            [
                ("c:\\a\\b", "/x"),
                ("c:\\a\\b\\c", "/y"),
            ],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        # The longer mapping c:\a\b\c -> /y should match first
        self.assertEqual(parsed["cwd"], "/y/file.txt")

    def test_non_matching_content_preserved(self) -> None:
        """Lines without matching paths pass through unchanged."""
        line = '{"cwd": "/home/linux/path", "msg": "hello"}'

        result, changed = self._rewrite(
            line, [("c:\\Users\\m\\test", "/home/m/test")],
        )

        self.assertFalse(changed)
        self.assertEqual(result, line)

    def test_output_is_valid_json(self) -> None:
        """Rewritten output parses as valid JSON."""
        obj = {
            "cwd": "c:/Users/m/test",
            "file": "c:/Users/m/test/README.md",
            "msg": "test message",
        }
        line = json.dumps(obj)

        result, _ = self._rewrite(
            line, [("c:\\Users\\m\\test", "/home/m/test")],
        )

        parsed = json.loads(result)
        self.assertEqual(parsed["cwd"], "/home/m/test")
        self.assertEqual(parsed["file"], "/home/m/test/README.md")
        self.assertEqual(parsed["msg"], "test message")

    def test_multiple_fields_rewritten(self) -> None:
        """Multiple path fields in the same line are all rewritten."""
        obj = {
            "cwd": "c:/Users/m/proj",
            "file_path": "c:/Users/m/proj/src/main.py",
        }
        line = json.dumps(obj)

        result, changed = self._rewrite(
            line, [("c:\\Users\\m\\proj", "/home/m/proj")],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        self.assertEqual(parsed["cwd"], "/home/m/proj")
        self.assertEqual(parsed["file_path"], "/home/m/proj/src/main.py")

    def test_linux_to_linux_rewrite(self) -> None:
        """A pure Linux path mapping rewrites correctly."""
        line = json.dumps({"cwd": "/old/path/subdir"})

        result, changed = self._rewrite(
            line, [("/old/path", "/new/path")],
        )

        self.assertTrue(changed)
        parsed = json.loads(result)
        self.assertEqual(parsed["cwd"], "/new/path/subdir")


# ---------------------------------------------------------------------------
# _rewrite_jsonl
# ---------------------------------------------------------------------------


class RewriteJsonlTests(unittest.TestCase):
    """Atomic JSONL file rewriting with path substitution and UUID reid."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_rewrites_paths_to_target(self) -> None:
        """Source is read, paths rewritten, output written to target."""
        src = self.root / "src.jsonl"
        # Use forward-slash Windows paths (matched by the forward-slash pattern)
        src.write_text(_make_session_jsonl("c:/Users/m/test", UUID_A))
        dst = self.root / "out" / "dst.jsonl"

        rewriters = _build_rewriters([("c:\\Users\\m\\test", "/home/m/test")])
        lines = _rewrite_jsonl(src, dst, rewriters, None, None, dry_run=False)

        self.assertGreater(lines, 0)
        self.assertTrue(dst.exists())
        for raw_line in dst.read_text().splitlines():
            parsed = json.loads(raw_line)
            if "cwd" in parsed:
                self.assertEqual(parsed["cwd"], "/home/m/test")

    def test_atomic_write_no_tmp_left(self) -> None:
        """After write, target exists and .tmp file does not."""
        src = self.root / "src.jsonl"
        src.write_text(_make_jsonl_line(cwd="/a") + "\n")
        dst = self.root / "dst.jsonl"

        rewriters = _build_rewriters([("/a", "/b")])
        _rewrite_jsonl(src, dst, rewriters, None, None, dry_run=False)

        self.assertTrue(dst.exists())
        self.assertFalse(dst.with_suffix(".tmp").exists())

    def test_dry_run_does_not_create_target(self) -> None:
        """Dry run returns line count but creates no file."""
        src = self.root / "src.jsonl"
        src.write_text(_make_session_jsonl("c:/Users/m/test", UUID_A))
        dst = self.root / "out" / "dst.jsonl"

        rewriters = _build_rewriters([("c:\\Users\\m\\test", "/home/m/test")])
        lines = _rewrite_jsonl(src, dst, rewriters, None, None, dry_run=True)

        self.assertGreater(lines, 0)
        self.assertFalse(dst.exists())

    def test_reid_replaces_session_id(self) -> None:
        """When old_uuid and new_uuid differ, sessionId is replaced."""
        src = self.root / "src.jsonl"
        src.write_text(_make_session_jsonl("c:/Users/m/test", UUID_A))
        dst = self.root / "dst.jsonl"

        rewriters = _build_rewriters([("c:\\Users\\m\\test", "/home/m/test")])
        _rewrite_jsonl(src, dst, rewriters, UUID_A, UUID_B, dry_run=False)

        for raw_line in dst.read_text().splitlines():
            parsed = json.loads(raw_line)
            if "sessionId" in parsed:
                self.assertEqual(parsed["sessionId"], UUID_B)
                self.assertNotEqual(parsed["sessionId"], UUID_A)

    def test_no_reid_preserves_session_id(self) -> None:
        """When old_uuid is None, sessionId is preserved."""
        src = self.root / "src.jsonl"
        src.write_text(_make_session_jsonl("/test", UUID_A))
        dst = self.root / "dst.jsonl"

        rewriters = _build_rewriters([("/test", "/test2")])
        _rewrite_jsonl(src, dst, rewriters, None, None, dry_run=False)

        for raw_line in dst.read_text().splitlines():
            parsed = json.loads(raw_line)
            if "sessionId" in parsed:
                self.assertEqual(parsed["sessionId"], UUID_A)

    def test_unreadable_source_returns_zero(self) -> None:
        """When source is unreadable, returns 0 lines."""
        src = self.root / "nonexistent.jsonl"
        dst = self.root / "dst.jsonl"

        rewriters = _build_rewriters([("/a", "/b")])
        lines = _rewrite_jsonl(src, dst, rewriters, None, None, dry_run=False)

        self.assertEqual(lines, 0)

    def test_creates_parent_directories(self) -> None:
        """Target parent directories are created automatically."""
        src = self.root / "src.jsonl"
        src.write_text(_make_jsonl_line(cwd="/a") + "\n")
        dst = self.root / "deep" / "nested" / "dst.jsonl"

        rewriters = _build_rewriters([("/a", "/b")])
        _rewrite_jsonl(src, dst, rewriters, None, None, dry_run=False)

        self.assertTrue(dst.exists())

    def test_reid_with_spaces_around_colon(self) -> None:
        """Reid handles sessionId with spaces around the colon."""
        src = self.root / "src.jsonl"
        # Write a line with "sessionId": "uuid" (space after colon)
        src.write_text(
            json.dumps({"sessionId": UUID_A, "type": "user"}) + "\n"
        )
        dst = self.root / "dst.jsonl"

        rewriters = _build_rewriters([])
        _rewrite_jsonl(src, dst, rewriters, UUID_A, UUID_B, dry_run=False)

        parsed = json.loads(dst.read_text().strip())
        self.assertEqual(parsed["sessionId"], UUID_B)


# ---------------------------------------------------------------------------
# Collision detection in run_import
# ---------------------------------------------------------------------------


class CollisionTests(unittest.TestCase):
    """UUID collision detection and reid behavior."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Source directory
        self.source = self.root / "source"
        self.source_projects = self.source / "projects" / "proj"
        self.source_projects.mkdir(parents=True)

        # Shared store
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.shared_projects = self.shared / "projects"
        self.shared_projects.mkdir()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def _write_source_session(self, uuid: str, cwd: str = "/test") -> None:
        """Create a source JSONL with the given UUID."""
        (self.source_projects / f"{uuid}.jsonl").write_text(
            _make_session_jsonl(cwd, uuid)
        )

    def test_no_collisions_import_succeeds(self) -> None:
        """When no collisions exist, sessions are imported."""
        self._write_source_session(UUID_A)

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
            )

        self.assertEqual(result.sessions_imported, 1)
        self.assertEqual(result.collisions, [])

    def test_collision_without_reid_returns_early(self) -> None:
        """Collision without reid returns with collision list, 0 imported."""
        self._write_source_session(UUID_A)
        # Pre-create colliding file in shared store
        target_dir = self.shared_projects / encode_path("/local/test")
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID_A}.jsonl").write_text("{}\n")

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=False,
            )

        self.assertGreater(len(result.collisions), 0)
        self.assertEqual(result.sessions_imported, 0)

    def test_collision_with_reid_assigns_new_uuid(self) -> None:
        """Collision with reid generates a new UUID and imports."""
        self._write_source_session(UUID_A)
        # Pre-create colliding file
        target_dir = self.shared_projects / encode_path("/local/test")
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID_A}.jsonl").write_text("{}\n")

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        self.assertEqual(result.sessions_imported, 1)
        self.assertEqual(result.sessions_reided, 1)
        # The original collision file should still exist
        self.assertTrue((target_dir / f"{UUID_A}.jsonl").exists())
        # A new file with a different UUID should exist
        jsonl_files = list(target_dir.glob("*.jsonl"))
        self.assertEqual(len(jsonl_files), 2)

    def test_companion_dir_collision_detected(self) -> None:
        """A collision on the companion directory is also detected."""
        self._write_source_session(UUID_A)
        # Create colliding companion dir in shared store
        target_dir = self.shared_projects / encode_path("/local/test")
        target_dir.mkdir(parents=True)
        (target_dir / UUID_A).mkdir()  # companion dir collision

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=False,
            )

        self.assertGreater(len(result.collisions), 0)


# ---------------------------------------------------------------------------
# Mapping validation
# ---------------------------------------------------------------------------


class MappingValidationTests(unittest.TestCase):
    """Validation that all discovered cwds have a mapping."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        self.source = self.root / "source"
        (self.source / "projects" / "proj").mkdir(parents=True)

        self.shared = self.root / "shared"
        self.shared.mkdir()
        (self.shared / "projects").mkdir()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_all_cwds_mapped_succeeds(self) -> None:
        """Import succeeds when all cwds have a mapping."""
        (self.source / "projects" / "proj" / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("/test", UUID_A)
        )

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
            )

        self.assertEqual(result.sessions_imported, 1)

    def test_unmapped_cwd_raises(self) -> None:
        """A cwd with no mapping raises ValueError."""
        (self.source / "projects" / "proj" / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("/unmapped", UUID_A)
        )

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/unmapped"):
            with self.assertRaises(ValueError) as ctx:
                run_import(
                    str(self.source),
                    mappings=[("/other", "/local/other")],
                )

        self.assertIn("unmapped", str(ctx.exception).lower())

    def test_multiple_from_to_same_to_allowed(self) -> None:
        """Multiple from-paths mapping to the same to-path is allowed."""
        enc_a = self.source / "projects" / "proj-a"
        enc_a.mkdir()
        (enc_a / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("/path-a", UUID_A)
        )
        enc_b = self.source / "projects" / "proj-b"
        enc_b.mkdir()
        (enc_b / f"{UUID_B}.jsonl").write_text(
            _make_session_jsonl("/path-b", UUID_B)
        )

        def mock_cwd(path, **kwargs):
            if UUID_A in str(path):
                return "/path-a"
            return "/path-b"

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", side_effect=mock_cwd):
            result = run_import(
                str(self.source),
                mappings=[
                    ("/path-a", "/local/merged"),
                    ("/path-b", "/local/merged"),
                ],
            )

        self.assertEqual(result.sessions_imported, 2)

    def test_normalization_applied_to_mappings(self) -> None:
        """Mapping from-paths are normalized (drive letter case, trailing slash)."""
        (self.source / "projects" / "proj" / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("C:\\Users\\m\\", UUID_A)
        )

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="C:\\Users\\m\\"):
            # Mapping uses lowercase drive letter without trailing slash
            result = run_import(
                str(self.source),
                mappings=[("c:\\Users\\m", "/home/m")],
            )

        self.assertEqual(result.sessions_imported, 1)

    def test_missing_projects_dir_raises(self) -> None:
        """Source without projects/ raises FileNotFoundError."""
        no_projects = self.root / "empty-source"
        no_projects.mkdir()

        with patch("claudewheel.import_.SHARED_DIR", self.shared):
            with self.assertRaises(FileNotFoundError):
                run_import(str(no_projects), mappings=[])


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class IntegrationTests(unittest.TestCase):
    """Full end-to-end import with fake source and shared store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Source directory structure (using forward-slash Windows paths)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source_proj = self.source / "projects" / "proj"
        self.source_proj.mkdir(parents=True)

        # Main session JSONL with forward-slash cwds
        self.source_proj_jsonl = self.source_proj / f"{UUID_A}.jsonl"
        self.source_proj_jsonl.write_text(
            _make_session_jsonl("c:/Users/m/test", UUID_A)
        )

        # Companion directory with agent file
        self.companion = self.source_proj / UUID_A
        self.companion.mkdir()
        agent_jsonl = self.companion / "agent.jsonl"
        agent_jsonl.write_text(
            _make_jsonl_line(
                cwd="c:/Users/m/test", sessionId=UUID_A, type="assistant",
            )
            + "\n"
        )

        # Non-JSONL artifact in companion
        (self.companion / "metadata.json").write_text('{"key": "value"}')

        # Simple dir artifacts
        todos = self.source / "todos"
        todos.mkdir()
        (todos / f"{UUID_A}-agent-cleanup.json").write_text('{"task": "clean"}')

        session_env = self.source / "session-env" / UUID_A
        session_env.mkdir(parents=True)
        (session_env / "env.txt").write_text("FOO=bar")

        # Paste cache
        paste = self.source / "paste-cache"
        paste.mkdir()
        (paste / "hash1.txt").write_text("pasted content")

        # Shared store
        self.shared = self.root / "shared"
        self.shared.mkdir()
        (self.shared / "projects").mkdir()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_full_import(self) -> None:
        """Complete import: JSONL rewritten, companions copied, artifacts moved."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch(
                 "claudewheel.import_.get_session_cwd",
                 return_value="c:/Users/m/test",
             ):
            result = run_import(
                str(self.source),
                mappings=[("c:/Users/m/test", "/home/m/test")],
            )

        self.assertEqual(result.sessions_imported, 1)
        self.assertGreater(result.lines_rewritten, 0)
        self.assertGreater(result.artifacts_copied, 0)
        self.assertEqual(result.paste_files_copied, 1)

        # Verify target location
        target_dir = self.shared / "projects" / encode_path("/home/m/test")
        target_jsonl = target_dir / f"{UUID_A}.jsonl"
        self.assertTrue(target_jsonl.exists())

        # Verify content has Linux paths
        for raw_line in target_jsonl.read_text().splitlines():
            parsed = json.loads(raw_line)
            if "cwd" in parsed:
                self.assertEqual(parsed["cwd"], "/home/m/test")

        # Verify companion directory
        target_companion = target_dir / UUID_A
        self.assertTrue(target_companion.is_dir())
        agent_content = (target_companion / "agent.jsonl").read_text()
        agent_parsed = json.loads(agent_content.strip())
        self.assertEqual(agent_parsed["cwd"], "/home/m/test")

        # Verify non-JSONL artifact copied
        self.assertTrue((target_companion / "metadata.json").exists())

        # Verify todos artifact
        todos_dst = self.shared / "todos" / f"{UUID_A}-agent-cleanup.json"
        self.assertTrue(todos_dst.exists())

        # Verify session-env artifact
        env_dst = self.shared / "session-env" / UUID_A
        self.assertTrue(env_dst.exists())

        # Verify paste cache
        paste_dst = self.shared / "paste-cache" / "hash1.txt"
        self.assertTrue(paste_dst.exists())

    def test_structure_preserved(self) -> None:
        """The session bundle directory structure is preserved in the target."""
        subdir = self.companion / "subagents"
        subdir.mkdir()
        (subdir / "sub.jsonl").write_text(
            _make_jsonl_line(cwd="c:/Users/m/test", sessionId=UUID_A) + "\n"
        )

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch(
                 "claudewheel.import_.get_session_cwd",
                 return_value="c:/Users/m/test",
             ):
            run_import(
                str(self.source),
                mappings=[("c:/Users/m/test", "/home/m/test")],
            )

        target_dir = self.shared / "projects" / encode_path("/home/m/test")
        sub_jsonl = target_dir / UUID_A / "subagents" / "sub.jsonl"
        self.assertTrue(sub_jsonl.exists())
        parsed = json.loads(sub_jsonl.read_text().strip())
        self.assertEqual(parsed["cwd"], "/home/m/test")

    def test_linux_to_linux_import(self) -> None:
        """A pure Linux-path source imports correctly."""
        # Override source data with Linux paths
        self.source_proj_jsonl.write_text(
            _make_session_jsonl("/old/project", UUID_A)
        )

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch(
                 "claudewheel.import_.get_session_cwd",
                 return_value="/old/project",
             ):
            result = run_import(
                str(self.source),
                mappings=[("/old/project", "/new/project")],
            )

        self.assertEqual(result.sessions_imported, 1)
        target_dir = self.shared / "projects" / encode_path("/new/project")
        target_jsonl = target_dir / f"{UUID_A}.jsonl"
        self.assertTrue(target_jsonl.exists())
        for raw_line in target_jsonl.read_text().splitlines():
            parsed = json.loads(raw_line)
            if "cwd" in parsed:
                self.assertEqual(parsed["cwd"], "/new/project")

    def test_empty_source_returns_zero(self) -> None:
        """An empty projects/ dir results in 0 sessions imported."""
        empty_source = self.root / "empty"
        (empty_source / "projects" / "proj").mkdir(parents=True)

        with patch("claudewheel.import_.SHARED_DIR", self.shared):
            result = run_import(
                str(empty_source),
                mappings=[("/test", "/test2")],
            )

        self.assertEqual(result.sessions_imported, 0)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class DryRunTests(unittest.TestCase):
    """Dry run reports counts but creates no files."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        self.source = self.root / "source"
        proj = self.source / "projects" / "proj"
        proj.mkdir(parents=True)
        (proj / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("c:/Users/m/test", UUID_A)
        )

        # Companion dir
        comp = proj / UUID_A
        comp.mkdir()
        (comp / "agent.jsonl").write_text(
            _make_jsonl_line(cwd="c:/Users/m/test") + "\n"
        )

        # Paste cache
        paste = self.source / "paste-cache"
        paste.mkdir()
        (paste / "hash1.txt").write_text("pasted")

        self.shared = self.root / "shared"
        self.shared.mkdir()
        (self.shared / "projects").mkdir()

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_dry_run_counts_correct(self) -> None:
        """Dry run reports the expected session and artifact counts."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch(
                 "claudewheel.import_.get_session_cwd",
                 return_value="c:/Users/m/test",
             ):
            result = run_import(
                str(self.source),
                mappings=[("c:/Users/m/test", "/home/m/test")],
                dry_run=True,
            )

        self.assertEqual(result.sessions_imported, 1)
        self.assertGreater(result.lines_rewritten, 0)
        self.assertGreater(result.artifacts_copied, 0)
        self.assertEqual(result.paste_files_copied, 1)

    def test_dry_run_no_files_created(self) -> None:
        """Dry run does not create any files in the shared store."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch(
                 "claudewheel.import_.get_session_cwd",
                 return_value="c:/Users/m/test",
             ):
            run_import(
                str(self.source),
                mappings=[("c:/Users/m/test", "/home/m/test")],
                dry_run=True,
            )

        # Shared projects should have no subdirectories created
        target_dir = self.shared / "projects" / encode_path("/home/m/test")
        self.assertFalse(target_dir.exists())
        # No paste cache in shared
        self.assertFalse((self.shared / "paste-cache").exists())

    def test_dry_run_collision_without_reid(self) -> None:
        """Dry run with collisions still returns collision list."""
        proj = self.source / "projects" / "proj"
        (proj / f"{UUID_B}.jsonl").write_text(
            _make_session_jsonl("/test2", UUID_B)
        )
        # Create collision in shared store
        target_dir = self.shared / "projects" / encode_path("/local/test2")
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID_B}.jsonl").write_text("{}\n")

        def mock_cwd(path, **kwargs):
            if UUID_A in str(path):
                return "c:/Users/m/test"
            return "/test2"

        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", side_effect=mock_cwd):
            result = run_import(
                str(self.source),
                mappings=[
                    ("c:/Users/m/test", "/home/m/test"),
                    ("/test2", "/local/test2"),
                ],
                dry_run=True,
                reid=False,
            )

        # Collision detected, early return
        self.assertGreater(len(result.collisions), 0)


# ---------------------------------------------------------------------------
# ImportResult dataclass
# ---------------------------------------------------------------------------


class ImportResultTests(unittest.TestCase):
    """ImportResult default values and field types."""

    def test_defaults(self) -> None:
        """All counters default to zero, collisions to empty list."""
        r = ImportResult()
        self.assertEqual(r.sessions_imported, 0)
        self.assertEqual(r.sessions_reided, 0)
        self.assertEqual(r.artifacts_copied, 0)
        self.assertEqual(r.lines_rewritten, 0)
        self.assertEqual(r.paste_files_copied, 0)
        self.assertEqual(r.collisions, [])

    def test_collisions_are_independent(self) -> None:
        """Each ImportResult has an independent collisions list."""
        r1 = ImportResult()
        r2 = ImportResult()
        r1.collisions.append("test")
        self.assertEqual(r2.collisions, [])


# ---------------------------------------------------------------------------
# Reid through companion directories
# ---------------------------------------------------------------------------


class ReidCompanionDirTests(unittest.TestCase):
    """Reid copies companion dirs under the new UUID and rewrites agent JSONL inside."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Source
        self.source = self.root / "source"
        self.source_proj = self.source / "projects" / "proj"
        self.source_proj.mkdir(parents=True)

        # Main session JSONL
        (self.source_proj / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("/test", UUID_A)
        )

        # Companion directory with subagents/ containing an agent JSONL
        companion = self.source_proj / UUID_A
        subagents = companion / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent.jsonl").write_text(
            _make_jsonl_line(cwd="/test", sessionId=UUID_A, type="assistant") + "\n"
        )

        # Shared store with pre-existing collision
        self.shared = self.root / "shared"
        (self.shared / "projects").mkdir(parents=True)
        target_dir = self.shared / "projects" / encode_path("/local/test")
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID_A}.jsonl").write_text("{}\n")

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_companion_dir_copied_under_new_uuid(self) -> None:
        """Companion dir uses the new UUID, not the old one."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        self.assertEqual(result.sessions_imported, 1)
        self.assertEqual(result.sessions_reided, 1)

        target_dir = self.shared / "projects" / encode_path("/local/test")
        # The old collision file should still exist
        self.assertTrue((target_dir / f"{UUID_A}.jsonl").exists())
        # Find the new UUID -- should be a JSONL file that is NOT UUID_A
        new_jsonl_files = [
            f for f in target_dir.glob("*.jsonl")
            if f.stem != UUID_A
        ]
        self.assertEqual(len(new_jsonl_files), 1)
        new_uuid = new_jsonl_files[0].stem

        # Companion dir should be under the new UUID, not the old one
        new_companion = target_dir / new_uuid
        self.assertTrue(new_companion.is_dir())
        # Old UUID companion should NOT have been created by the import
        # (there is no source companion for UUID_A pre-existing in target)

    def test_agent_jsonl_session_id_rewritten(self) -> None:
        """Agent JSONL inside the companion dir has the new sessionId."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        target_dir = self.shared / "projects" / encode_path("/local/test")
        new_jsonl_files = [
            f for f in target_dir.glob("*.jsonl")
            if f.stem != UUID_A
        ]
        new_uuid = new_jsonl_files[0].stem

        agent_jsonl = target_dir / new_uuid / "subagents" / "agent.jsonl"
        self.assertTrue(agent_jsonl.exists())
        parsed = json.loads(agent_jsonl.read_text().strip())
        self.assertEqual(parsed["sessionId"], new_uuid)
        self.assertNotEqual(parsed["sessionId"], UUID_A)

    def test_main_jsonl_has_new_uuid_filename_and_session_id(self) -> None:
        """The session JSONL file has the new UUID as filename and updated sessionId."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        target_dir = self.shared / "projects" / encode_path("/local/test")
        new_jsonl_files = [
            f for f in target_dir.glob("*.jsonl")
            if f.stem != UUID_A
        ]
        self.assertEqual(len(new_jsonl_files), 1)
        new_uuid = new_jsonl_files[0].stem

        # Verify every sessionId in the file is the new UUID
        for raw_line in new_jsonl_files[0].read_text().splitlines():
            parsed = json.loads(raw_line)
            if "sessionId" in parsed:
                self.assertEqual(parsed["sessionId"], new_uuid)


# ---------------------------------------------------------------------------
# Reid renaming simple artifacts
# ---------------------------------------------------------------------------


class ReidSimpleArtifactsTests(unittest.TestCase):
    """Reid renames todos files and session-env directories to the new UUID."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Source
        self.source = self.root / "source"
        proj = self.source / "projects" / "proj"
        proj.mkdir(parents=True)
        (proj / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("/test", UUID_A)
        )

        # Todos file: <uuid>-agent-<uuid>.json (both positions use the same UUID)
        todos = self.source / "todos"
        todos.mkdir()
        (todos / f"{UUID_A}-agent-{UUID_A}.json").write_text('{"task":"clean"}')

        # Session-env directory named after the UUID
        session_env = self.source / "session-env" / UUID_A
        session_env.mkdir(parents=True)
        (session_env / "env.txt").write_text("FOO=bar")

        # Shared store with collision
        self.shared = self.root / "shared"
        (self.shared / "projects").mkdir(parents=True)
        target_dir = self.shared / "projects" / encode_path("/local/test")
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID_A}.jsonl").write_text("{}\n")

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_todos_file_renamed_with_new_uuid(self) -> None:
        """Todos file has both UUID positions replaced with the new UUID."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        self.assertEqual(result.sessions_reided, 1)

        # Find the new UUID from the imported JSONL
        target_dir = self.shared / "projects" / encode_path("/local/test")
        new_uuid = [
            f.stem for f in target_dir.glob("*.jsonl")
            if f.stem != UUID_A
        ][0]

        # The todos file should use the new UUID in both positions
        expected_name = f"{new_uuid}-agent-{new_uuid}.json"
        todos_dst = self.shared / "todos" / expected_name
        self.assertTrue(
            todos_dst.exists(),
            f"expected {expected_name} in {self.shared / 'todos'}, "
            f"found: {list((self.shared / 'todos').iterdir()) if (self.shared / 'todos').exists() else 'dir missing'}",
        )

    def test_session_env_dir_renamed_to_new_uuid(self) -> None:
        """Session-env directory is renamed to the new UUID."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        target_dir = self.shared / "projects" / encode_path("/local/test")
        new_uuid = [
            f.stem for f in target_dir.glob("*.jsonl")
            if f.stem != UUID_A
        ][0]

        # The session-env dir should use the new UUID
        env_dst = self.shared / "session-env" / new_uuid
        self.assertTrue(
            env_dst.exists(),
            f"expected session-env/{new_uuid}, "
            f"found: {list((self.shared / 'session-env').iterdir()) if (self.shared / 'session-env').exists() else 'dir missing'}",
        )
        # Old UUID should NOT exist (it was renamed)
        old_env = self.shared / "session-env" / UUID_A
        self.assertFalse(old_env.exists())


# ---------------------------------------------------------------------------
# Paste-cache dedup
# ---------------------------------------------------------------------------


class PasteCacheDedupTests(unittest.TestCase):
    """Paste-cache import skips pre-existing files and counts only new ones."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Source with two paste-cache files
        self.source = self.root / "source"
        proj = self.source / "projects" / "proj"
        proj.mkdir(parents=True)
        (proj / f"{UUID_C}.jsonl").write_text(
            _make_session_jsonl("/test", UUID_C)
        )
        paste_src = self.source / "paste-cache"
        paste_src.mkdir()
        (paste_src / "abcdef123456.txt").write_text("duplicate content")
        (paste_src / "newfile789abc.txt").write_text("new content")

        # Shared store with pre-existing paste-cache file
        self.shared = self.root / "shared"
        (self.shared / "projects").mkdir(parents=True)
        paste_dst = self.shared / "paste-cache"
        paste_dst.mkdir(parents=True)
        (paste_dst / "abcdef123456.txt").write_text("original content")

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_paste_files_copied_counts_only_new(self) -> None:
        """paste_files_copied reflects only newly copied files."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
            )

        # Only the new file should be counted
        self.assertEqual(result.paste_files_copied, 1)

    def test_preexisting_paste_file_not_overwritten(self) -> None:
        """The pre-existing paste-cache file retains its original content."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
            )

        existing = self.shared / "paste-cache" / "abcdef123456.txt"
        self.assertEqual(existing.read_text(), "original content")


# ---------------------------------------------------------------------------
# Empty JSONL skip
# ---------------------------------------------------------------------------


class EmptyJsonlSkipTests(unittest.TestCase):
    """Zero-byte JSONL files are skipped by _scan_source without error."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_jsonl_skipped(self) -> None:
        """A 0-byte JSONL file is not included in the bundles."""
        enc = self.projects / "proj"
        enc.mkdir()
        # Create a 0-byte file with a valid UUID name
        (enc / f"{UUID_A}.jsonl").write_text("")
        # Create a non-empty file for comparison
        (enc / f"{UUID_B}.jsonl").write_text(
            _make_jsonl_line(cwd="/test") + "\n"
        )

        with patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            bundles = _scan_source(self.root)

        # Only the non-empty file should produce a bundle
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].uuid, UUID_B)

    def test_all_empty_returns_no_bundles(self) -> None:
        """If every JSONL file is 0-byte, no bundles are returned."""
        enc = self.projects / "proj"
        enc.mkdir()
        (enc / f"{UUID_A}.jsonl").write_text("")
        (enc / f"{UUID_B}.jsonl").write_text("")

        with patch("claudewheel.import_.get_session_cwd"):
            bundles = _scan_source(self.root)

        self.assertEqual(len(bundles), 0)


# ---------------------------------------------------------------------------
# Non-JSONL relative path rewrite during reid
# ---------------------------------------------------------------------------


class ReidNonJsonlPathTests(unittest.TestCase):
    """Non-JSONL files with UUID in their relative path are copied under the new UUID."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._stdout_trap = contextlib.redirect_stdout(io.StringIO())
        self._stdout_trap.__enter__()

        # Source
        self.source = self.root / "source"
        proj = self.source / "projects" / "proj"
        proj.mkdir(parents=True)
        (proj / f"{UUID_A}.jsonl").write_text(
            _make_session_jsonl("/test", UUID_A)
        )

        # Companion dir with nested non-JSONL file containing UUID in path
        # e.g. <uuid>/tool-results/<uuid>/output.txt
        companion = proj / UUID_A
        nested = companion / "tool-results" / UUID_A
        nested.mkdir(parents=True)
        (nested / "output.txt").write_text("some tool output")

        # Shared store with collision
        self.shared = self.root / "shared"
        (self.shared / "projects").mkdir(parents=True)
        target_dir = self.shared / "projects" / encode_path("/local/test")
        target_dir.mkdir(parents=True)
        (target_dir / f"{UUID_A}.jsonl").write_text("{}\n")

    def tearDown(self) -> None:
        self._stdout_trap.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_non_jsonl_copied_under_new_uuid_path(self) -> None:
        """Non-JSONL file with UUID in relative path gets the UUID replaced."""
        with patch("claudewheel.import_.SHARED_DIR", self.shared), \
             patch("claudewheel.import_.get_session_cwd", return_value="/test"):
            result = run_import(
                str(self.source),
                mappings=[("/test", "/local/test")],
                reid=True,
            )

        self.assertEqual(result.sessions_reided, 1)

        target_dir = self.shared / "projects" / encode_path("/local/test")
        new_uuid = [
            f.stem for f in target_dir.glob("*.jsonl")
            if f.stem != UUID_A
        ][0]

        # The file should be under new_uuid/tool-results/new_uuid/output.txt
        expected = target_dir / new_uuid / "tool-results" / new_uuid / "output.txt"
        self.assertTrue(
            expected.exists(),
            f"expected {expected.relative_to(target_dir)}, "
            f"found: {list(target_dir.rglob('output.txt'))}",
        )
        self.assertEqual(expected.read_text(), "some tool output")


if __name__ == "__main__":
    unittest.main()
