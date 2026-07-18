"""Tests for shared-settings.json drift detection and canonical source logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel import guardrail
from claudewheel.defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from claudewheel.health import check_shared_settings_drift


class _HomeDirTestCase(unittest.TestCase):
    """Base class that sets up a temp dir as Path.home() and patches it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patcher = patch.object(Path, "home", return_value=self.home)
        self._patcher.start()
        self._profiles_dir = self.home / ".claudewheel" / "profiles"
        self._shared_settings = self.home / ".claudewheel" / "shared-settings.json"
        self._scripts_dir = self.home / ".claudewheel" / "scripts"
        self._tokens_file = self.home / ".claudewheel" / "tokens.json"
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def _make_profile(self, name: str) -> Path:
        """Create a profile dir with .credentials.json and return its path."""
        pdir = self._profiles_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / ".credentials.json").write_text("{}")
        return pdir

    def _write_shared_settings(self, data: dict) -> None:
        """Write shared-settings.json in the temp home."""
        self._shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings.write_text(json.dumps(data, indent=2) + "\n")

    def _write_profile_settings(self, pdir: Path, settings: dict) -> None:
        """Write settings.json into a profile directory."""
        (pdir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    def _canonical(self) -> dict:
        """Return canonical shared settings using the test scripts dir."""
        return build_canonical_shared_settings(self._scripts_dir)


# ---------------------------------------------------------------------------
# check_shared_settings_drift
# ---------------------------------------------------------------------------


class CheckSharedSettingsDriftTests(_HomeDirTestCase):
    """Tests for check_shared_settings_drift(self.ws)."""

    def test_all_profiles_in_sync(self) -> None:
        """Returns OK when all profiles match shared-settings.json exactly."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("alpha")
        self._write_profile_settings(
            pdir,
            {
                "hooks": canonical["hooks"],
                "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
            },
        )
        pdir2 = self._make_profile("beta")
        self._write_profile_settings(
            pdir2,
            {
                "hooks": canonical["hooks"],
                "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
            },
        )

        result = check_shared_settings_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("2 profiles in sync", result.detail)

    def test_profile_missing_hook(self) -> None:
        """Drift detected when a profile is missing a hook entry."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("drifted")
        # Write hooks with hook-timestamp removed from UserPromptSubmit
        hooks = {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        # Missing hook-timestamp
                    ],
                }
            ]
        }
        self._write_profile_settings(
            pdir,
            {
                "hooks": hooks,
                "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
            },
        )

        result = check_shared_settings_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("drifted", result.detail)

    def test_profile_extra_hook(self) -> None:
        """Drift detected when a profile has an extra hook not in canonical."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("extra")
        # Add an extra hook command
        hooks = {
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        *canonical["hooks"]["UserPromptSubmit"][0]["hooks"],
                        {"type": "command", "command": "/usr/bin/extra-hook"},
                    ],
                }
            ]
        }
        self._write_profile_settings(
            pdir,
            {
                "hooks": hooks,
                "claudewheel": {"disallowedTools": canonical["disallowedTools"]},
            },
        )

        result = check_shared_settings_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("extra", result.detail)

    def test_disallowed_tools_mismatch(self) -> None:
        """Drift detected when disallowedTools differs from canonical."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("tools-off")
        # Remove one tool from the profile's disallowed list
        partial_tools = canonical["disallowedTools"][:-1]
        self._write_profile_settings(
            pdir,
            {
                "hooks": canonical["hooks"],
                "claudewheel": {"disallowedTools": partial_tools},
            },
        )

        result = check_shared_settings_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("tools-off", result.detail)
        self.assertIn("missing", result.detail)

    def test_shared_settings_missing(self) -> None:
        """Handles gracefully when shared-settings.json doesn't exist."""
        self._make_profile("lonely")

        result = check_shared_settings_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("not found", result.detail)

    def test_no_profiles(self) -> None:
        """Returns OK when no profiles exist."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        result = check_shared_settings_drift(self.ws)
        self.assertTrue(result.ok)
        self.assertIn("no profiles found", result.detail)

    def test_profile_no_settings_json(self) -> None:
        """Drift reported when a profile has no settings.json."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)
        self._make_profile("bare")

        result = check_shared_settings_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("bare: no settings.json", result.detail)

    def test_profile_extra_disallowed_tool(self) -> None:
        """Drift detected when a profile has extra tools not in canonical."""
        canonical = self._canonical()
        self._write_shared_settings(canonical)

        pdir = self._make_profile("surplus")
        extra_tools = canonical["disallowedTools"] + ["ExtraTool"]
        self._write_profile_settings(
            pdir,
            {
                "hooks": canonical["hooks"],
                "claudewheel": {"disallowedTools": extra_tools},
            },
        )

        result = check_shared_settings_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("surplus", result.detail)
        self.assertIn("ExtraTool", result.detail)

    def test_corrupt_shared_settings(self) -> None:
        """Returns failure when shared-settings.json is corrupt JSON."""
        self._shared_settings.parent.mkdir(parents=True, exist_ok=True)
        self._shared_settings.write_text("not valid json{{{")
        self._make_profile("victim")

        result = check_shared_settings_drift(self.ws)
        self.assertFalse(result.ok)
        self.assertIn("unreadable", result.detail)


# ---------------------------------------------------------------------------
# build_canonical_shared_settings
# ---------------------------------------------------------------------------


class BuildCanonicalSharedSettingsTests(unittest.TestCase):
    """Tests for build_canonical_shared_settings()."""

    def test_has_hooks_key(self) -> None:
        """Result contains a hooks dict with UserPromptSubmit and PreToolUse."""
        result = build_canonical_shared_settings(Path("/scripts"))
        self.assertIn("hooks", result)
        self.assertIn("UserPromptSubmit", result["hooks"])
        self.assertIn("PreToolUse", result["hooks"])

    def test_has_disallowed_tools(self) -> None:
        """Result contains the full DISALLOWED_TOOLS list."""
        result = build_canonical_shared_settings(Path("/scripts"))
        self.assertEqual(result["disallowedTools"], DISALLOWED_TOOLS)

    def test_hooks_reference_scripts_dir(self) -> None:
        """Hook commands reference the provided scripts_dir."""
        scripts = Path("/my/scripts")
        result = build_canonical_shared_settings(scripts)
        ups_hooks = result["hooks"]["UserPromptSubmit"][0]["hooks"]
        self.assertTrue(any("hook-timestamp" in h["command"] for h in ups_hooks))
        for h in ups_hooks:
            self.assertTrue(h["command"].startswith(str(scripts)))
        # Agent matcher
        ptu_agent = result["hooks"]["PreToolUse"][0]
        self.assertEqual(ptu_agent["matcher"], "Agent")
        self.assertTrue(
            any("hook-block-worktree" in h["command"] for h in ptu_agent["hooks"])
        )
        for h in ptu_agent["hooks"]:
            self.assertTrue(h["command"].startswith(str(scripts)))

    def test_pretooluse_bash_matcher_present(self) -> None:
        """PreToolUse contains a Bash matcher entry with hook-block-unsafe-commands."""
        scripts = Path("/my/scripts")
        result = build_canonical_shared_settings(scripts)
        ptu_list = result["hooks"]["PreToolUse"]
        # Find the Bash matcher entry
        bash_entries = [e for e in ptu_list if e.get("matcher") == "Bash"]
        self.assertEqual(
            len(bash_entries), 1, "Expected exactly one Bash matcher entry"
        )
        bash_entry = bash_entries[0]
        hook_commands = [h["command"] for h in bash_entry["hooks"]]
        self.assertTrue(
            any("hook-block-unsafe-commands" in cmd for cmd in hook_commands),
            f"Expected hook-block-unsafe-commands in Bash hooks, got: {hook_commands}",
        )
        # Verify it references the scripts dir
        for cmd in hook_commands:
            self.assertTrue(cmd.startswith(str(scripts)))

    def test_disallowed_tools_is_copy(self) -> None:
        """Returned disallowedTools is a copy, not the original list."""
        result = build_canonical_shared_settings(Path("/scripts"))
        self.assertIsNot(result["disallowedTools"], DISALLOWED_TOOLS)

    # -- canonical permissions derive from the guardrail model --------------

    def test_deny_matches_guardrail_model(self) -> None:
        """profileDefaults.permissions.deny is exactly canonical_deny_rules()."""
        result = build_canonical_shared_settings(Path("/scripts"))
        deny = result["profileDefaults"]["permissions"]["deny"]
        self.assertEqual(deny, guardrail.canonical_deny_rules())

    def test_ask_matches_guardrail_model(self) -> None:
        """profileDefaults.permissions.ask is exactly canonical_ask_rules()."""
        result = build_canonical_shared_settings(Path("/scripts"))
        ask = result["profileDefaults"]["permissions"]["ask"]
        self.assertEqual(ask, guardrail.canonical_ask_rules())

    def test_deny_starts_with_rm_hard_deny(self) -> None:
        """The first deny entry is the rm hard-deny rule (model order)."""
        result = build_canonical_shared_settings(Path("/scripts"))
        deny = result["profileDefaults"]["permissions"]["deny"]
        self.assertEqual(deny[0], "Bash(rm:*)")

    def test_ask_ends_with_sudo(self) -> None:
        """The last ask entry is the sudo prompt (model order)."""
        result = build_canonical_shared_settings(Path("/scripts"))
        ask = result["profileDefaults"]["permissions"]["ask"]
        self.assertEqual(ask[-1], "Bash(sudo:*)")

    def test_old_rm_chain_kill_pkill_ask_entries_gone(self) -> None:
        """The old literal rm-chain and kill/pkill ask entries are removed.

        rm now lives in the deny array (hard-deny), and kill/pkill are handled
        by the advise-tier PostToolUse hook, not a settings ask rule.
        """
        result = build_canonical_shared_settings(Path("/scripts"))
        ask = result["profileDefaults"]["permissions"]["ask"]
        for gone in (
            "Bash(rm:*)",
            "Bash(*&& rm:*)",
            "Bash(*; rm:*)",
            "Bash(*| rm:*)",
            "Bash(*| xargs rm:*)",
            "Bash(kill:*)",
            "Bash(pkill:*)",
        ):
            self.assertNotIn(gone, ask)

    def test_hooks_include_posttooluse_advise_wiring(self) -> None:
        """PostToolUse wires hook-advise-commands on the Bash matcher, alongside
        the original UserPromptSubmit and PreToolUse (Agent/Bash) wirings."""
        scripts = Path("/my/scripts")
        result = build_canonical_shared_settings(scripts)
        hooks = result["hooks"]

        # Original three still present.
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PreToolUse", hooks)
        pre_matchers = {e["matcher"] for e in hooks["PreToolUse"]}
        self.assertEqual(pre_matchers, {"Agent", "Bash"})

        # New PostToolUse advise wiring.
        self.assertIn("PostToolUse", hooks)
        post = hooks["PostToolUse"]
        bash_entries = [e for e in post if e.get("matcher") == "Bash"]
        self.assertEqual(len(bash_entries), 1)
        cmds = [h["command"] for h in bash_entries[0]["hooks"]]
        self.assertTrue(any("hook-advise-commands" in c for c in cmds))
        for c in cmds:
            self.assertTrue(c.startswith(str(scripts)))

    def test_hooks_match_all_expected_wirings(self) -> None:
        """Every guardrail.EXPECTED_HOOK_WIRINGS tuple is present in the hooks."""
        scripts = Path("/my/scripts")
        hooks = build_canonical_shared_settings(scripts)["hooks"]
        for event, matcher, script in guardrail.EXPECTED_HOOK_WIRINGS:
            entries = hooks.get(event, [])
            entry = next((e for e in entries if e.get("matcher") == matcher), None)
            self.assertIsNotNone(entry, f"missing {event}[{matcher}] wiring")
            cmds = [h["command"] for h in entry["hooks"]]
            self.assertTrue(
                any(c == str(scripts / script) for c in cmds),
                f"{event}[{matcher}] missing {script}",
            )

    def test_permissions_are_copies_not_model_references(self) -> None:
        """Mutating the returned deny/ask must not mutate the guardrail model."""
        result = build_canonical_shared_settings(Path("/scripts"))
        perms = result["profileDefaults"]["permissions"]
        perms["deny"].append("Bash(SENTINEL-DENY)")
        perms["ask"].append("Bash(SENTINEL-ASK)")
        self.assertNotIn("Bash(SENTINEL-DENY)", guardrail.canonical_deny_rules())
        self.assertNotIn("Bash(SENTINEL-ASK)", guardrail.canonical_ask_rules())
        # A fresh build is likewise unpolluted.
        fresh = build_canonical_shared_settings(Path("/scripts"))
        self.assertNotIn(
            "Bash(SENTINEL-DENY)",
            fresh["profileDefaults"]["permissions"]["deny"],
        )


if __name__ == "__main__":
    unittest.main()
