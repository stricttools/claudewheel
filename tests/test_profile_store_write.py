"""Contract, parity, and crash-safety tests for ProfileStore write operations.

These target the NEW ProfileStore write path (create/delete/rename/recover),
built beside the live wizard/profile_ops code. The old paths remain the
production code until a later cutover phase; the parity tests here pin the new
path's artifacts to what the old code produces today.

RED observation (wizard atomicity, documented, not a permanent test)
--------------------------------------------------------------------
Before implementing ``ProfileStore.create``, the settings write of the OLD path
(``wizard.create_profile``, which writes ``settings.json`` via a plain
``Path.write_text``) was fault-injected to write only the first half of the JSON
and then raise mid-write. The result on disk was a TRUNCATED, unparseable file:

    settings.json exists: True
    bytes on disk: 63
    content: '{\\n  "foo": "bar",\\n  "permissions": {\\n    "disableAutoMode": "di'
    json.loads -> JSONDecodeError: Unterminated string starting at line 4 column 24

That is the bug this store fixes. ``ProfileStore.create`` writes ``settings.json``
via ``fsutil.write_json_atomic`` (tmp-file + rename), so an interrupted write can
never leave a truncated target -- the permanent GREEN test below
(``test_create_settings_write_is_atomic``) asserts the store leaves either the
complete prior state or no file at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import claudewheel.profile_store as ps_mod
import claudewheel.wizard as wiz
from claudewheel.appdata import OptionsFile, StateFile
from claudewheel.profile_store import (
    DeletionResult,
    Profile,
    ProfileStore,
    _RENAME_PENDING_FILE,
)
from claudewheel.shared_store import SharedStore
from claudewheel.tokens import TokenStore
from claudewheel.wizard import WizardResult
from claudewheel.workspace import Workspace
from tests.wheelhelpers import SandboxHomeTestCase, write_json


class _WriteBase(SandboxHomeTestCase):
    """Shared setup: a fully-wired store plus small store/disk helpers."""

    def setUp(self) -> None:
        super().setUp()
        # Workspace.default() honors the poisoned Path.home, so root resolves to
        # <sandbox>/.claudewheel and claude_dir to <sandbox>/.claude.
        self.store = Workspace.default().profiles
        self.profiles_dir = self.sandbox_paths["PROFILES_DIR"]
        self.options_file = self.sandbox_paths["OPTIONS_FILE"]
        self.tokens_file = self.sandbox_paths["TOKENS_FILE"]
        self.state_file = self.sandbox_paths["STATE_FILE"]
        self.shared_dir = self.sandbox_paths["SHARED_DIR"]
        self.skills_dir = self.sandbox_paths["SKILLS_DIR"]

    def _read_options(self) -> dict:
        return json.loads(self.options_file.read_text())

    def _read_tokens(self) -> dict:
        return json.loads(self.tokens_file.read_text())

    def _read_state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def _set_profile_options(self, section: dict) -> None:
        options = self._read_options()
        options["profile"] = section
        write_json(self.options_file, options)


# ---------------------------------------------------------------------------
# Write-store guards
# ---------------------------------------------------------------------------


class WriteGuardTests(_WriteBase):
    def _readonly_store(self) -> ProfileStore:
        return ProfileStore(
            self.profiles_dir, self.home / ".claude", TokenStore(self.tokens_file)
        )

    def test_write_ops_require_stores(self) -> None:
        store = self._readonly_store()
        with self.assertRaises(RuntimeError):
            store.create("x", {})
        with self.assertRaises(RuntimeError):
            store.delete("x")
        with self.assertRaises(RuntimeError):
            store.rename("a", "b")
        with self.assertRaises(RuntimeError):
            store.recover_incomplete_renames()

    def test_classify_requires_shared(self) -> None:
        store = self._readonly_store()
        with self.assertRaises(RuntimeError):
            store.classify_shared_dirs("x")

    def test_read_apis_work_without_write_stores(self) -> None:
        # Read path must still function with no shared/options/state wired.
        store = self._readonly_store()
        self.assertEqual(store.enumerate(), [])
        self.assertEqual(store.path_for("default"), self.home / ".claude")


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------


class CreateTests(_WriteBase):
    _SETTINGS = {
        "model": "claude-opus-4-8",
        "permissions": {"disableAutoMode": "disable"},
    }

    def test_create_artifacts(self) -> None:
        profile = self.store.create("alpha", self._SETTINGS)
        self.assertIsInstance(profile, Profile)
        target = self.profiles_dir / "alpha"
        self.assertEqual(profile.path, target)
        self.assertTrue(target.is_dir())

        # settings.json content (semantic, parsed-JSON equality)
        written = json.loads((target / "settings.json").read_text())
        self.assertEqual(written, self._SETTINGS)

        # onboarding flag merged into .claude.json
        claude_json = json.loads((target / ".claude.json").read_text())
        self.assertTrue(claude_json["hasCompletedOnboarding"])

        # all six shared symlinks + skills, pointing at the shared store
        for sub in SharedStore.SHARED_SUBDIRS:
            link = target / sub
            self.assertTrue(link.is_symlink(), sub)
            self.assertEqual(link.resolve(), (self.shared_dir / sub).resolve())
        skills_link = target / "skills"
        self.assertTrue(skills_link.is_symlink())
        self.assertEqual(skills_link.resolve(), self.skills_dir.resolve())

        # registration lands in options.json pinned (no metadata written)
        profile_sec = self._read_options()["profile"]
        self.assertIn("alpha", profile_sec["pinned"])
        self.assertNotIn("alpha", profile_sec.get("metadata", {}))

    def test_create_no_symlinks_when_disabled(self) -> None:
        # symlink_shared=False mirrors the wizard checkbox: a plain profile dir
        # with NO shared-store subdir links and NO skills link, while settings
        # and options registration still land.
        target = self.profiles_dir / "plain"
        self.store.create("plain", self._SETTINGS, symlink_shared=False)
        for sub in SharedStore.SHARED_SUBDIRS:
            self.assertFalse((target / sub).exists(), sub)
            self.assertFalse((target / sub).is_symlink(), sub)
        self.assertFalse((target / "skills").exists())
        self.assertFalse((target / "skills").is_symlink())
        # Settings + registration still landed.
        self.assertTrue((target / "settings.json").exists())
        self.assertIn("plain", self._read_options()["profile"]["pinned"])

    def test_create_no_onboarding(self) -> None:
        target = self.profiles_dir / "noonb"
        self.store.create("noonb", self._SETTINGS, set_onboarding=False)
        self.assertFalse((target / ".claude.json").exists())

    def test_create_reserved_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create("default", self._SETTINGS)

    def test_create_existing_dir(self) -> None:
        (self.profiles_dir / "dup").mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            self.store.create("dup", self._SETTINGS)

    def test_create_skips_skills_when_absent(self) -> None:
        # The skills symlink is guarded by skills_dir.is_dir(); with no skills
        # dir, no skills link is created (the reachable "skip" branch of the
        # wizard's symlink loop -- the subdir links are always created because
        # create() refuses a pre-existing profile dir).
        import shutil

        shutil.rmtree(self.skills_dir)
        target = self.profiles_dir / "noskills"
        self.store.create("noskills", self._SETTINGS)
        self.assertFalse((target / "skills").exists())
        self.assertFalse((target / "skills").is_symlink())
        # subdir links still present
        for sub in SharedStore.SHARED_SUBDIRS:
            self.assertTrue((target / sub).is_symlink())

    def test_create_settings_write_is_atomic(self) -> None:
        # GREEN counterpart to the wizard RED observation in the module
        # docstring. Inject a failure at the atomic rename step; the target
        # settings.json must be absent -- never a truncated partial file.
        target = self.profiles_dir / "atomic"
        with patch.object(Path, "rename", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.store.create("atomic", self._SETTINGS)
        self.assertFalse((target / "settings.json").exists())

    def test_create_cleanup_on_failure_enables_retry(self) -> None:
        # A failure partway through create() must remove the partially-created
        # target dir (everything under it was made by this call), so a retry is
        # not blocked by the pre-mkdir FileExistsError guard.
        target = self.profiles_dir / "retry"
        with patch.object(Path, "rename", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.store.create("retry", self._SETTINGS)
        # The debris is gone -- the whole target dir was cleaned up.
        self.assertFalse(target.exists())
        # A retry now succeeds instead of hitting FileExistsError.
        profile = self.store.create("retry", self._SETTINGS)
        self.assertIsInstance(profile, Profile)
        self.assertTrue((target / "settings.json").exists())

    def test_create_refusal_preserves_existing_dir(self) -> None:
        # The pre-mkdir FileExistsError refusal path must NOT delete anything:
        # the store never created the dir, so it does not own its contents.
        target = self.profiles_dir / "keepme"
        target.mkdir(parents=True)
        (target / "sentinel.txt").write_text("do not delete")
        with self.assertRaises(FileExistsError):
            self.store.create("keepme", self._SETTINGS)
        # Pre-existing dir and its contents survive untouched.
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "sentinel.txt").exists())
        self.assertEqual((target / "sentinel.txt").read_text(), "do not delete")

    def test_create_cleanup_is_symlink_safe(self) -> None:
        # Cleanup on failure must unlink shared-store symlinks WITHOUT following
        # them into the shared store -- real session data behind the links must
        # survive. Inject a failure at options registration (after symlinks are
        # created) so the cleanup path exercises symlink removal.
        (self.shared_dir / "projects" / "payload.jsonl").write_text("keep me")
        target = self.profiles_dir / "symsafe"
        with patch.object(
            self.store.options, "add_pinned", side_effect=OSError("injected")
        ):
            with self.assertRaises(OSError):
                self.store.create("symsafe", self._SETTINGS)
        # Debris removed, but shared data behind the symlinks survives.
        self.assertFalse(target.exists())
        self.assertTrue((self.shared_dir / "projects" / "payload.jsonl").exists())
        self.assertEqual(
            (self.shared_dir / "projects" / "payload.jsonl").read_text(), "keep me"
        )

    def test_create_parity_with_wizard(self) -> None:
        # Give the wizard real profileDefaults so it produces a non-trivial
        # final settings dict, then feed that SAME dict to the store and compare
        # on-disk artifacts.
        write_json(
            self.sandbox_paths["SHARED_SETTINGS_FILE"],
            {
                "profileDefaults": {"model": "claude-opus-4-8", "theme": "dark"},
                "hooks": {},
                "disallowedTools": ["Foo"],
            },
        )
        result = WizardResult(
            name="wizprof",
            config_dir=str(self.profiles_dir / "wizprof"),
            clone_from=None,
            wire_hooks=False,
            symlink_shared=True,
            disable_recap=False,
            cleanup_10y=False,
            disable_memory=False,
            disable_attribution=False,
        )
        wiz.create_profile(self.ws, result)
        wiz_dir = self.profiles_dir / "wizprof"
        final_settings = json.loads((wiz_dir / "settings.json").read_text())

        self.store.create("stprof", final_settings)
        st_dir = self.profiles_dir / "stprof"

        # Semantic settings equality
        self.assertEqual(
            json.loads((st_dir / "settings.json").read_text()), final_settings
        )
        # Onboarding parity
        self.assertTrue(
            json.loads((wiz_dir / ".claude.json").read_text())["hasCompletedOnboarding"]
        )
        self.assertTrue(
            json.loads((st_dir / ".claude.json").read_text())["hasCompletedOnboarding"]
        )
        # Symlink target parity
        for sub in list(SharedStore.SHARED_SUBDIRS) + ["skills"]:
            self.assertEqual(
                (wiz_dir / sub).resolve(), (st_dir / sub).resolve(), sub
            )


# ---------------------------------------------------------------------------
# classify_shared_dirs (rehomed from discovery)
# ---------------------------------------------------------------------------


class ClassifySharedDirsTests(_WriteBase):
    """ProfileStore.classify_shared_dirs over all four states (pinned expectations).

    Formerly a parity test against the (now deleted)
    ``discovery.classify_shared_dirs``; the four-state classification is now
    pinned by the explicit expectations below.
    """

    def test_classify_all_four_states(self) -> None:
        cls_dir = self.profiles_dir / "cls"
        cls_dir.mkdir(parents=True)
        # intact
        (cls_dir / "projects").symlink_to(self.shared_dir / "projects")
        # wrong-target
        other = self.home / "elsewhere"
        other.mkdir()
        (cls_dir / "session-env").symlink_to(other)
        # real-dir
        (cls_dir / "file-history").mkdir()
        # real file (classified as real-dir)
        (cls_dir / "tasks").write_text("data")
        # todos, paste-cache: missing
        # skills intact
        (cls_dir / "skills").symlink_to(self.skills_dir)

        store_states = self.store.classify_shared_dirs("cls")

        self.assertEqual(store_states["projects"], "intact")
        self.assertEqual(store_states["session-env"], "wrong-target")
        self.assertEqual(store_states["file-history"], "real-dir")
        self.assertEqual(store_states["tasks"], "real-dir")
        self.assertEqual(store_states["todos"], "missing")
        self.assertEqual(store_states["paste-cache"], "missing")
        self.assertEqual(store_states["skills"], "intact")


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class DeleteTests(_WriteBase):
    _SETTINGS = {"model": "claude-opus-4-8"}

    def test_delete_default_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.delete("default")

    def test_delete_not_found(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.store.delete("ghost")
        self.assertIn("ghost", str(ctx.exception))

    def test_delete_real_data_refused_lists_offenders(self) -> None:
        self.store.create("data", self._SETTINGS)
        target = self.profiles_dir / "data"
        # Convert the projects symlink into a real dir holding data.
        (target / "projects").unlink()
        (target / "projects").mkdir()
        (target / "projects" / "sess.jsonl").write_text("x")

        with self.assertRaises(ValueError) as ctx:
            self.store.delete("data")
        self.assertIn("projects", str(ctx.exception))
        # Nothing was removed.
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "projects" / "sess.jsonl").exists())

    def test_delete_allow_data_destruction(self) -> None:
        self.store.create("data2", self._SETTINGS)
        target = self.profiles_dir / "data2"
        (target / "projects").unlink()
        (target / "projects").mkdir()
        (target / "projects" / "sess.jsonl").write_text("x")

        result = self.store.delete("data2", allow_data_destruction=True)
        self.assertIsInstance(result, DeletionResult)
        self.assertFalse(target.exists())
        self.assertGreaterEqual(result.removed_real, 1)

    def test_delete_shared_session_data_survives(self) -> None:
        self.store.create("keep", self._SETTINGS)
        target = self.profiles_dir / "keep"
        # Write real session data into the SHARED store (the symlink target).
        (self.shared_dir / "projects" / "sess.jsonl").write_text("payload")

        result = self.store.delete("keep")
        self.assertFalse(target.exists())
        # The symlinks were unlinked without following: shared data survives.
        self.assertTrue((self.shared_dir / "projects" / "sess.jsonl").exists())
        # 6 subdir symlinks + skills = 7; settings.json + .claude.json = 2 real.
        self.assertEqual(result.removed_symlinks, 7)
        self.assertEqual(result.removed_real, 2)

    def test_delete_full_cleanup(self) -> None:
        self.store.create("full", self._SETTINGS)
        self.store.token_store.add("full", "TOKEN")
        self.store.state.set_value("last_config", {"profile": "full", "model": "m"})

        result = self.store.delete("full")

        self.assertTrue(result.removed_from_options)
        self.assertTrue(result.removed_from_tokens)
        self.assertTrue(result.last_config_purged)
        self.assertNotIn("full", self._read_options()["profile"]["pinned"])
        self.assertNotIn("full", self._read_tokens())
        self.assertNotIn("profile", self._read_state()["last_config"])
        # Other last_config keys are preserved.
        self.assertEqual(self._read_state()["last_config"].get("model"), "m")


# ---------------------------------------------------------------------------
# rename() + recovery
# ---------------------------------------------------------------------------


class RenameTests(_WriteBase):
    _OLD_META = {"config_dir": "~/.claudewheel/profiles/old"}

    def _seed(self) -> None:
        """Create profile 'old' with token, options (values+pinned+metadata), state."""
        old_dir = self.profiles_dir / "old"
        old_dir.mkdir(parents=True)
        (old_dir / "projects").symlink_to(self.shared_dir / "projects")
        self._set_profile_options(
            {
                "values": ["old"],
                "pinned": ["old"],
                "metadata": {"old": dict(self._OLD_META)},
            }
        )
        write_json(self.tokens_file, {"old": {"token": "T", "created": "2026-01-01"}})
        self.store.state.set_value("last_config", {"profile": "old"})

    def _assert_clean_rename(self) -> None:
        self.assertFalse((self.profiles_dir / "old").exists())
        self.assertTrue((self.profiles_dir / "new").is_dir())
        tokens = self._read_tokens()
        self.assertIn("new", tokens)
        self.assertNotIn("old", tokens)
        sec = self._read_options()["profile"]
        self.assertEqual(sec["values"], ["new"])
        self.assertEqual(sec["pinned"], ["new"])
        self.assertIn("new", sec["metadata"])
        self.assertNotIn("old", sec["metadata"])
        # Metadata key moved VERBATIM -- config_dir NOT rewritten.
        self.assertEqual(sec["metadata"]["new"], self._OLD_META)
        self.assertEqual(self._read_state()["last_config"]["profile"], "new")
        self._assert_no_breadcrumbs()

    def _assert_clean_no_rename(self) -> None:
        self.assertTrue((self.profiles_dir / "old").is_dir())
        self.assertFalse((self.profiles_dir / "new").exists())
        tokens = self._read_tokens()
        self.assertIn("old", tokens)
        self.assertNotIn("new", tokens)
        sec = self._read_options()["profile"]
        self.assertEqual(sec["values"], ["old"])
        self.assertEqual(sec["pinned"], ["old"])
        self.assertIn("old", sec["metadata"])
        self.assertEqual(self._read_state()["last_config"]["profile"], "old")
        self._assert_no_breadcrumbs()

    def _assert_no_breadcrumbs(self) -> None:
        for d in self.profiles_dir.iterdir():
            if d.is_dir():
                self.assertFalse(
                    (d / _RENAME_PENDING_FILE).exists(), f"leftover crumb in {d}"
                )

    # --- happy path ------------------------------------------------------

    def test_rename_happy(self) -> None:
        self._seed()
        self.store.rename("old", "new")
        self._assert_clean_rename()

    def test_rename_default_refused_both_directions(self) -> None:
        with self.assertRaises(ValueError):
            self.store.rename("default", "x")
        with self.assertRaises(ValueError):
            self.store.rename("x", "default")

    def test_rename_missing_source(self) -> None:
        with self.assertRaises(ValueError):
            self.store.rename("nope", "new")

    def test_rename_target_exists(self) -> None:
        self._seed()
        (self.profiles_dir / "new").mkdir()
        with self.assertRaises(ValueError):
            self.store.rename("old", "new")

    # --- crash-window fault injection ------------------------------------

    def _fail_dir_rename(self):
        """Patch os.rename to fail ONLY the directory rename, not the atomic
        breadcrumb file write (which also renames a tmp file into place)."""
        real = os.rename

        def side(src, dst, *a, **k):
            if Path(src).is_dir():
                raise OSError("boom")
            return real(src, dst, *a, **k)

        return patch.object(ps_mod.os, "rename", side_effect=side)

    def test_crash_after_breadcrumb_before_os_rename(self) -> None:
        self._seed()
        with self._fail_dir_rename():
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        # Pre-rename crash: dir never moved, stale breadcrumb present.
        self.assertTrue((self.profiles_dir / "old" / _RENAME_PENDING_FILE).exists())
        self.store.recover_incomplete_renames()
        self._assert_clean_no_rename()

    def test_crash_after_os_rename_before_tokens(self) -> None:
        self._seed()
        with patch.object(self.store.token_store, "rename", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        self.assertTrue((self.profiles_dir / "new").is_dir())
        self.assertTrue((self.profiles_dir / "new" / _RENAME_PENDING_FILE).exists())
        self.store.recover_incomplete_renames()
        self._assert_clean_rename()

    def test_crash_after_tokens_before_options(self) -> None:
        self._seed()
        with patch.object(self.store.options, "rename_value", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        self.store.recover_incomplete_renames()
        self._assert_clean_rename()

    def test_crash_after_options_before_state(self) -> None:
        self._seed()
        with patch.object(self.store.state, "set_value", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        self.store.recover_incomplete_renames()
        self._assert_clean_rename()

    def test_crash_after_state_before_breadcrumb_removal(self) -> None:
        self._seed()
        real_unlink = Path.unlink

        def fail_crumb_unlink(self_path, *a, **k):
            if self_path.name == _RENAME_PENDING_FILE:
                raise OSError("boom")
            return real_unlink(self_path, *a, **k)

        with patch.object(Path, "unlink", fail_crumb_unlink):
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        # Everything moved; only the breadcrumb removal failed.
        self.assertTrue((self.profiles_dir / "new" / _RENAME_PENDING_FILE).exists())
        self.store.recover_incomplete_renames()
        self._assert_clean_rename()

    # --- recovery robustness ---------------------------------------------

    def test_recovery_idempotent(self) -> None:
        self._seed()
        self.store.rename("old", "new")
        first = self.store.recover_incomplete_renames()
        second = self.store.recover_incomplete_renames()
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self._assert_clean_rename()

    def test_recovery_reports_completed_action(self) -> None:
        self._seed()
        with patch.object(self.store.token_store, "rename", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        actions = self.store.recover_incomplete_renames()
        self.assertEqual(
            actions, [{"action": "completed", "from": "old", "to": "new"}]
        )

    def test_recovery_reports_reverted_action(self) -> None:
        self._seed()
        with self._fail_dir_rename():
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        actions = self.store.recover_incomplete_renames()
        self.assertEqual(
            actions, [{"action": "reverted", "from": "old", "to": "new"}]
        )

    def test_recovery_tolerates_malformed_breadcrumb(self) -> None:
        bad = self.profiles_dir / "weird"
        bad.mkdir(parents=True)
        (bad / _RENAME_PENDING_FILE).write_text("{not json")
        missing = self.profiles_dir / "partial"
        missing.mkdir(parents=True)
        (missing / _RENAME_PENDING_FILE).write_text(json.dumps({"from": "partial"}))

        actions = self.store.recover_incomplete_renames()
        reasons = {a["profile"]: a["reason"] for a in actions if a["action"] == "skipped"}
        self.assertEqual(reasons.get("weird"), "unparseable")
        self.assertEqual(reasons.get("partial"), "missing-fields")
        # Malformed breadcrumbs are left in place (tolerant, like the old code).
        self.assertTrue((bad / _RENAME_PENDING_FILE).exists())
        self.assertTrue((missing / _RENAME_PENDING_FILE).exists())

    # --- recovery through the REAL boundary ------------------------------

    def test_recovery_runs_via_appconfig_construction(self) -> None:
        """Recovery fires through the REAL boundary, not just a direct call.

        The crash-window tests above invoke ``recover_incomplete_renames()``
        directly. In production, recovery is triggered by constructing the app
        config store: ``AppConfigStore.__post_init__`` calls
        ``workspace.profiles.recover_incomplete_renames()``. This pins that
        wiring: seed an interrupted rename (dir moved, breadcrumb present,
        tokens/options/state still on 'old'), then build the store via
        ``Workspace.appconfig()`` and assert -- purely from the recovered on-disk
        state -- that the rename was rolled forward and the breadcrumb cleared.
        """
        self._seed()
        # Crash after the directory rename but before token migration: leaves the
        # 'new' dir with a pending breadcrumb; the roll-forward completes it.
        with patch.object(
            self.store.token_store, "rename", side_effect=OSError("boom")
        ):
            with self.assertRaises(OSError):
                self.store.rename("old", "new")
        self.assertTrue((self.profiles_dir / "new" / _RENAME_PENDING_FILE).exists())

        # REAL boundary: constructing the app config store runs recovery in
        # __post_init__ -- no direct recover_incomplete_renames() call here.
        Workspace.default().appconfig()

        # Assert via the recovered state -- only the migration-independent facts
        # that recovery itself owns (the store's __post_init__ migrations run
        # BEFORE recovery and legitimately drop the options 'metadata' section,
        # which is orthogonal to rename recovery, so _assert_clean_rename's
        # metadata check does not apply through this boundary):
        #   - directory rolled forward (new present, old gone)
        #   - no pending breadcrumb left anywhere
        #   - token entry migrated old -> new
        #   - last_config profile migrated old -> new
        self.assertTrue((self.profiles_dir / "new").is_dir())
        self.assertFalse((self.profiles_dir / "old").exists())
        self._assert_no_breadcrumbs()
        tokens = self._read_tokens()
        self.assertIn("new", tokens)
        self.assertNotIn("old", tokens)
        self.assertEqual(self._read_state()["last_config"]["profile"], "new")
