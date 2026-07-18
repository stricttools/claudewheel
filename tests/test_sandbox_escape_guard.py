"""Permanent guard: no claudewheel write path may touch the REAL home's config.

Background
----------
A test-sandbox escape once rewrote real profile hook paths: production code
under test resolved a path against the real ``~/.claudewheel`` instead of the
sandbox and mutated a live file. The frozen-constant root cause is gone --
every path now derives from an injected :class:`~claudewheel.workspace.Workspace`
and nothing holds import-time path copies. This test is the permanent proof:
it drives every meaningful WRITE path against a *sandbox* workspace and asserts
that a content-hash snapshot of the real home's claudewheel config surface is
byte-for-byte identical before and after. If any driver escapes the sandbox and
writes under the real home, the snapshots diverge and the failure names exactly
which real files changed.

The allowlist (the real-home surface we protect)
------------------------------------------------
Captured under ``wheelhelpers.REAL_HOME / ".claudewheel"``:

- ``profiles/*/settings.json`` and ``profiles/*/.credentials.json``
- ``shared-settings.json``
- ``scripts/**`` (files) and ``hooks/**`` (files)
- ``config.json``, ``segments.json``, ``options.json``, ``tokens.json``
- ``themes/**`` (files)

Files that do not exist are recorded as MISSING (see
:func:`wheelhelpers.hash_snapshot`); equality of the two snapshots is what
matters, so a MISSING-on-both entry is a match. The path set is re-enumerated
for both snapshots, so a file *created* under the real home (not just a
modification) is caught too: it is MISSING before and present after.

Exclusions (deliberately NOT in the allowlist)
----------------------------------------------
- ``shared/`` -- the shared session store is written live and continuously by
  every running Claude session (projects, tasks, todos, session-env, ...). Some
  drivers here (import, migrate, mv) legitimately target it; their primary
  writes land in the sandbox's ``shared/``, and the real ``shared/`` is not a
  claudewheel *config* surface, so it is out of scope by design.
- ``state.json`` -- rewritten out-of-band by running sessions (launch state,
  auth-browser memory) and is not a durable config surface.
- Per-profile runtime files (``history.jsonl``, ``statsig/``, ``.claude.json``,
  ``projects/``, ...) -- written live by running sessions, not config.

Why content hashes (not mtime + size)
--------------------------------------
An in-place rewrite that preserves a file's byte length AND its mtime (a real
possibility for the escape class this guards) is invisible to an (mtime, size)
snapshot. SHA-256 over each file's bytes catches such same-size rewrites. It is
affordable here only because the allowlist is a bounded handful of small config
files, never a whole tree. The non-vacuity tests below prove both a new-file and
a same-size in-place rewrite trip the comparison.

Concurrency caveat
------------------
``options.json``, every profile ``settings.json``, and ``tokens.json`` are
steady-state quiescent -- nothing rewrites them except explicit operator
actions. A concurrent launcher pin, ``claudewheel config`` edit, or auth flow
landing inside this test's window would change one of them and produce a rare,
honest flake: rerun. This cannot happen in CI, where no interactive sessions
run. If the failure persists across reruns, it is a real sandbox escape, not a
flake -- read the named files.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel.appdata import OptionsFile, StateFile
from claudewheel.hook_scripts import HOOK_SCRIPTS, deploy_scripts
from claudewheel.import_ import run_import
from claudewheel.migrate import migrate_sessions
from claudewheel.mv import run_mv
from claudewheel.patch_profiles import run_patch_profiles
from claudewheel.permission import add_rule, load_settings, remove_rule, save_settings
from claudewheel.reconcile import run_reconcile
from claudewheel.wizard import WizardResult, create_profile
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    MISSING,
    REAL_HOME,
    SandboxHomeTestCase,
    _Missing,
    hash_snapshot,
)

_A_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _real_home_allowlist_paths() -> list[Path]:
    """Enumerate the protected files under the REAL home's ``.claudewheel``.

    Fixed single files are included unconditionally (a MISSING file is a valid,
    comparable state). Glob/tree families enumerate only what currently exists;
    because this is called for BOTH snapshots, a newly-created file appears in
    the second enumeration and so is caught.
    """
    cw = REAL_HOME / ".claudewheel"
    paths: list[Path] = [
        cw / "config.json",
        cw / "segments.json",
        cw / "options.json",
        cw / "tokens.json",
        cw / "shared-settings.json",
    ]
    paths += sorted(cw.glob("profiles/*/settings.json"))
    paths += sorted(cw.glob("profiles/*/.credentials.json"))
    for family in ("scripts", "hooks", "themes"):
        base = cw / family
        if base.is_dir():
            paths += sorted(p for p in base.rglob("*") if p.is_file())
    return paths


def _snapshot_real_home() -> dict[str, str | _Missing]:
    """Content-hash the current real-home allowlist surface."""
    return hash_snapshot(_real_home_allowlist_paths())


def _fmt(value: object) -> str:
    """Render a hash (shortened) or the MISSING sentinel for a diff line."""
    if value is MISSING:
        return "<MISSING>"
    return f"{str(value)[:12]}..."


def _diff_snapshots(
    before: dict[str, str | _Missing], after: dict[str, str | _Missing]
) -> list[str]:
    """Return one ``path: before -> after`` line per differing file (empty = equal)."""
    lines: list[str] = []
    for key in sorted(set(before) | set(after)):
        b = before.get(key, MISSING)
        a = after.get(key, MISSING)
        if b != a:
            lines.append(f"{key}: {_fmt(b)} -> {_fmt(a)}")
    return lines


class SandboxEscapeGuardTest(SandboxHomeTestCase):
    """Every write path runs against the sandbox; the real home must not change."""

    def _drive_all_write_paths(self) -> None:
        """Exercise every meaningful claudewheel WRITE path against the sandbox.

        All state comes from :class:`SandboxHomeTestCase` (poisoned ``Path.home``,
        redirected ``HOME``, ``self.ws`` rooted at the sandbox). Each driver's
        signature was read from its module; argument shapes are exact, not
        assumed. Output is swallowed -- the drivers print progress.

        Excluded on purpose:
        - ``install.install_version`` -- performs a real network download and
          targets a different real-home subtree governed by ``BinaryLocator``.
        - import/migrate/mv primarily write the sandbox ``shared/`` and profile
          runtime dirs, which are outside the allowlist by design (honesty note).
        """
        ws = self.ws

        # 1. appconfig() on a FRESH sandbox root: dir seeding + schema migrations.
        fresh_parent = self.home / "fresh"
        fresh_parent.mkdir()
        Workspace.open(
            fresh_parent / ".claudewheel", claude_dir=self.home / ".claude"
        ).appconfig()

        # 2. ProfileStore.create (settings dict; one create suffices).
        base_settings: dict[str, Any] = {
            "permissions": {"allow": [], "deny": [], "ask": []}
        }
        ws.profiles.create("gp-store", dict(base_settings))

        # 3. Programmatic wizard create_profile (WizardResult built directly).
        wiz = WizardResult(
            name="gp-wiz",
            config_dir=str(ws.profiles.path_for("gp-wiz")),
            clone_from=None,
            wire_hooks=True,
            symlink_shared=False,
            disable_recap=True,
            cleanup_10y=False,
            disable_memory=False,
            disable_attribution=False,
        )
        create_profile(ws, wiz)

        # 4. permission add + remove (the settings.json rule primitives).
        settings_path = ws.profiles.path_for("gp-store") / "settings.json"
        data = load_settings(settings_path)
        add_rule(data, "deny", "Bash(rm -rf /:*)")
        save_settings(settings_path, data)
        data = load_settings(settings_path)
        remove_rule(data, "deny", "Bash(rm -rf /:*)")
        save_settings(settings_path, data)

        # 5. run_patch_profiles with a stale-path hook so the repath write fires.
        stale_dir = ws.profiles_dir / "gp-stale"
        stale_dir.mkdir(parents=True)
        (stale_dir / ".credentials.json").write_text("{}")
        save_settings(
            stale_dir / "settings.json",
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/nonexistent/old-scripts/hook-timestamp",
                                },
                            ],
                        },
                    ],
                },
            },
        )
        run_patch_profiles(ws)

        # 6. run_reconcile across all profiles + shared-settings profileDefaults.
        run_reconcile(ws, dry_run=False, profile=None)

        # 7. hook_scripts.deploy_scripts into the sandbox scripts dir.
        deploy_scripts(list(HOOK_SCRIPTS.keys()), ws.scripts_dir, force_overwrite=True)

        # 8. OptionsFile add_pinned + write (sandbox options.json).
        opts = OptionsFile(ws.options_file)
        opts.add_pinned("profile", "gp-pinned", {})
        current = opts.load({})
        opts.write(current)

        # 9. StateFile save + set_value (sandbox state.json).
        state = StateFile(ws.state_file)
        state.save({"guard": "value"})
        state.set_value("guard_key", "guard_val")

        # 10. TokenStore add / set_tier / rename / remove (sandbox tokens.json).
        ws.tokens.add("gp-tok", "token-value")
        ws.tokens.set_tier("gp-tok", tier="pro", subscription="max")
        ws.tokens.rename("gp-tok", "gp-tok2")
        ws.tokens.remove("gp-tok2")

        # 11. run_import against a small fake source (writes sandbox shared/).
        source = self.home / "import-src"
        src_proj = source / "projects" / "encoded-proj"
        src_proj.mkdir(parents=True)
        (src_proj / f"{_A_UUID}.jsonl").write_text(
            '{"cwd":"/guard/proj","sessionId":"' + _A_UUID + '"}\n'
        )
        with mock.patch(
            "claudewheel.import_.get_session_cwd",
            autospec=True,
            return_value="/guard/proj",
        ):
            run_import(
                ws.shared,
                str(source),
                mappings=[("/guard/proj", str(self.home / "guard-target"))],
            )

        # 12. run_mv: rename a project dir + migrate (sandbox only).
        mv_old = self.home / "mv-old"
        mv_old.mkdir()
        run_mv(ws, str(mv_old), str(self.home / "mv-new"))

        # 13. migrate_sessions between two sandbox profiles.
        src_prof = ws.profiles_dir / "gp-src"
        dst_prof = ws.profiles_dir / "gp-dst"
        (src_prof / "session-env" / _A_UUID).mkdir(parents=True)
        dst_prof.mkdir(parents=True)
        migrate_sessions(ws, "gp-src", "gp-dst")

        # 14. ProfileStore.rename then delete (sandbox profile lifecycle).
        ws.profiles.rename("gp-store", "gp-store2")
        ws.profiles.delete("gp-store2")

    def test_real_home_config_surface_is_immutable(self) -> None:
        """Drive every write path against the sandbox; the real home stays byte-identical."""
        before = _snapshot_real_home()

        with contextlib.redirect_stdout(io.StringIO()):
            self._drive_all_write_paths()

        after = _snapshot_real_home()

        diff = _diff_snapshots(before, after)
        self.assertEqual(
            diff,
            [],
            "SANDBOX ESCAPE: a write path mutated the REAL home's claudewheel "
            "config surface. Changed files:\n  " + "\n  ".join(diff),
        )

    # -- Non-vacuity: prove the comparison actually trips on real changes ----

    def test_nonvacuity_new_file_is_detected(self) -> None:
        """Planting a NEW file between snapshots must trip the comparison.

        Operates on a THROWAWAY monitored dir -- never the real tree.
        """
        monitored = self.home / "monitored-newfile"
        monitored.mkdir()
        (monitored / "a.json").write_text("{}\n")
        (monitored / "b.json").write_text("[]\n")

        def watched() -> list[Path]:
            return sorted(p for p in monitored.rglob("*") if p.is_file())

        before = hash_snapshot(watched())
        (monitored / "c.json").write_text('{"planted": true}\n')  # new file
        after = hash_snapshot(watched())

        diff = _diff_snapshots(before, after)
        self.assertTrue(diff, "a newly planted file was not detected")
        self.assertTrue(any("c.json" in line for line in diff))

    def test_nonvacuity_same_size_rewrite_is_detected(self) -> None:
        """A same-byte-size, mtime-preserving in-place rewrite must trip the comparison.

        This is exactly what an (mtime, size) snapshot would MISS and content
        hashing must catch. Operates on a THROWAWAY monitored dir.
        """
        monitored = self.home / "monitored-rewrite"
        monitored.mkdir()
        target = monitored / "settings.json"
        target.write_text("AAAA")
        original_stat = target.stat()
        watched = [target]

        before = hash_snapshot(watched)

        # In-place, same 4-byte length, then restore the original mtime/atime so
        # an (mtime, size) snapshot would see no change at all.
        target.write_text("BBBB")
        os.utime(target, (original_stat.st_atime, original_stat.st_mtime))
        self.assertEqual(target.stat().st_size, original_stat.st_size)
        self.assertEqual(target.stat().st_mtime, original_stat.st_mtime)

        after = hash_snapshot(watched)

        diff = _diff_snapshots(before, after)
        self.assertTrue(
            diff, "a same-size, mtime-preserved rewrite was not detected by hashing"
        )
        self.assertTrue(any("settings.json" in line for line in diff))
