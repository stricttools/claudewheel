"""Contract tests for profile.resolve_profile() read-only / corrupt-tokens behavior.

These pin the contract described in
``todo/resolve-profile-constructs-full-config-manager.md``, now that the
thin-facade refactor (``resolve_profile`` -> ``Workspace.default().profiles.env``)
has landed:

1. ``resolve_profile()`` resolves a profile with ZERO filesystem writes and
   ZERO terminal I/O, so it works on a read-only bind mount / headless
   container. Historically it constructed a full ``AppConfigStore`` whose
   ``__post_init__`` ran schema migrations that wrote ``config.json`` (and, on
   first run, mkdirs + default files), so it crashed when the tree was locked
   down. This is now the LIVE contract.

2. A corrupt token entry is a HARD ERROR (``TokenStoreError``) that names the
   file, rather than being silently swallowed (historically ``resolve_profile``
   caught ``json.JSONDecodeError`` and returned env WITHOUT the token). A
   profile that stores no entry at all remains fine (no token, no error).

Both contracts are enforced live -- no ``@unittest.expectedFailure`` remains.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from unittest import mock

from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_OPTIONS,
    DEFAULT_SEGMENTS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
)
from claudewheel.health import run_health_check
from claudewheel.profile import resolve_profile
from claudewheel.tokens import TokenStoreError
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    set_tree_mode as _set_tree_mode,
    snapshot_tree as _snapshot,
    write_token_entry,
)


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _build_fake_home(home: Path, *, token: dict[str, Any] | None) -> Path:
    """Populate *home* with a complete ~/.claudewheel tree and profile 'alpha'.

    Creates every dir/file AppConfigStore.__post_init__ would otherwise create,
    so a read-only tree cannot be "fixed" by _ensure_dir writing missing files.
    Returns the alpha profile dir.
    """
    cw = home / ".claudewheel"
    profiles = cw / "profiles"
    alpha = profiles / "alpha"
    themes = cw / "themes"
    hooks = cw / "hooks"
    scripts = cw / "scripts"
    for d in (cw, profiles, alpha, themes, hooks, scripts):
        d.mkdir(parents=True, exist_ok=True)

    alpha_settings = alpha / "settings.json"
    _write_json(alpha_settings, {"permissions": {"allow": [], "deny": [], "ask": []}})

    # An explicit non-"auto" theme so _resolve_theme_name never queries the
    # terminal. This alone does NOT dodge writes: _run_versioned_migrations
    # still bumps _schema_version (0 -> latest) and writes config.json.
    _write_json(cw / "config.json", {**DEFAULT_CONFIG, "theme": "dark"})
    _write_json(cw / "segments.json", DEFAULT_SEGMENTS)
    _write_json(cw / "options.json", DEFAULT_OPTIONS)
    _write_json(cw / "state.json", DEFAULT_STATE)
    _write_json(themes / "dark.json", DEFAULT_THEME_DARK)
    _write_json(themes / "light.json", DEFAULT_THEME_LIGHT)
    _write_json(cw / "shared-settings.json", {})

    if token is not None:
        write_token_entry(alpha, token)

    return alpha


def _corrupt_token_path(home: Path) -> Path:
    """Leave an unparseable token entry in the 'alpha' profile; return its path."""
    alpha = home / ".claudewheel" / "profiles" / "alpha"
    path = write_token_entry(alpha, {"token": "t"})
    path.write_text("{invalid json")
    return path


class _FakeHomeMixin:
    """Patch every path constant + Path.home + $HOME onto a tmp fake home."""

    if TYPE_CHECKING:
        # Mixed into unittest.TestCase; declare the borrowed members for mypy.
        def addCleanup(
            self, func: Callable[..., object], /, *args: object, **kwargs: object
        ) -> None: ...

    def _patch_env(self, home: Path, *, detect: str | mock.Mock) -> None:
        """Start patches redirecting all IO to *home*. Returns nothing; uses addCleanup.

        *detect* is the replacement for config.detect_terminal_background
        (a return_value string, or a Mock whose call should never happen).
        """
        import claudewheel.config as cfg_mod

        # resolve_profile resolves via Workspace.default(), which derives every
        # path from Path.home()/.claudewheel -- so poisoning Path.home + $HOME
        # is the whole redirection.
        patches = [
            patch_obj
            for patch_obj in (
                mock.patch.object(Path, "home", classmethod(lambda cls: home)),
                mock.patch.dict(os.environ, {"HOME": str(home)}),
            )
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # config.detect_terminal_background: spy (raises) or stub ("dark").
        if isinstance(detect, mock.Mock):
            dp = mock.patch.object(cfg_mod, "detect_terminal_background", detect)
        else:
            dp = mock.patch.object(
                cfg_mod,
                "detect_terminal_background",
                autospec=True,
                return_value=detect,
            )
        dp.start()
        self.addCleanup(dp.stop)


class ReadOnlyResolutionContractTests(_FakeHomeMixin, unittest.TestCase):
    """resolve_profile must resolve a profile on a read-only tree with no writes/no TTY IO."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.alpha_dir = _build_fake_home(self.home, token={"token": "tok-alpha"})
        # Lock the whole tree down: dirs r-x, files r--.
        _set_tree_mode(self.home, dir_mode=0o555, file_mode=0o444)

    def tearDown(self) -> None:
        # Restore write bits BEFORE TemporaryDirectory cleanup, else rmtree fails.
        _set_tree_mode(self.home, dir_mode=0o755, file_mode=0o644)
        self._tmp.cleanup()

    def test_readonly_resolution_zero_writes_zero_tty(self) -> None:
        """Historically RED: the config store's schema migration tried to write
        config.json into the chmod-locked tree -> PermissionError [Errno 13]
        Permission denied (the fixture locks perms via chmod, it is not a
        read-only mount, so the errno is 13, not 30) (verified undecorated).

        Live contract: pure read-only resolution -- returns the profile dir and
        token from its own store, performs zero writes, and never queries the TTY.
        """
        # Spy that fails loudly if terminal background detection is attempted.
        spy = mock.Mock(side_effect=AssertionError("terminal I/O attempted"))
        self._patch_env(self.home, detect=spy)

        before = _snapshot(self.home)

        env = resolve_profile("alpha")

        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")
        self.assertFalse(spy.called, "terminal background detection must not run")
        self.assertEqual(_snapshot(self.home), before, "resolve_profile wrote to disk")


class CorruptTokensContractTests(_FakeHomeMixin, unittest.TestCase):
    """A corrupt token entry -> hard error; no entry at all -> fine."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.alpha_dir = self.home / ".claudewheel" / "profiles" / "alpha"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_corrupt_token_entry_is_hard_error(self) -> None:
        """Historically RED: a corrupt token store was silently swallowed (no
        exception, env returned without a token) (verified undecorated).

        Live contract: a corrupt entry raises TokenStoreError naming the file.
        """
        _build_fake_home(self.home, token=None)  # writable tree
        path = _corrupt_token_path(self.home)
        self._patch_env(self.home, detect="dark")

        with self.assertRaises(TokenStoreError) as ctx:
            resolve_profile("alpha")
        self.assertIn(str(path), str(ctx.exception))

    def test_profile_storing_no_token_is_fine(self) -> None:
        """No stored entry at all: succeeds, returns env WITHOUT a token."""
        _build_fake_home(self.home, token=None)
        self._patch_env(self.home, detect="dark")

        env = resolve_profile("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_another_profiles_entry_is_never_used(self) -> None:
        """A token stored by a different profile is not this profile's token."""
        _build_fake_home(self.home, token=None)
        other = self.home / ".claudewheel" / "profiles" / "someone-else"
        other.mkdir(parents=True, exist_ok=True)
        write_token_entry(other, {"token": "tok-other"})
        self._patch_env(self.home, detect="dark")

        env = resolve_profile("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)


class WholePackageReadOnlyContractTests(_FakeHomeMixin, unittest.TestCase):
    """Every READ path must work on a fully-migrated, chmod-locked workspace.

    Phase decisions: reads must work on read-only mounts; fail-loud is only for
    WRITE operations. This builds a fully-populated, already-migrated workspace
    (``appconfig()`` runs all migrations + dir seeding while writable), then locks
    the whole tree down (dirs r-x, files r--) and exercises the read surface:

    - ``ProfileStore.enumerate`` / ``profiles.env(name)``
    - the ``resolve_profile`` facade (via ``Workspace.default()``)
    - each profile's own token store
    - ``SharedStore`` path accessors
    - ``run_health_check`` -- a diagnostic that must COMPLETE and report on a
      read-only tree, never crash. The inode-renames check attempts to rewrite
      ``inodes.json`` when it finds stale entries; on a locked tree that write
      must fail cleanly (guarded ``except OSError``), not blow up the run. The
      fixture seeds a stale inode entry precisely to exercise that write path.

    A before/after snapshot proves zero successful writes reached the tree.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.alpha_dir = _build_fake_home(self.home, token={"token": "tok-alpha"})

        # Construct the store once WHILE WRITABLE so every migration/seed runs
        # (schema bump writes config.json, dir seeding creates shared subdirs).
        self.ws = Workspace.open(
            self.home / ".claudewheel", claude_dir=self.home / ".claude"
        )
        self.ws.appconfig()

        # Seed a stale inode entry (path does not exist) so check_inode_renames
        # takes its write branch -- the whole point is to prove that write fails
        # cleanly on the locked tree rather than crashing run_health_check.
        self.ws.inodes_file.parent.mkdir(parents=True, exist_ok=True)
        _write_json(self.ws.inodes_file, {"/nonexistent/stale-dir-xyz": 424242})

        # Lock the whole tree down: dirs r-x, files r--.
        _set_tree_mode(self.home, dir_mode=0o555, file_mode=0o444)

    def tearDown(self) -> None:
        # Restore write bits BEFORE TemporaryDirectory cleanup, else rmtree fails.
        _set_tree_mode(self.home, dir_mode=0o755, file_mode=0o644)
        self._tmp.cleanup()

    def test_all_reads_work_and_zero_writes_on_readonly_tree(self) -> None:
        self._patch_env(self.home, detect="dark")

        before = _snapshot(self.home)

        # -- ProfileStore.enumerate --
        profiles = self.ws.profiles.enumerate()
        self.assertIn("alpha", [p.name for p in profiles])

        # -- profiles.env(name) --
        env = self.ws.profiles.env("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")

        # -- resolve_profile facade (resolves via Workspace.default()) --
        resolved = resolve_profile("alpha")
        self.assertEqual(resolved["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertEqual(resolved["CLAUDE_CODE_OAUTH_TOKEN"], "tok-alpha")

        # -- the profile's own token store --
        self.assertEqual(self.ws.profiles.data_for("alpha").token(), "tok-alpha")

        # -- SharedStore path accessors (pure path reads) --
        shared = self.ws.shared
        self.assertEqual(shared.inodes_file, self.ws.inodes_file)
        self.assertTrue(str(shared.projects_dir).startswith(str(self.ws.shared_dir)))
        self.assertTrue(str(shared.subdir("tasks")).startswith(str(self.ws.shared_dir)))

        # -- health's read-only checks: must COMPLETE and report, never crash --
        results = run_health_check(self.ws)
        self.assertTrue(results)
        labels = {r.label for r in results}
        self.assertIn("inode-renames", labels)
        # The inode check attempted a write (stale entry) but the locked tree
        # rejected it; the guarded except swallowed it, so the run still reports.
        inode_result = next(r for r in results if r.label == "inode-renames")
        self.assertTrue(inode_result.ok)
        # Honest detail: the write to prune inodes.json was rejected by the
        # locked tree, so the check must NOT claim it "cleaned" anything. It
        # must report the stale entries were found but could not be persisted
        # (read-only likelihood).
        self.assertNotIn("cleaned", inode_result.detail)
        self.assertIn("stale", inode_result.detail)
        self.assertIn("could not persist", inode_result.detail)
        self.assertIn("read-only", inode_result.detail)

        # -- zero successful writes reached the tree --
        self.assertEqual(
            _snapshot(self.home),
            before,
            "read-only workspace was mutated by a read/diagnostic path",
        )


if __name__ == "__main__":
    unittest.main()
