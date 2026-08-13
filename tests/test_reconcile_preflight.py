"""Phase 1.2: the always-on preflight reconcile step heals the guardrail
surface on every launch.

These drive the REAL ``_do_launch_sequence`` (PREFLIGHT_STEPS is NOT patched,
so the registered ``reconcile-guardrails`` step runs) with the launch/exec
boundary stubbed, and assert that:

  - injected drift is healed to canonical on disk after a launch;
  - a second launch performs no writes (idempotent, byte-identical);
  - a discoverable ``~/.claude`` (the "default" profile) is never touched;
  - a managed profile's ``.credentials.json`` is byte-identical after a launch
    that rewrote ``settings.json`` right beside it.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import cli
from claudewheel.defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from claudewheel.guardrail import canonical_ask_rules, canonical_deny_rules
from tests.wheelhelpers import (
    FakeAppConfigStore,
    build_profile_dir,
    claude_dir_write_canary,
    hash_snapshot,
)


def _drifted_profile_settings() -> dict[str, Any]:
    return {
        "cleanupPeriodDays": 3650,
        "permissions": {
            "deny": ["Bash(git stash:*)", "Bash(bogus:*)"],
            "ask": ["kill"],
            "allow": ["Bash(git stash:*)", "Bash(git rm:*)"],
        },
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]},
        "claudewheel": {"disallowedTools": ["Artifact"]},
    }


def _drifted_shared_settings() -> dict[str, Any]:
    return {
        "hooks": {"UserPromptSubmit": []},
        "disallowedTools": ["Artifact"],
        "profileDefaults": {
            "cleanupPeriodDays": 3650,
            "permissions": {"deny": [], "ask": ["extra"], "defaultMode": "default"},
        },
    }


class PreflightReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._home_patch = mock.patch.object(
            Path, "home", autospec=True, return_value=self.home
        )
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

        self.cw = self.home / ".claudewheel"
        self.profiles_dir = self.cw / "profiles"
        self.shared_settings = self.cw / "shared-settings.json"
        self.claude_dir = self.home / ".claude"
        self.cw.mkdir(parents=True, exist_ok=True)

        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(self.cw, claude_dir=self.claude_dir)

        # A hooks-free target directory so the approved-hooks preflight step is a
        # no-op here (these tests exercise the reconcile step, not hook approval).
        self.project = self.home / "project"
        self.project.mkdir(parents=True, exist_ok=True)

    # -- fixtures ----------------------------------------------------------

    def _make_profile(self, name: str, settings: dict[str, Any]) -> Path:
        return build_profile_dir(
            self.profiles_dir,
            name,
            parents=True,
            exist_ok=True,
            credentials=True,
            settings=settings,
        )

    def _make_default_claude(self, settings: dict[str, Any]) -> Path:
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        (self.claude_dir / ".credentials.json").write_text("{}")
        sp = self.claude_dir / "settings.json"
        sp.write_text(json.dumps(settings, indent=2) + "\n")
        return sp

    def _canonical_hooks(self) -> dict[str, Any]:
        hooks: dict[str, Any] = build_canonical_shared_settings(self.ws.scripts_dir)[
            "hooks"
        ]
        return hooks

    def _read(self, name: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(
            (self.profiles_dir / name / "settings.json").read_text()
        )
        return data

    # -- launch driver -----------------------------------------------------

    def _launch(self, interactive: bool = True) -> None:
        """Drive the REAL _do_launch_sequence with the exec boundary stubbed."""
        out = io.StringIO()
        with (
            # Every reconcile-driven launch here writes only managed profiles and
            # shared-settings under ~/.claudewheel; the discoverable ~/.claude
            # must stay untouched. The canary enforces that at the fsutil seam,
            # complementing the byte-identical snapshot assertions below.
            claude_dir_write_canary(self.claude_dir),
            mock.patch("claudewheel.hooks.run_hooks", autospec=True, return_value=True),
            mock.patch("claudewheel.state.save_launch_state", autospec=True),
            mock.patch("claudewheel.state.record_inode", autospec=True),
            mock.patch(
                "claudewheel.launch.resolve_launch_config",
                autospec=True,
                return_value=("/cwd", ["/bin/claude"], {}),
            ),
            mock.patch("claudewheel.launch.do_launch", mock.MagicMock()),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            cli._do_launch_sequence(
                self.ws,
                mock.MagicMock(),
                FakeAppConfigStore(),
                {"profile": "work", "directory": str(self.project)},
                interactive=interactive,
            )

    # -- tests -------------------------------------------------------------

    def test_launch_heals_drift_to_canonical(self) -> None:
        self._make_profile("work", _drifted_profile_settings())
        self.shared_settings.write_text(
            json.dumps(_drifted_shared_settings(), indent=2) + "\n"
        )

        self._launch()

        s = self._read("work")
        self.assertEqual(s["hooks"], self._canonical_hooks())
        self.assertEqual(s["claudewheel"]["disallowedTools"], list(DISALLOWED_TOOLS))
        self.assertEqual(set(s["permissions"]["deny"]), set(canonical_deny_rules()))
        self.assertEqual(set(s["permissions"]["ask"]), set(canonical_ask_rules()))
        self.assertNotIn("Bash(git stash:*)", s["permissions"]["allow"])
        self.assertIn("Bash(git rm:*)", s["permissions"]["allow"])

        shared = json.loads(self.shared_settings.read_text())
        self.assertEqual(shared["hooks"], self._canonical_hooks())
        self.assertEqual(shared["disallowedTools"], list(DISALLOWED_TOOLS))
        pd = shared["profileDefaults"]
        self.assertEqual(set(pd["permissions"]["deny"]), set(canonical_deny_rules()))
        self.assertEqual(set(pd["permissions"]["ask"]), set(canonical_ask_rules()))

        # Guardrail hook scripts were deployed as part of canonical.
        self.assertTrue((self.ws.scripts_dir / "hook-block-unsafe-commands").exists())

    def test_second_launch_writes_nothing(self) -> None:
        self._make_profile("work", _drifted_profile_settings())
        self.shared_settings.write_text(
            json.dumps(_drifted_shared_settings(), indent=2) + "\n"
        )

        self._launch()  # first launch heals to canonical
        profile_path = self.profiles_dir / "work" / "settings.json"
        first_profile = profile_path.read_text()
        first_profile_mtime = profile_path.stat().st_mtime_ns
        first_shared = self.shared_settings.read_text()
        first_shared_mtime = self.shared_settings.stat().st_mtime_ns

        self._launch()  # second launch: everything already canonical

        self.assertEqual(profile_path.read_text(), first_profile)
        self.assertEqual(profile_path.stat().st_mtime_ns, first_profile_mtime)
        self.assertEqual(self.shared_settings.read_text(), first_shared)
        self.assertEqual(self.shared_settings.stat().st_mtime_ns, first_shared_mtime)

    def test_discoverable_default_claude_is_never_touched(self) -> None:
        self._make_profile("work", _drifted_profile_settings())
        self.shared_settings.write_text(
            json.dumps(_drifted_shared_settings(), indent=2) + "\n"
        )
        # A discoverable ~/.claude (the "default" profile) with DRIFTED settings:
        # the core must never read or write it.
        default_sp = self._make_default_claude(_drifted_profile_settings())
        before = default_sp.read_text()
        before_mtime = default_sp.stat().st_mtime_ns

        self._launch()

        self.assertEqual(default_sp.read_text(), before)
        self.assertEqual(default_sp.stat().st_mtime_ns, before_mtime)
        # The real (managed) profile WAS healed, proving the launch reconciled.
        self.assertEqual(self._read("work")["hooks"], self._canonical_hooks())

    # -- the credential file is never a launch's business -------------------

    def test_launch_leaves_the_credential_file_untouched(self) -> None:
        """A launch never writes the launched profile's ``.credentials.json``.

        Claude Code owns that file; claudewheel's launch path reads a profile's
        stored token and never rewrites the session credentials beside it. No
        writer for it exists today, so the property holds by construction --
        this is what would fail the day one comes back, and it runs on the
        launch that DOES write, rewriting ``settings.json`` in the very same
        directory.

        Byte-level (``hash_snapshot``) rather than mtime/size alone, so an
        in-place rewrite of identical length would still be seen; mtime and
        size are asserted too, so even a rewrite of the same bytes shows up.
        """
        self._make_profile("work", _drifted_profile_settings())
        self.shared_settings.write_text(
            json.dumps(_drifted_shared_settings(), indent=2) + "\n"
        )
        creds = self.profiles_dir / "work" / ".credentials.json"
        creds.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-known-bytes"}}) + "\n"
        )
        before_hash = hash_snapshot([creds])
        before_stat = creds.stat()

        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                self._launch(interactive=interactive)

                self.assertEqual(hash_snapshot([creds]), before_hash)
                after_stat = creds.stat()
                self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
                self.assertEqual(after_stat.st_size, before_stat.st_size)
                # The launch really ran a writer next to it: the drifted
                # profile settings were healed to canonical.
                self.assertEqual(self._read("work")["hooks"], self._canonical_hooks())


if __name__ == "__main__":
    unittest.main()
