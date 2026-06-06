"""Tests for profile_ops.py (--delete-profile implementation)."""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claudewheel import profile_ops


def _xattr_supported() -> bool:
    """Return True if user xattrs work in tempfile dirs."""
    try:
        with tempfile.NamedTemporaryFile() as f:
            os.setxattr(f.name, "user.test", b"1")
        return True
    except OSError:
        return False


HAVE_XATTR = _xattr_supported()


class _ProfileOpsTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as home and patches paths."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher_home = patch.object(Path, "home", return_value=self.home)
        self._patcher_home.start()

        # Set up .claudewheel/ for options and tokens
        self.launcher_dir = self.home / ".claudewheel"
        self.launcher_dir.mkdir()
        self.options_file = self.launcher_dir / "options.json"
        self.tokens_file = self.launcher_dir / "tokens.json"

        self._patcher_opts = patch.object(profile_ops, "OPTIONS_FILE", self.options_file)
        self._patcher_tokens = patch.object(profile_ops, "TOKENS_FILE", self.tokens_file)
        self._patcher_origins = patch.object(
            profile_ops, "ORIGINS_FILE",
            self.home / ".claude-common" / "profile-origins.jsonl",
        )
        self._patcher_shared = patch.object(
            profile_ops, "SHARED_DIR", self.home / ".claude-shared",
        )
        self._patcher_opts.start()
        self._patcher_tokens.start()
        self._patcher_origins.start()
        self._patcher_shared.start()

    def tearDown(self) -> None:
        self._patcher_shared.stop()
        self._patcher_origins.stop()
        self._patcher_tokens.stop()
        self._patcher_opts.stop()
        self._patcher_home.stop()
        self._tmp.cleanup()

    def _write_options(self, profile_values: list[str],
                       metadata: dict | None = None) -> None:
        opts = {"profile": {"values": profile_values}}
        if metadata:
            opts["profile"]["metadata"] = metadata
        self.options_file.write_text(json.dumps(opts, indent=2) + "\n")

    def _write_tokens(self, tokens: dict) -> None:
        self.tokens_file.write_text(json.dumps(tokens, indent=2) + "\n")

    def _make_profile_dir(self, name: str) -> Path:
        pdir = self.home / f".claude-{name}"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        (pdir / "settings.json").write_text("{}")
        return pdir

    def _make_origins_file(self, lines: list[dict]) -> None:
        common = self.home / ".claude-common"
        common.mkdir(parents=True, exist_ok=True)
        origins = common / "profile-origins.jsonl"
        origins.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class DeleteProfileValidationTests(_ProfileOpsTestCase):
    """Tests for do_delete_profile validation logic."""

    def test_refuses_unregistered_profile(self) -> None:
        """Exit code 1 when profile not in options.json."""
        self._write_options(["alpha", "beta"])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("ghost")
        self.assertEqual(rc, 1)
        self.assertIn("not registered", err.getvalue())

    def test_refuses_missing_options_file(self) -> None:
        """Exit code 1 when options.json does not exist."""
        # Don't write options file
        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("any")
        self.assertEqual(rc, 1)

    def test_refuses_running_profile(self) -> None:
        """Exit code 1 when profile appears to have active sessions."""
        self._write_options(["busy"])
        pdir = self._make_profile_dir("busy")
        sessions = pdir / "sessions"
        sessions.mkdir()
        # Write a PID file with our own PID (guaranteed alive)
        (sessions / "sess.pid").write_text(str(os.getpid()))

        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("busy")
        self.assertEqual(rc, 1)
        self.assertIn("active sessions", err.getvalue())

    def test_force_overrides_running_check(self) -> None:
        """With force=True, deletion proceeds even if sessions look active."""
        self._write_options(["busy"])
        pdir = self._make_profile_dir("busy")
        sessions = pdir / "sessions"
        sessions.mkdir()
        (sessions / "sess.pid").write_text(str(os.getpid()))

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = profile_ops.do_delete_profile("busy", force=True)
        self.assertEqual(rc, 0)
        self.assertFalse(pdir.exists())


# ---------------------------------------------------------------------------
# Directory removal
# ---------------------------------------------------------------------------


class RemoveProfileDirTests(_ProfileOpsTestCase):
    """Tests for _remove_profile_dir()."""

    def test_removes_real_files(self) -> None:
        """Real files and dirs are removed."""
        pdir = self._make_profile_dir("target")
        (pdir / "subdir").mkdir()
        (pdir / "subdir" / "nested.txt").write_text("x")

        sym, real = profile_ops._remove_profile_dir("target")
        self.assertFalse(pdir.exists())
        self.assertGreater(real, 0)

    def test_unlinks_symlinks_without_following(self) -> None:
        """Symlinks are unlinked, not followed into."""
        pdir = self._make_profile_dir("linked")
        shared = self.home / ".claude-shared" / "projects"
        shared.mkdir(parents=True)
        (shared / "important.jsonl").write_text("data")
        (pdir / "projects").symlink_to(shared)

        sym, real = profile_ops._remove_profile_dir("linked")
        self.assertFalse(pdir.exists())
        self.assertGreater(sym, 0)
        # Shared target must still exist
        self.assertTrue(shared.exists())
        self.assertTrue((shared / "important.jsonl").exists())

    def test_noop_when_dir_missing(self) -> None:
        """Returns (0, 0) when profile dir doesn't exist."""
        sym, real = profile_ops._remove_profile_dir("nonexistent")
        self.assertEqual(sym, 0)
        self.assertEqual(real, 0)


# ---------------------------------------------------------------------------
# Options cleanup
# ---------------------------------------------------------------------------


class RemoveFromOptionsTests(_ProfileOpsTestCase):
    """Tests for _remove_from_options()."""

    def test_removes_value_and_metadata(self) -> None:
        self._write_options(
            ["alpha", "beta"],
            metadata={"alpha": {"config_dir": "~/.claude-alpha"},
                       "beta": {"config_dir": "~/.claude-beta"}},
        )
        result = profile_ops._remove_from_options("alpha")
        self.assertTrue(result)

        opts = json.loads(self.options_file.read_text())
        self.assertNotIn("alpha", opts["profile"]["values"])
        self.assertNotIn("alpha", opts["profile"]["metadata"])
        self.assertIn("beta", opts["profile"]["values"])

    def test_returns_false_when_not_present(self) -> None:
        self._write_options(["alpha"])
        result = profile_ops._remove_from_options("ghost")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Xattr stripping
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_XATTR, "filesystem does not support user xattrs")
class StripXattrsTests(_ProfileOpsTestCase):
    """Tests for _strip_xattrs()."""

    def test_strips_matching_xattrs(self) -> None:
        projects = self.home / ".claude-shared" / "projects" / "myproject"
        projects.mkdir(parents=True)
        f1 = projects / "sess1.jsonl"
        f2 = projects / "sess2.jsonl"
        f3 = projects / "sess3.jsonl"
        f1.write_text("{}")
        f2.write_text("{}")
        f3.write_text("{}")
        os.setxattr(str(f1), b"user.origin-profile", b"doomed")
        os.setxattr(str(f2), b"user.origin-profile", b"doomed")
        os.setxattr(str(f3), b"user.origin-profile", b"keeper")

        count = profile_ops._strip_xattrs("doomed")
        self.assertEqual(count, 2)

        # f1 and f2 should have no xattr; f3 should still have it
        with self.assertRaises(OSError):
            os.getxattr(str(f1), b"user.origin-profile")
        val = os.getxattr(str(f3), b"user.origin-profile")
        self.assertEqual(val, b"keeper")

    def test_returns_zero_when_no_projects_dir(self) -> None:
        count = profile_ops._strip_xattrs("any")
        self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# Origins file cleanup
# ---------------------------------------------------------------------------


class CleanOriginsFileTests(_ProfileOpsTestCase):
    """Tests for _clean_origins_file()."""

    def test_removes_matching_lines(self) -> None:
        self._make_origins_file([
            {"path": "/a", "profile": "doomed", "ts": "t1"},
            {"path": "/b", "profile": "keeper", "ts": "t2"},
            {"path": "/c", "profile": "doomed", "ts": "t3"},
        ])
        count = profile_ops._clean_origins_file("doomed")
        self.assertEqual(count, 2)

        origins = profile_ops.ORIGINS_FILE
        remaining = [json.loads(l) for l in origins.read_text().strip().splitlines()]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["profile"], "keeper")

    def test_returns_zero_when_no_file(self) -> None:
        count = profile_ops._clean_origins_file("any")
        self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# Tokens cleanup
# ---------------------------------------------------------------------------


class RemoveFromTokensTests(_ProfileOpsTestCase):
    """Tests for _remove_from_tokens()."""

    def test_removes_entry(self) -> None:
        self._write_tokens({"alpha": "tok-a", "beta": "tok-b"})
        result = profile_ops._remove_from_tokens("alpha")
        self.assertTrue(result)

        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("alpha", tokens)
        self.assertIn("beta", tokens)

    def test_returns_false_when_missing(self) -> None:
        self._write_tokens({"alpha": "tok-a"})
        result = profile_ops._remove_from_tokens("ghost")
        self.assertFalse(result)

    def test_returns_false_when_no_file(self) -> None:
        result = profile_ops._remove_from_tokens("any")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Full do_delete_profile integration
# ---------------------------------------------------------------------------


class DoDeleteProfileIntegrationTests(_ProfileOpsTestCase):
    """End-to-end tests for do_delete_profile()."""

    def test_full_deletion(self) -> None:
        """Successful deletion removes dir, options, tokens, and origins."""
        self._write_options(
            ["target", "other"],
            metadata={"target": {"config_dir": "~/.claude-target"}},
        )
        self._write_tokens({"target": "tok-t", "other": "tok-o"})
        self._make_profile_dir("target")
        self._make_origins_file([
            {"path": "/x", "profile": "target", "ts": "t1"},
            {"path": "/y", "profile": "other", "ts": "t2"},
        ])

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = profile_ops.do_delete_profile("target")
        self.assertEqual(rc, 0)

        # Dir gone
        self.assertFalse((self.home / ".claude-target").exists())
        # Options updated
        opts = json.loads(self.options_file.read_text())
        self.assertNotIn("target", opts["profile"]["values"])
        # Tokens updated
        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("target", tokens)
        self.assertIn("other", tokens)
        # Origins preserved (historical data kept for analytics)
        origins_path = self.home / ".claude-common" / "profile-origins.jsonl"
        remaining = [json.loads(l) for l in origins_path.read_text().strip().splitlines()]
        self.assertEqual(len(remaining), 2)
        profiles = {e["profile"] for e in remaining}
        self.assertIn("target", profiles)
        self.assertIn("other", profiles)

    def test_deletion_when_dir_already_gone(self) -> None:
        """Succeeds even if profile dir does not exist (cleans up metadata only)."""
        self._write_options(["ghost"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = profile_ops.do_delete_profile("ghost")
        self.assertEqual(rc, 0)
        opts = json.loads(self.options_file.read_text())
        self.assertNotIn("ghost", opts["profile"]["values"])


if __name__ == "__main__":
    unittest.main()
