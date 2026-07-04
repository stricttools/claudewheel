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



from claudewheel import discovery, profile_info, profile_ops, state


class _ProfileOpsTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as home and patches paths."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher_home = patch.object(Path, "home", return_value=self.home)
        self._patcher_home.start()

        # Set up .claudewheel/ for options, tokens, and state
        self.launcher_dir = self.home / ".claudewheel"
        self.launcher_dir.mkdir()
        self.options_file = self.launcher_dir / "options.json"
        self.tokens_file = self.launcher_dir / "tokens.json"
        self.state_file = self.launcher_dir / "state.json"
        self.shared_dir = self.launcher_dir / "shared"
        self.skills_dir = self.launcher_dir / "skills"

        self._patchers = [
            patch.object(profile_ops, "OPTIONS_FILE", self.options_file),
            patch.object(profile_ops, "TOKENS_FILE", self.tokens_file),
            patch.object(
                profile_ops, "PROFILES_DIR", self.home / ".claudewheel" / "profiles",
            ),
            # fix_auth_shadow uses config_dir_for from profile_info
            patch.object(
                profile_info, "PROFILES_DIR", self.home / ".claudewheel" / "profiles",
            ),
            # delete_profile_core purges last_config via the state helpers --
            # keep it away from the real ~/.claudewheel/state.json.
            patch.object(state, "STATE_FILE", self.state_file),
            # classify_shared_dirs resolves symlink targets against these.
            patch.object(discovery, "SHARED_DIR", self.shared_dir),
            patch.object(discovery, "SKILLS_DIR", self.skills_dir),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patchers):
            p.stop()
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
# Data-destruction guard (red-green: plain delete used to rmtree REAL shared
# dirs -- a profile whose projects/ was a real directory lost actual
# conversation data on default delete)
# ---------------------------------------------------------------------------


class DataDestructionGuardTests(_ProfileOpsTestCase):
    """Real data at shared-dir names must block default deletion."""

    def _make_victim(self) -> Path:
        """Profile whose projects/ is a REAL directory holding data."""
        self._write_options(["victim"])
        pdir = self._make_profile_dir("victim")
        projects = pdir / "projects"
        projects.mkdir()
        (projects / "conversation.jsonl").write_text("irreplaceable session data")
        return projects

    def test_real_projects_dir_blocks_default_delete(self) -> None:
        """Default delete refuses when projects/ is a real dir; data survives."""
        projects = self._make_victim()

        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = profile_ops.do_delete_profile("victim")

        self.assertNotEqual(rc, 0)
        self.assertTrue(projects.exists(), "real projects/ dir must survive")
        self.assertTrue((projects / "conversation.jsonl").exists())

    def test_cli_refusal_names_at_risk_dirs_and_flag(self) -> None:
        """The refusal message names the at-risk dirs and --force-delete-data."""
        self._make_victim()
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = profile_ops.do_delete_profile("victim")
        self.assertEqual(rc, 1)
        self.assertIn("projects", err.getvalue())
        self.assertIn("--force-delete-data", err.getvalue())

    def test_core_refuses_with_at_risk_dirs(self) -> None:
        """delete_profile_core reports data-destruction with the dir names."""
        self._make_victim()
        result = profile_ops.delete_profile_core("victim")
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal_reason, "data-destruction")
        self.assertEqual(result.at_risk_dirs, ["projects"])

    def test_allow_data_destruction_deletes(self) -> None:
        """allow_data_destruction=True is the only way past the guard."""
        projects = self._make_victim()
        result = profile_ops.delete_profile_core(
            "victim", allow_data_destruction=True)
        self.assertTrue(result.ok)
        self.assertFalse(projects.exists())
        self.assertFalse(
            (self.home / ".claudewheel" / "profiles" / "victim").exists())

    def test_force_delete_data_flag_deletes_via_cli_wrapper(self) -> None:
        """force_data=True wires through do_delete_profile."""
        projects = self._make_victim()
        with redirect_stdout(io.StringIO()):
            rc = profile_ops.do_delete_profile("victim", force_data=True)
        self.assertEqual(rc, 0)
        self.assertFalse(projects.exists())

    def test_real_file_at_shared_name_also_blocks(self) -> None:
        """A real FILE at a shared-dir name blocks the same as a real dir."""
        self._write_options(["victim"])
        pdir = self._make_profile_dir("victim")
        (pdir / "todos").write_text("real data in a file")
        result = profile_ops.delete_profile_core("victim")
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal_reason, "data-destruction")
        self.assertEqual(result.at_risk_dirs, ["todos"])

    def test_all_missing_shared_dirs_delete_fine(self) -> None:
        """A profile with NO shared entries at all (all missing) deletes."""
        self._write_options(["plain"])
        self._make_profile_dir("plain")
        result = profile_ops.delete_profile_core("plain")
        self.assertTrue(result.ok)
        self.assertFalse(
            (self.home / ".claudewheel" / "profiles" / "plain").exists())

    def test_intact_symlinks_delete_fine(self) -> None:
        """Intact shared-store symlinks are unlinked, target survives."""
        self._write_options(["linked"])
        pdir = self._make_profile_dir("linked")
        target = self.shared_dir / "projects"
        target.mkdir(parents=True)
        (target / "data.jsonl").write_text("shared data")
        (pdir / "projects").symlink_to(target)

        result = profile_ops.delete_profile_core("linked")
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.removed_symlinks, 1)
        self.assertTrue((target / "data.jsonl").exists())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class DeleteProfileValidationTests(_ProfileOpsTestCase):
    """Tests for do_delete_profile validation logic."""

    def test_refuses_profile_with_no_registration_and_no_dir(self) -> None:
        """Exit code 1 when profile is neither in options.json nor on disk."""
        self._write_options(["alpha", "beta"])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("ghost")
        self.assertEqual(rc, 1)
        self.assertIn("not registered", err.getvalue())

    def test_deletes_unregistered_profile_with_dir_on_disk(self) -> None:
        """A profile absent from options.json but present under PROFILES_DIR
        deletes fine (the TUI shows discovered-but-unregistered profiles)."""
        self._write_options(["alpha"])
        pdir = self._make_profile_dir("orphan")
        with redirect_stdout(io.StringIO()):
            rc = profile_ops.do_delete_profile("orphan")
        self.assertEqual(rc, 0)
        self.assertFalse(pdir.exists())

    def test_refuses_missing_options_file_and_no_dir(self) -> None:
        """Exit code 1 when options.json is missing and the dir is absent."""
        # Don't write options file
        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("any")
        self.assertEqual(rc, 1)

    def test_refuses_default_profile(self) -> None:
        """'default' (~/.claude) is never deletable, even when registered."""
        self._write_options(["default"])
        self._write_tokens({"default": "tok-d"})
        err = io.StringIO()
        with redirect_stderr(err):
            rc = profile_ops.do_delete_profile("default")
        self.assertEqual(rc, 1)
        self.assertIn("built-in", err.getvalue())
        # Nothing was cleaned up: options and tokens entries survive
        opts = json.loads(self.options_file.read_text())
        self.assertIn("default", opts["profile"]["values"])
        tokens = json.loads(self.tokens_file.read_text())
        self.assertIn("default", tokens)

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
# delete_profile_core: DeleteResult refusals, success flags, last_config purge
# ---------------------------------------------------------------------------


class DeleteProfileCoreTests(_ProfileOpsTestCase):
    """Tests for the print-free core and its DeleteResult."""

    def test_refusal_default_profile(self) -> None:
        result = profile_ops.delete_profile_core("default")
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal_reason, "default-profile")

    def test_refusal_not_found_lists_known_profiles(self) -> None:
        self._write_options(["alpha"])
        opts = json.loads(self.options_file.read_text())
        opts["profile"]["pinned"] = ["beta"]
        self.options_file.write_text(json.dumps(opts))
        result = profile_ops.delete_profile_core("ghost")
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal_reason, "not-found")
        self.assertEqual(result.known_profiles, ["alpha", "beta"])

    def test_refusal_running(self) -> None:
        self._write_options(["busy"])
        pdir = self._make_profile_dir("busy")
        sessions = pdir / "sessions"
        sessions.mkdir()
        (sessions / "sess.pid").write_text(str(os.getpid()))
        result = profile_ops.delete_profile_core("busy")
        self.assertFalse(result.ok)
        self.assertEqual(result.refusal_reason, "running")
        self.assertTrue(pdir.exists())

    def test_skip_running_check(self) -> None:
        self._write_options(["busy"])
        pdir = self._make_profile_dir("busy")
        sessions = pdir / "sessions"
        sessions.mkdir()
        (sessions / "sess.pid").write_text(str(os.getpid()))
        result = profile_ops.delete_profile_core("busy", skip_running_check=True)
        self.assertTrue(result.ok)
        self.assertFalse(pdir.exists())

    def test_success_reports_removal_flags(self) -> None:
        self._write_options(["target"])
        self._write_tokens({"target": "tok-t"})
        self._make_profile_dir("target")
        result = profile_ops.delete_profile_core("target")
        self.assertTrue(result.ok)
        self.assertIsNone(result.refusal_reason)
        self.assertTrue(result.removed_from_options)
        self.assertTrue(result.removed_from_tokens)
        self.assertGreater(result.removed_real, 0)

    def test_core_prints_nothing(self) -> None:
        """The core must not print, even on success or refusal."""
        self._write_options(["target"])
        self._make_profile_dir("target")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            profile_ops.delete_profile_core("target")
            profile_ops.delete_profile_core("default")
            profile_ops.delete_profile_core("ghost")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_last_config_purged_when_it_names_the_profile(self) -> None:
        self._write_options(["target"])
        self._make_profile_dir("target")
        self.state_file.write_text(json.dumps({
            "last_config": {"profile": "target", "model": "opus"},
            "launch_count": 7,
        }))
        result = profile_ops.delete_profile_core("target")
        self.assertTrue(result.ok)
        self.assertTrue(result.last_config_purged)
        on_disk = json.loads(self.state_file.read_text())
        self.assertNotIn("profile", on_disk["last_config"])
        # Everything else survives the read-modify-write
        self.assertEqual(on_disk["last_config"]["model"], "opus")
        self.assertEqual(on_disk["launch_count"], 7)

    def test_last_config_untouched_when_other_profile(self) -> None:
        self._write_options(["target"])
        self._make_profile_dir("target")
        self.state_file.write_text(json.dumps({
            "last_config": {"profile": "other"},
        }))
        result = profile_ops.delete_profile_core("target")
        self.assertTrue(result.ok)
        self.assertFalse(result.last_config_purged)
        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk["last_config"]["profile"], "other")

    def test_last_config_purge_tolerates_missing_state_file(self) -> None:
        self._write_options(["target"])
        self._make_profile_dir("target")
        result = profile_ops.delete_profile_core("target")
        self.assertTrue(result.ok)
        self.assertFalse(result.last_config_purged)


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


# ---------------------------------------------------------------------------
# fix_auth_shadow
# ---------------------------------------------------------------------------


class FixAuthShadowTests(_ProfileOpsTestCase):
    """Tests for fix_auth_shadow: remove claudeAiOauth from .credentials.json."""

    def _write_credentials(self, pdir: Path, data: dict) -> None:
        creds = pdir / ".credentials.json"
        creds.write_text(json.dumps(data))
        creds.chmod(0o600)

    def test_no_token_returns_reason(self) -> None:
        """When tokens.json has no entry for the profile, reason is 'no-token'."""
        self._make_profile_dir("orphan")
        self._write_tokens({})
        result = profile_ops.fix_auth_shadow("orphan")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-token")

    def test_no_credentials_file_returns_no_shadow(self) -> None:
        """When .credentials.json doesn't exist, reason is 'no-shadow'."""
        pdir = self._make_profile_dir("clean")
        # Remove the .credentials.json that _make_profile_dir creates
        (pdir / ".credentials.json").unlink()
        self._write_tokens({"clean": {"token": "tok-abc"}})
        result = profile_ops.fix_auth_shadow("clean")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-shadow")

    def test_no_claudeAiOauth_key_returns_no_shadow(self) -> None:
        """When .credentials.json exists but has no claudeAiOauth, reason is 'no-shadow'."""
        pdir = self._make_profile_dir("noshadow")
        self._write_credentials(pdir, {"mcpOAuth": {"x": "y"}})
        self._write_tokens({"noshadow": {"token": "tok-ns"}})
        result = profile_ops.fix_auth_shadow("noshadow")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no-shadow")

    def test_unreadable_credentials_returns_reason(self) -> None:
        """When .credentials.json is corrupt JSON, reason is 'unreadable-creds'."""
        pdir = self._make_profile_dir("corrupt")
        (pdir / ".credentials.json").write_text("{not json at all")
        self._write_tokens({"corrupt": {"token": "tok-c"}})
        result = profile_ops.fix_auth_shadow("corrupt")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "unreadable-creds")

    def test_strips_shadow_and_saves_tier(self) -> None:
        """Shadow is stripped, tier data saved to tokens.json."""
        pdir = self._make_profile_dir("work")
        self._write_credentials(pdir, {
            "claudeAiOauth": {
                "accessToken": "short-lived",
                "rateLimitTier": "default_claude_pro",
                "subscriptionType": "claude_pro",
            },
            "mcpOAuth": {"keep": "this"},
        })
        self._write_tokens({"work": {"token": "tok-work"}})

        result = profile_ops.fix_auth_shadow("work")

        self.assertTrue(result.ok)
        self.assertIsNone(result.reason)
        self.assertEqual(result.tier_saved, "default_claude_pro")
        self.assertEqual(result.subscription_saved, "claude_pro")

        # Verify .credentials.json was updated
        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)
        self.assertIn("mcpOAuth", creds)

        # Verify tokens.json has tier fields
        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["work"]["rateLimitTier"], "default_claude_pro")
        self.assertEqual(tokens["work"]["subscriptionType"], "claude_pro")
        # Original token preserved
        self.assertEqual(tokens["work"]["token"], "tok-work")

    def test_strips_shadow_no_tier_data(self) -> None:
        """Shadow stripped even without tier fields; no tier saved."""
        pdir = self._make_profile_dir("notier")
        self._write_credentials(pdir, {
            "claudeAiOauth": {"accessToken": "short"},
        })
        self._write_tokens({"notier": {"token": "tok-nt"}})

        result = profile_ops.fix_auth_shadow("notier")

        self.assertTrue(result.ok)
        self.assertIsNone(result.tier_saved)
        self.assertIsNone(result.subscription_saved)

        # Verify .credentials.json was updated
        creds = json.loads((pdir / ".credentials.json").read_text())
        self.assertNotIn("claudeAiOauth", creds)

        # Verify tokens.json was NOT modified (no tier to save)
        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("rateLimitTier", tokens.get("notier", {}))

    def test_bare_string_token_upgraded_to_dict_with_tier(self) -> None:
        """When token entry is a bare string, it's upgraded to a dict to hold tier."""
        pdir = self._make_profile_dir("legacy")
        self._write_credentials(pdir, {
            "claudeAiOauth": {
                "accessToken": "ephemeral",
                "rateLimitTier": "tier_max",
            },
        })
        self._write_tokens({"legacy": "bare-tok-string"})

        result = profile_ops.fix_auth_shadow("legacy")

        self.assertTrue(result.ok)
        self.assertEqual(result.tier_saved, "tier_max")

        tokens = json.loads(self.tokens_file.read_text())
        self.assertEqual(tokens["legacy"]["token"], "bare-tok-string")
        self.assertEqual(tokens["legacy"]["rateLimitTier"], "tier_max")

    def test_atomic_write_preserves_credentials_permissions(self) -> None:
        """The atomic write to .credentials.json preserves 0600 permissions."""
        pdir = self._make_profile_dir("perms")
        self._write_credentials(pdir, {
            "claudeAiOauth": {"accessToken": "x"},
            "other": "keep",
        })
        creds_path = pdir / ".credentials.json"
        creds_path.chmod(0o600)
        self._write_tokens({"perms": {"token": "tok-p"}})

        profile_ops.fix_auth_shadow("perms")

        mode = creds_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


# ---------------------------------------------------------------------------
# Profile rename
# ---------------------------------------------------------------------------


class RenameProfileTests(_ProfileOpsTestCase):
    """rename_profile moves dir and updates all JSON stores."""

    def test_full_rename_updates_all_stores(self) -> None:
        """Rename updates dir, tokens, options, and state."""
        self._make_profile_dir("alpha")
        self._write_options(
            ["alpha", "beta"],
            metadata={"alpha": {"config_dir": "~/.claudewheel/profiles/alpha"}},
        )
        self._write_tokens({"alpha": {"token": "tok-a"}, "beta": "tok-b"})
        self.state_file.write_text(json.dumps({
            "last_config": {"profile": "alpha", "model": "opus"},
        }))

        profile_ops.rename_profile("alpha", "zeta")

        # Dir moved
        profiles_dir = self.home / ".claudewheel" / "profiles"
        self.assertFalse((profiles_dir / "alpha").exists())
        self.assertTrue((profiles_dir / "zeta").is_dir())
        # No breadcrumb
        self.assertFalse((profiles_dir / "zeta" / ".rename_pending").exists())

        # Tokens updated
        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("alpha", tokens)
        self.assertIn("zeta", tokens)
        self.assertEqual(tokens["zeta"]["token"], "tok-a")

        # Options updated
        options = json.loads(self.options_file.read_text())
        values = options["profile"]["values"]
        self.assertNotIn("alpha", values)
        self.assertIn("zeta", values)
        meta = options["profile"]["metadata"]
        self.assertNotIn("alpha", meta)
        self.assertIn("zeta", meta)
        self.assertEqual(meta["zeta"]["config_dir"], "~/.claudewheel/profiles/zeta")

        # State updated
        state_data = json.loads(self.state_file.read_text())
        self.assertEqual(state_data["last_config"]["profile"], "zeta")

    def test_rename_tokens_absent_skipped(self) -> None:
        """If profile has no token, rename still succeeds (tokens step is skipped)."""
        self._make_profile_dir("notoken")
        self._write_options(["notoken"])
        # No tokens.json at all

        profile_ops.rename_profile("notoken", "renamed")

        profiles_dir = self.home / ".claudewheel" / "profiles"
        self.assertTrue((profiles_dir / "renamed").is_dir())

    def test_rename_state_not_matching_unchanged(self) -> None:
        """If last_config.profile != old, state.json is not modified."""
        self._make_profile_dir("gamma")
        self._write_options(["gamma"])
        self.state_file.write_text(json.dumps({
            "last_config": {"profile": "other", "model": "sonnet"},
        }))

        profile_ops.rename_profile("gamma", "delta")

        state_data = json.loads(self.state_file.read_text())
        self.assertEqual(state_data["last_config"]["profile"], "other")

    def test_rename_nonexistent_dir_raises(self) -> None:
        """Renaming a profile with no directory raises ValueError."""
        self._write_options(["ghost"])
        with self.assertRaises(ValueError):
            profile_ops.rename_profile("ghost", "new-name")

    def test_rename_target_exists_raises(self) -> None:
        """Renaming to an existing directory raises ValueError."""
        self._make_profile_dir("src")
        self._make_profile_dir("dst")
        self._write_options(["src", "dst"])
        with self.assertRaises(ValueError):
            profile_ops.rename_profile("src", "dst")

    def test_pinned_list_updated(self) -> None:
        """Profile in pinned list gets renamed there too."""
        self._make_profile_dir("pinned-one")
        opts = {
            "profile": {
                "values": ["pinned-one"],
                "pinned": ["pinned-one"],
                "metadata": {},
            }
        }
        self.options_file.write_text(json.dumps(opts))

        profile_ops.rename_profile("pinned-one", "pinned-two")

        options = json.loads(self.options_file.read_text())
        self.assertIn("pinned-two", options["profile"]["pinned"])
        self.assertNotIn("pinned-one", options["profile"]["pinned"])


class RenameRecoveryTests(_ProfileOpsTestCase):
    """recover_incomplete_renames finishes a crashed rename."""

    def test_recovery_completes_rename(self) -> None:
        """Breadcrumb in renamed dir triggers store updates."""
        # Simulate crash: dir already renamed, but JSON stores still reference old
        profiles_dir = self.home / ".claudewheel" / "profiles"
        new_dir = profiles_dir / "new-name"
        new_dir.mkdir(parents=True)
        (new_dir / ".credentials.json").write_text("{}")
        (new_dir / ".rename_pending").write_text(
            json.dumps({"from": "old-name", "to": "new-name"})
        )

        self._write_options(
            ["old-name"],
            metadata={"old-name": {"config_dir": "~/.claudewheel/profiles/old-name"}},
        )
        self._write_tokens({"old-name": "tok-old"})
        self.state_file.write_text(json.dumps({
            "last_config": {"profile": "old-name"},
        }))

        recovered = profile_ops.recover_incomplete_renames()

        self.assertEqual(recovered, ["new-name"])
        # Breadcrumb removed
        self.assertFalse((new_dir / ".rename_pending").exists())
        # Stores updated
        tokens = json.loads(self.tokens_file.read_text())
        self.assertNotIn("old-name", tokens)
        self.assertIn("new-name", tokens)
        options = json.loads(self.options_file.read_text())
        self.assertNotIn("old-name", options["profile"]["values"])
        self.assertIn("new-name", options["profile"]["values"])
        state_data = json.loads(self.state_file.read_text())
        self.assertEqual(state_data["last_config"]["profile"], "new-name")

    def test_recovery_no_breadcrumb_noop(self) -> None:
        """Without breadcrumb, nothing happens."""
        profiles_dir = self.home / ".claudewheel" / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "clean").mkdir()

        recovered = profile_ops.recover_incomplete_renames()
        self.assertEqual(recovered, [])

    def test_recovery_idempotent(self) -> None:
        """Running recovery when stores already reference new name is safe."""
        profiles_dir = self.home / ".claudewheel" / "profiles"
        new_dir = profiles_dir / "fresh"
        new_dir.mkdir(parents=True)
        (new_dir / ".rename_pending").write_text(
            json.dumps({"from": "stale", "to": "fresh"})
        )

        # Stores already have "fresh" (not "stale")
        self._write_options(["fresh"])
        self._write_tokens({"fresh": "tok-f"})

        recovered = profile_ops.recover_incomplete_renames()

        self.assertEqual(recovered, ["fresh"])
        # Breadcrumb removed, stores unchanged
        self.assertFalse((new_dir / ".rename_pending").exists())
        tokens = json.loads(self.tokens_file.read_text())
        self.assertIn("fresh", tokens)


if __name__ == "__main__":
    unittest.main()
