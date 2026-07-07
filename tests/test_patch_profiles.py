"""Tests for the patch-profiles command and its additive sync helpers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claudewheel import cli
from claudewheel.defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from claudewheel.patch_profiles import (
    merge_hooks,
    run_patch_profiles,
    sync_profile_settings,
    sync_shared_settings,
)

# The three disallowedTools entries most recently added to canonical.
_NEW_TOOLS = ["Artifact", "DesignSync", "ReportFindings"]


class _PatchProfilesTestCase(unittest.TestCase):
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
        self.scripts_dir = cw / "scripts"
        self.shared_settings = cw / "shared-settings.json"
        self.tokens_file = cw / "tokens.json"

        patches = [
            patch("claudewheel.patch_profiles.SCRIPTS_DIR", self.scripts_dir),
            patch("claudewheel.patch_profiles.SHARED_SETTINGS_FILE", self.shared_settings),
            patch("claudewheel.discovery.PROFILES_DIR", self.profiles_dir),
            patch("claudewheel.discovery.TOKENS_FILE", self.tokens_file),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- fixture helpers ---------------------------------------------------

    def canonical(self) -> dict:
        return build_canonical_shared_settings(self.scripts_dir)

    def make_profile(self, name: str, settings: dict) -> Path:
        pdir = self.profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        (pdir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        return pdir

    def read_settings(self, name: str) -> dict:
        return json.loads((self.profiles_dir / name / "settings.json").read_text())

    def stale_profile_settings(self) -> dict:
        """A profile mirroring the pre-patch live state: has hook-timestamp and
        the Agent worktree hook, but no Bash hook and missing the 3 new tools."""
        c = self.canonical()
        pretooluse = [c["hooks"]["PreToolUse"][0]]  # Agent entry only, no Bash
        tools = [t for t in DISALLOWED_TOOLS if t not in _NEW_TOOLS]
        return {
            "awaySummaryEnabled": False,
            "cleanupPeriodDays": 3650,
            "autoMemoryEnabled": False,
            "permissions": {
                "deny": ["a", "b", "c", "d", "e"],
                "ask": ["w", "x", "y", "z"],
                "disableAutoMode": "disable",
            },
            "hooks": {
                "UserPromptSubmit": c["hooks"]["UserPromptSubmit"],
                "PreToolUse": pretooluse,
            },
            "claudewheel": {"disallowedTools": tools},
        }

    def _run_patch(self, dry_run: bool = False) -> str:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = run_patch_profiles(dry_run=dry_run)
        self.assertEqual(rc, 0)
        return out.getvalue()


# ---------------------------------------------------------------------------
# merge_hooks / sync helpers (pure functions)
# ---------------------------------------------------------------------------


class MergeHooksTests(_PatchProfilesTestCase):
    def test_appends_missing_bash_entry(self) -> None:
        c = self.canonical()
        existing = {
            "UserPromptSubmit": c["hooks"]["UserPromptSubmit"],
            "PreToolUse": [c["hooks"]["PreToolUse"][0]],  # Agent only
        }
        added = merge_hooks(existing, c["hooks"])
        self.assertEqual(len(added), 1)
        self.assertIn("hook-block-unsafe-commands", added[0])
        matchers = [e.get("matcher") for e in existing["PreToolUse"]]
        self.assertIn("Bash", matchers)

    def test_idempotent_when_already_present(self) -> None:
        c = self.canonical()
        existing = json.loads(json.dumps(c["hooks"]))
        self.assertEqual(merge_hooks(existing, c["hooks"]), [])

    def test_preserves_user_added_hook(self) -> None:
        c = self.canonical()
        custom = {"type": "command", "command": "/opt/mine/custom-hook"}
        existing = json.loads(json.dumps(c["hooks"]))
        existing["UserPromptSubmit"][0]["hooks"].append(custom)
        merge_hooks(existing, c["hooks"])
        cmds = [h["command"] for h in existing["UserPromptSubmit"][0]["hooks"]]
        self.assertIn("/opt/mine/custom-hook", cmds)

    def test_dedups_by_basename_across_different_path(self) -> None:
        """A hook already wired under a different absolute path is not duplicated."""
        c = self.canonical()
        existing = {
            "UserPromptSubmit": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "/old/scripts/hook-timestamp"},
                ]},
            ],
        }
        added = merge_hooks(existing, {"UserPromptSubmit": c["hooks"]["UserPromptSubmit"]})
        self.assertEqual(added, [])
        self.assertEqual(len(existing["UserPromptSubmit"][0]["hooks"]), 1)


class SyncProfileSettingsTests(_PatchProfilesTestCase):
    def test_adds_missing_tools(self) -> None:
        c = self.canonical()
        settings = self.stale_profile_settings()
        changes = sync_profile_settings(settings, c)
        got = settings["claudewheel"]["disallowedTools"]
        for tool in _NEW_TOOLS:
            self.assertIn(tool, got)
        self.assertTrue(any("hook-block-unsafe-commands" in ch for ch in changes))

    def test_preserves_user_extra_tool(self) -> None:
        c = self.canonical()
        settings = self.stale_profile_settings()
        settings["claudewheel"]["disallowedTools"].append("MyCustomTool")
        sync_profile_settings(settings, c)
        self.assertIn("MyCustomTool", settings["claudewheel"]["disallowedTools"])

    def test_folds_and_removes_inert_top_level_key(self) -> None:
        c = self.canonical()
        settings = self.stale_profile_settings()
        settings["disallowedTools"] = ["InertOnly"]
        changes = sync_profile_settings(settings, c)
        self.assertNotIn("disallowedTools", settings)
        self.assertIn("InertOnly", settings["claudewheel"]["disallowedTools"])
        self.assertTrue(any("removed inert top-level" in ch for ch in changes))

    def test_idempotent(self) -> None:
        c = self.canonical()
        settings = self.stale_profile_settings()
        sync_profile_settings(settings, c)
        self.assertEqual(sync_profile_settings(settings, c), [])

    def test_permissions_untouched(self) -> None:
        c = self.canonical()
        settings = self.stale_profile_settings()
        before = json.loads(json.dumps(settings["permissions"]))
        sync_profile_settings(settings, c)
        self.assertEqual(settings["permissions"], before)


class SyncSharedSettingsTests(_PatchProfilesTestCase):
    def test_adds_missing_top_level_tools(self) -> None:
        c = self.canonical()
        shared = json.loads(json.dumps(c))
        shared["disallowedTools"] = [t for t in DISALLOWED_TOOLS if t not in _NEW_TOOLS]
        changes = sync_shared_settings(shared, c)
        for tool in _NEW_TOOLS:
            self.assertIn(tool, shared["disallowedTools"])
        self.assertTrue(changes)

    def test_idempotent(self) -> None:
        c = self.canonical()
        shared = json.loads(json.dumps(c))
        self.assertEqual(sync_shared_settings(shared, c), [])


# ---------------------------------------------------------------------------
# run_patch_profiles (end to end)
# ---------------------------------------------------------------------------


class RunPatchProfilesTests(_PatchProfilesTestCase):
    def test_patches_stale_profile_and_shared(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        stale_shared = self.canonical()
        stale_shared["disallowedTools"] = [
            t for t in DISALLOWED_TOOLS if t not in _NEW_TOOLS
        ]
        self.shared_settings.write_text(json.dumps(stale_shared, indent=2) + "\n")
        self.make_profile("work", self.stale_profile_settings())

        out = self._run_patch()

        self.assertIn("work: updated", out)
        self.assertIn("shared-settings.json: updated", out)
        # Profile now has Bash hook and all tools.
        s = self.read_settings("work")
        matchers = [e.get("matcher") for e in s["hooks"]["PreToolUse"]]
        self.assertIn("Bash", matchers)
        self.assertTrue(set(DISALLOWED_TOOLS).issubset(s["claudewheel"]["disallowedTools"]))
        # Shared settings now a superset too.
        shared = json.loads(self.shared_settings.read_text())
        self.assertTrue(set(DISALLOWED_TOOLS).issubset(shared["disallowedTools"]))

    def test_deploys_missing_hook_scripts(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self.shared_settings.write_text(json.dumps(self.canonical(), indent=2) + "\n")
        self.make_profile("work", self.stale_profile_settings())
        self.assertFalse(self.scripts_dir.exists())

        self._run_patch()

        for name in ("hook-timestamp", "hook-block-worktree", "hook-block-unsafe-commands"):
            self.assertTrue((self.scripts_dir / name).exists(), f"{name} should be deployed")

    def test_idempotent_second_run_no_changes(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self.shared_settings.write_text(json.dumps(self.canonical(), indent=2) + "\n")
        self.make_profile("work", self.stale_profile_settings())

        self._run_patch()  # first run patches
        settings_file = self.profiles_dir / "work" / "settings.json"
        content_after_first = settings_file.read_text()

        out = self._run_patch()  # second run
        self.assertIn("work: already up to date", out)
        self.assertIn("Everything already up to date.", out)
        self.assertEqual(settings_file.read_text(), content_after_first)

    def test_dry_run_writes_nothing(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        stale_shared = self.canonical()
        stale_shared["disallowedTools"] = [
            t for t in DISALLOWED_TOOLS if t not in _NEW_TOOLS
        ]
        self.shared_settings.write_text(json.dumps(stale_shared, indent=2) + "\n")
        pdir = self.make_profile("work", self.stale_profile_settings())
        settings_file = pdir / "settings.json"
        before_profile = settings_file.read_text()
        before_shared = self.shared_settings.read_text()

        out = self._run_patch(dry_run=True)

        self.assertIn("would update", out)
        self.assertIn("Dry run: no files were written.", out)
        self.assertEqual(settings_file.read_text(), before_profile)
        self.assertEqual(self.shared_settings.read_text(), before_shared)
        # Hook scripts must not be deployed under dry-run either.
        self.assertFalse((self.scripts_dir / "hook-timestamp").exists())

    def test_preserves_user_extras_end_to_end(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self.shared_settings.write_text(json.dumps(self.canonical(), indent=2) + "\n")
        settings = self.stale_profile_settings()
        settings["claudewheel"]["disallowedTools"].append("MyCustomTool")
        settings["hooks"]["UserPromptSubmit"][0]["hooks"].append(
            {"type": "command", "command": "/opt/mine/extra"}
        )
        self.make_profile("work", settings)

        self._run_patch()

        s = self.read_settings("work")
        self.assertIn("MyCustomTool", s["claudewheel"]["disallowedTools"])
        cmds = [h["command"] for h in s["hooks"]["UserPromptSubmit"][0]["hooks"]]
        self.assertIn("/opt/mine/extra", cmds)

    def test_no_profiles(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self.shared_settings.write_text(json.dumps(self.canonical(), indent=2) + "\n")
        out = self._run_patch()
        self.assertIn("no profiles found", out)


class PatchProfilesCliTests(_PatchProfilesTestCase):
    """Route through cli.main() to confirm the command is wired up."""

    def _run_cli(self, argv: list[str]) -> tuple[str, bool]:
        out = io.StringIO()
        exited = False
        with patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                cli.main()
            except SystemExit:
                exited = True
        return out.getvalue(), exited

    def test_command_registered_and_dry_run(self) -> None:
        self.shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self.shared_settings.write_text(json.dumps(self.canonical(), indent=2) + "\n")
        self.make_profile("work", self.stale_profile_settings())

        out, _ = self._run_cli(["c", "patch-profiles", "--dry-run"])
        self.assertIn("would update", out)
        # Nothing written.
        s = self.read_settings("work")
        matchers = [e.get("matcher") for e in s["hooks"]["PreToolUse"]]
        self.assertNotIn("Bash", matchers)


if __name__ == "__main__":
    unittest.main()
