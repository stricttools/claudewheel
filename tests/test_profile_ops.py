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
        self._patcher_profiles = patch.object(
            profile_ops, "PROFILES_DIR", self.home / ".claudewheel" / "profiles",
        )
        self._patcher_opts.start()
        self._patcher_tokens.start()
        self._patcher_profiles.start()

    def tearDown(self) -> None:
        self._patcher_profiles.stop()
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
        pdir = self.home / ".claudewheel" / "profiles" / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        (pdir / "settings.json").write_text("{}")
        return pdir

    def _make_sibling_file(self, lines: list[dict]) -> None:
        """Write a JSONL file next to profiles/ to verify deletion spares siblings."""
        sibling = self.launcher_dir / "extra-data.jsonl"
        sibling.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


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
        shared = self.home / ".claudewheel" / "shared" / "projects"
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
            metadata={"alpha": {"config_dir": "~/.claudewheel/profiles/alpha"},
                       "beta": {"config_dir": "~/.claudewheel/profiles/beta"}},
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

    def test_preserves_target_file_mode(self) -> None:
        """The atomic tmp-swap must preserve the existing file's permissions
        (the tmp file is created with umask-default perms and its mode wins
        after rename). Regression test for the tmp-swap perms bug."""
        old_umask = os.umask(0o022)  # pin umask so the tmp file defaults 0644
        self.addCleanup(os.umask, old_umask)
        self._write_options(["alpha", "beta"])
        self.options_file.chmod(0o640)

        self.assertTrue(profile_ops._remove_from_options("alpha"))

        mode = self.options_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o640)


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

    def test_preserves_0600_permissions(self) -> None:
        """tokens.json holds secrets and must stay 0600 after the atomic
        tmp-swap rewrite. Regression test for the tmp-swap perms bug."""
        old_umask = os.umask(0o022)  # pin umask so the tmp file defaults 0644
        self.addCleanup(os.umask, old_umask)
        self._write_tokens({"alpha": "tok-a", "beta": "tok-b"})
        self.tokens_file.chmod(0o600)

        self.assertTrue(profile_ops._remove_from_tokens("alpha"))

        mode = self.tokens_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


# AddTokenTests moved to tests/test_tokens.py when add_token moved to tokens.py.


# ---------------------------------------------------------------------------
# Full do_delete_profile integration
# ---------------------------------------------------------------------------


class DoDeleteProfileIntegrationTests(_ProfileOpsTestCase):
    """End-to-end tests for do_delete_profile()."""

    def test_full_deletion(self) -> None:
        """Successful deletion removes dir, options, and tokens."""
        self._write_options(
            ["target", "other"],
            metadata={"target": {"config_dir": "~/.claudewheel/profiles/target"}},
        )
        self._write_tokens({"target": "tok-t", "other": "tok-o"})
        self._make_profile_dir("target")
        self._make_sibling_file([
            {"key": "a", "value": 1},
            {"key": "b", "value": 2},
        ])

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = profile_ops.do_delete_profile("target")
        self.assertEqual(rc, 0)

        # Dir gone
        self.assertFalse((self.home / ".claudewheel" / "profiles" / "target").exists())
        # Options updated
        opts = json.loads(self.options_file.read_text())
        self.assertNotIn("target", opts["profile"]["values"])
        # Tokens updated
        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("target", tokens)
        self.assertIn("other", tokens)
        # Sibling file preserved (deletion only targets the profile dir)
        sibling_path = self.launcher_dir / "extra-data.jsonl"
        remaining = [json.loads(line) for line in sibling_path.read_text().strip().splitlines()]
        self.assertEqual(len(remaining), 2)

    def test_deletion_when_dir_already_gone(self) -> None:
        """Succeeds even if profile dir does not exist (cleans up metadata only)."""
        self._write_options(["ghost"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = profile_ops.do_delete_profile("ghost")
        self.assertEqual(rc, 0)
        opts = json.loads(self.options_file.read_text())
        self.assertNotIn("ghost", opts["profile"]["values"])


# ---------------------------------------------------------------------------
# Pinned profile support
# ---------------------------------------------------------------------------


class PinnedProfileTests(_ProfileOpsTestCase):
    """Tests for profiles registered in options.json pinned list (wizard-created)."""

    def _write_options_with_pinned(
        self,
        values: list[str],
        pinned: list[str],
        metadata: dict | None = None,
    ) -> None:
        opts: dict = {"profile": {"values": values, "pinned": pinned}}
        if metadata:
            opts["profile"]["metadata"] = metadata
        self.options_file.write_text(json.dumps(opts, indent=2) + "\n")

    def test_delete_profile_registered_only_in_pinned(self) -> None:
        """A profile registered only in pinned (not values) can be deleted."""
        self._write_options_with_pinned(values=[], pinned=["wizard-prof"])
        self._make_profile_dir("wizard-prof")

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = profile_ops.do_delete_profile("wizard-prof")
        self.assertEqual(rc, 0)
        self.assertFalse(
            (self.home / ".claudewheel" / "profiles" / "wizard-prof").exists()
        )

    def test_remove_from_options_clears_pinned(self) -> None:
        """_remove_from_options removes the profile from the pinned list."""
        self._write_options_with_pinned(
            values=["other"], pinned=["wizard-prof"],
        )
        result = profile_ops._remove_from_options("wizard-prof")
        self.assertTrue(result)

        opts = json.loads(self.options_file.read_text())
        self.assertNotIn("wizard-prof", opts["profile"].get("pinned", []))
        self.assertIn("other", opts["profile"]["values"])

    def test_validation_rejects_unknown_profile(self) -> None:
        """A profile in neither values nor pinned is rejected."""
        self._write_options_with_pinned(
            values=["alpha"], pinned=["beta"],
        )
        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("ghost")
        self.assertEqual(rc, 1)
        self.assertIn("not registered", err.getvalue())

    def test_known_profiles_message_includes_pinned(self) -> None:
        """Error message for unknown profile lists both values and pinned profiles."""
        self._write_options_with_pinned(
            values=["from-values"], pinned=["from-pinned"],
        )
        err = io.StringIO()
        with redirect_stderr(err):
            profile_ops.do_delete_profile("ghost")
        output = err.getvalue()
        self.assertIn("from-values", output)
        self.assertIn("from-pinned", output)


if __name__ == "__main__":
    unittest.main()
