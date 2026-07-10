"""Tests for the reconcile-permissions command and its diff/apply helpers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claudewheel import cli
from claudewheel.guardrail import (
    ALLOW_CONFLICTS,
    canonical_ask_rules,
    canonical_deny_rules,
)
from claudewheel.reconcile import (
    apply_settings_diff,
    compute_settings_diff,
    run_reconcile,
)


class _ReconcileTestCase(unittest.TestCase):
    """Base: temp home with patched path constants across the modules used."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._home_patch = patch.object(Path, "home", return_value=self.home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

        cw = self.home / ".claudewheel"
        self.profiles_dir = cw / "profiles"
        self.shared_settings = cw / "shared-settings.json"
        self.tokens_file = cw / "tokens.json"
        cw.mkdir(parents=True, exist_ok=True)

        from claudewheel.workspace import Workspace
        self.ws = Workspace.open(cw, claude_dir=self.home / ".claude")

    # -- fixture helpers ---------------------------------------------------

    def make_profile(self, name: str, settings: dict) -> Path:
        pdir = self.profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        (pdir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return pdir

    def settings_path(self, name: str) -> Path:
        return self.profiles_dir / name / "settings.json"

    def read_settings(self, name: str) -> dict:
        return json.loads(self.settings_path(name).read_text())

    def drifted_settings(self) -> dict:
        """A profile whose permissions have drifted from canonical.

        - deny has one canonical entry present (git stash) plus one bogus extra,
          and is missing the rest of the canonical deny set.
        - ask has non-canonical entries (kill, pkill) and none of the canonical
          ask set.
        - allow mixes a dead/conflicting entry (git stash:*) with two entries
          that must survive (git rm:*, npm run kill:*).
        - hooks and an unrelated top-level key exist to prove they are untouched.
        """
        return {
            "cleanupPeriodDays": 3650,
            "permissions": {
                "deny": ["Bash(git stash:*)", "Bash(bogus:*)"],
                "ask": ["kill", "pkill"],
                "allow": [
                    "Bash(git stash:*)",
                    "Bash(git rm:*)",
                    "Bash(npm run kill:*)",
                ],
                "defaultMode": "default",
            },
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
            "claudewheel": {"disallowedTools": ["Artifact"]},
        }

    def canonical_settings(self) -> dict:
        """A profile already exactly canonical (a clean no-op target)."""
        return {
            "permissions": {
                "deny": list(canonical_deny_rules()),
                "ask": list(canonical_ask_rules()),
                "allow": ["Bash(git rm:*)"],
            },
        }

    def write_shared(self, deny: list[str], ask: list[str]) -> None:
        self.shared_settings.write_text(
            json.dumps(
                {
                    "hooks": {"UserPromptSubmit": []},
                    "disallowedTools": ["Artifact"],
                    "profileDefaults": {
                        "cleanupPeriodDays": 3650,
                        "permissions": {
                            "deny": deny,
                            "ask": ask,
                            "defaultMode": "default",
                        },
                    },
                },
                indent=2,
            )
            + "\n"
        )

    def _run(self, dry_run: bool, profile: str | None = None) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = run_reconcile(self.ws, dry_run=dry_run, profile=profile)
        self.assertEqual(rc, 0)
        return out.getvalue()


# ---------------------------------------------------------------------------
# compute_settings_diff / apply_settings_diff (pure functions)
# ---------------------------------------------------------------------------


class ComputeDiffTests(_ReconcileTestCase):
    def test_drifted_diff_add_and_remove_sets(self) -> None:
        diff = compute_settings_diff(self.drifted_settings())

        # deny: git stash already present (not re-added); every other canonical
        # deny is added; the bogus extra is removed.
        self.assertNotIn("Bash(git stash:*)", diff.deny_add)
        self.assertIn("Bash(rm:*)", diff.deny_add)
        self.assertEqual(diff.deny_remove, ["Bash(bogus:*)"])
        expected_deny_add = [
            r for r in canonical_deny_rules() if r != "Bash(git stash:*)"
        ]
        self.assertEqual(diff.deny_add, expected_deny_add)

        # ask: kill/pkill removed; full canonical ask set added.
        self.assertEqual(diff.ask_remove, ["kill", "pkill"])
        self.assertEqual(diff.ask_add, list(canonical_ask_rules()))

        # allow: only the dead conflicting entry is scheduled for removal.
        self.assertEqual(diff.allow_remove, ["Bash(git stash:*)"])

    def test_add_preserves_canonical_order(self) -> None:
        diff = compute_settings_diff({"permissions": {"deny": [], "ask": []}})
        self.assertEqual(diff.deny_add, list(canonical_deny_rules()))
        self.assertEqual(diff.ask_add, list(canonical_ask_rules()))

    def test_canonical_profile_is_empty_diff(self) -> None:
        diff = compute_settings_diff(self.canonical_settings())
        self.assertTrue(diff.is_empty())

    def test_missing_permissions_block(self) -> None:
        diff = compute_settings_diff({})
        self.assertEqual(diff.deny_add, list(canonical_deny_rules()))
        self.assertEqual(diff.ask_add, list(canonical_ask_rules()))
        self.assertEqual(diff.deny_remove, [])
        self.assertEqual(diff.allow_remove, [])

    def test_no_allow_array_means_no_allow_removals(self) -> None:
        diff = compute_settings_diff(
            {"permissions": {"deny": list(canonical_deny_rules()),
                             "ask": list(canonical_ask_rules())}}
        )
        self.assertEqual(diff.allow_remove, [])
        self.assertTrue(diff.is_empty())

    def test_every_allow_conflict_is_removed(self) -> None:
        diff = compute_settings_diff(
            {"permissions": {"allow": list(ALLOW_CONFLICTS) + ["Bash(git rm:*)"]}}
        )
        self.assertEqual(diff.allow_remove, list(ALLOW_CONFLICTS))


class ApplyDiffTests(_ReconcileTestCase):
    def test_apply_makes_deny_and_ask_exactly_canonical(self) -> None:
        settings = self.drifted_settings()
        diff = compute_settings_diff(settings)
        apply_settings_diff(settings, diff)

        self.assertEqual(
            set(settings["permissions"]["deny"]), set(canonical_deny_rules())
        )
        self.assertEqual(len(settings["permissions"]["deny"]), len(canonical_deny_rules()))
        self.assertEqual(
            set(settings["permissions"]["ask"]), set(canonical_ask_rules())
        )
        self.assertEqual(len(settings["permissions"]["ask"]), len(canonical_ask_rules()))

    def test_apply_removes_conflicts_keeps_other_allow(self) -> None:
        settings = self.drifted_settings()
        apply_settings_diff(settings, compute_settings_diff(settings))
        allow = settings["permissions"]["allow"]
        self.assertNotIn("Bash(git stash:*)", allow)
        self.assertIn("Bash(git rm:*)", allow)
        self.assertIn("Bash(npm run kill:*)", allow)

    def test_apply_leaves_hooks_and_other_keys_untouched(self) -> None:
        settings = self.drifted_settings()
        hooks_before = json.loads(json.dumps(settings["hooks"]))
        cw_before = json.loads(json.dumps(settings["claudewheel"]))
        cleanup_before = settings["cleanupPeriodDays"]
        mode_before = settings["permissions"]["defaultMode"]

        apply_settings_diff(settings, compute_settings_diff(settings))

        self.assertEqual(settings["hooks"], hooks_before)
        self.assertEqual(settings["claudewheel"], cw_before)
        self.assertEqual(settings["cleanupPeriodDays"], cleanup_before)
        self.assertEqual(settings["permissions"]["defaultMode"], mode_before)


# ---------------------------------------------------------------------------
# run_reconcile (end to end)
# ---------------------------------------------------------------------------


class RunReconcileTests(_ReconcileTestCase):
    def test_apply_reconciles_profile_and_shared(self) -> None:
        self.make_profile("work", self.drifted_settings())
        self.write_shared(deny=[], ask=["extra"])

        out = self._run(dry_run=False)

        self.assertIn("work: reconciled", out)
        self.assertIn("shared-settings.json profileDefaults: reconciled", out)

        s = self.read_settings("work")
        self.assertEqual(set(s["permissions"]["deny"]), set(canonical_deny_rules()))
        self.assertEqual(set(s["permissions"]["ask"]), set(canonical_ask_rules()))
        self.assertNotIn("Bash(git stash:*)", s["permissions"]["allow"])
        self.assertEqual(s["hooks"], {"PreToolUse": [{"matcher": "Bash", "hooks": []}]})

        pd = json.loads(self.shared_settings.read_text())["profileDefaults"]
        self.assertEqual(set(pd["permissions"]["deny"]), set(canonical_deny_rules()))
        self.assertEqual(set(pd["permissions"]["ask"]), set(canonical_ask_rules()))
        # profileDefaults has no allow array; it must not gain one.
        self.assertNotIn("allow", pd["permissions"])

    def test_idempotent_second_apply_no_changes_and_byte_identical(self) -> None:
        self.make_profile("work", self.drifted_settings())
        self.write_shared(deny=[], ask=["extra"])

        self._run(dry_run=False)  # first apply
        first_profile = self.settings_path("work").read_text()
        first_shared = self.shared_settings.read_text()

        out = self._run(dry_run=False)  # second apply
        self.assertIn("work: already canonical, no changes", out)
        self.assertIn("shared-settings.json profileDefaults: already canonical", out)
        self.assertIn("Everything already canonical.", out)
        self.assertEqual(self.settings_path("work").read_text(), first_profile)
        self.assertEqual(self.shared_settings.read_text(), first_shared)

    def test_dry_run_changes_nothing_but_reports_diff(self) -> None:
        pdir = self.make_profile("work", self.drifted_settings())
        self.write_shared(deny=[], ask=["extra"])
        before_profile = (pdir / "settings.json").read_text()
        before_shared = self.shared_settings.read_text()

        out = self._run(dry_run=True)

        self.assertIn("work: would reconcile", out)
        self.assertIn("deny  +Bash(rm:*)", out)
        self.assertIn("allow -Bash(git stash:*)", out)
        self.assertIn("no files were written", out)
        self.assertEqual((pdir / "settings.json").read_text(), before_profile)
        self.assertEqual(self.shared_settings.read_text(), before_shared)

    def test_shared_profiledefaults_reconciled(self) -> None:
        self.write_shared(deny=["Bash(bogus:*)"], ask=[])
        out = self._run(dry_run=False)
        self.assertIn("shared-settings.json profileDefaults: reconciled", out)
        pd = json.loads(self.shared_settings.read_text())["profileDefaults"]
        self.assertEqual(set(pd["permissions"]["deny"]), set(canonical_deny_rules()))
        self.assertNotIn("Bash(bogus:*)", pd["permissions"]["deny"])

    def test_profile_scoping_touches_only_named_profile_and_skips_shared(self) -> None:
        self.make_profile("work", self.drifted_settings())
        self.make_profile("play", self.drifted_settings())
        self.write_shared(deny=[], ask=["extra"])
        shared_before = self.shared_settings.read_text()
        play_before = self.settings_path("play").read_text()

        out = self._run(dry_run=False, profile="work")

        self.assertIn("work: reconciled", out)
        self.assertNotIn("play", out)
        self.assertNotIn("shared-settings", out)
        # work changed; play and shared untouched.
        w = self.read_settings("work")
        self.assertEqual(set(w["permissions"]["deny"]), set(canonical_deny_rules()))
        self.assertEqual(self.settings_path("play").read_text(), play_before)
        self.assertEqual(self.shared_settings.read_text(), shared_before)

    def test_scoped_missing_profile_is_error(self) -> None:
        self.make_profile("work", self.drifted_settings())
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = run_reconcile(self.ws, dry_run=True, profile="nope")
        self.assertEqual(rc, 1)
        self.assertIn("not found", err.getvalue())

    def test_already_canonical_profile_is_clean_noop(self) -> None:
        pdir = self.make_profile("work", self.canonical_settings())
        before = (pdir / "settings.json").read_text()
        # No shared-settings file present.
        out = self._run(dry_run=False)
        self.assertIn("work: already canonical, no changes", out)
        self.assertEqual((pdir / "settings.json").read_text(), before)

    def test_no_profiles_and_no_shared(self) -> None:
        out = self._run(dry_run=False)
        self.assertIn("no profiles found", out)
        self.assertIn("shared-settings.json: not found, skipping", out)


# ---------------------------------------------------------------------------
# CLI wiring and mandatory-flag enforcement
# ---------------------------------------------------------------------------


class ReconcileCliTests(_ReconcileTestCase):
    def _run_cli(self, argv: list[str]) -> tuple[str, str, int | None]:
        out = io.StringIO()
        err = io.StringIO()
        code: int | None = None
        with patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return out.getvalue(), err.getvalue(), code

    def test_command_registered_dry_run(self) -> None:
        self.make_profile("work", self.drifted_settings())
        out, _, _ = self._run_cli(["c", "reconcile-permissions", "--dry-run"])
        self.assertIn("would reconcile", out)
        # Nothing written under dry-run.
        s = self.read_settings("work")
        self.assertIn("Bash(bogus:*)", s["permissions"]["deny"])

    def test_command_apply_writes(self) -> None:
        self.make_profile("work", self.drifted_settings())
        out, _, _ = self._run_cli(["c", "reconcile-permissions", "--apply"])
        self.assertIn("reconciled", out)
        s = self.read_settings("work")
        self.assertEqual(set(s["permissions"]["deny"]), set(canonical_deny_rules()))

    def test_neither_flag_is_hard_error(self) -> None:
        self.make_profile("work", self.drifted_settings())
        out, err, code = self._run_cli(["c", "reconcile-permissions"])
        self.assertNotEqual(code, 0)
        self.assertIn("exactly one of --dry-run or --apply", err)
        # Nothing written.
        s = self.read_settings("work")
        self.assertIn("Bash(bogus:*)", s["permissions"]["deny"])

    def test_both_flags_is_hard_error(self) -> None:
        self.make_profile("work", self.drifted_settings())
        out, err, code = self._run_cli(
            ["c", "reconcile-permissions", "--dry-run", "--apply"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("exactly one of --dry-run or --apply", err)
        s = self.read_settings("work")
        self.assertIn("Bash(bogus:*)", s["permissions"]["deny"])

    def test_scoped_profile_flag(self) -> None:
        self.make_profile("work", self.drifted_settings())
        self.make_profile("play", self.drifted_settings())
        play_before = self.settings_path("play").read_text()
        out, _, _ = self._run_cli(
            ["c", "reconcile-permissions", "--apply", "--profile", "work"]
        )
        self.assertIn("work: reconciled", out)
        self.assertEqual(self.settings_path("play").read_text(), play_before)


if __name__ == "__main__":
    unittest.main()
