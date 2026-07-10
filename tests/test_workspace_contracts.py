"""Contract tests for profile.resolve_profile() read-only / corrupt-tokens behavior.

These pin the FUTURE contract described in
``todo/resolve-profile-constructs-full-config-manager.md``:

1. ``resolve_profile()`` must resolve a profile with ZERO filesystem writes and
   ZERO terminal I/O, so it works on a read-only bind mount / headless container.
   Today it constructs a full ``ConfigManager`` whose ``__post_init__`` runs
   schema migrations that write ``config.json`` (and, on first run, mkdirs +
   default files), so it crashes on a read-only filesystem. That test is marked
   ``@unittest.expectedFailure`` until the refactor lands.

2. A corrupt ``tokens.json`` must become a HARD ERROR that names the file, rather
   than being silently swallowed (today ``resolve_profile`` catches
   ``json.JSONDecodeError`` and returns env WITHOUT the token). That case is
   ``@unittest.expectedFailure``. A MISSING file and a MISSING entry must remain
   fine (no token, no error) -- those two are green today and must stay green.

When the refactor ships, drop the ``@unittest.expectedFailure`` decorators.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_OPTIONS,
    DEFAULT_SEGMENTS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
)
from claudewheel.profile import resolve_profile


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _set_tree_mode(root: Path, dir_mode: int, file_mode: int) -> None:
    """chmod every dir/file under *root* (inclusive). Files first, then dirs."""
    dirs: list[Path] = [root]
    files: list[Path] = []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            dirs.append(Path(dp) / d)
        for f in fns:
            files.append(Path(dp) / f)
    for f in files:
        os.chmod(f, file_mode)
    for d in dirs:
        os.chmod(d, dir_mode)


def _build_fake_home(home: Path, *, tokens: str | dict | None) -> Path:
    """Populate *home* with a complete ~/.claudewheel tree and profile 'alpha'.

    Creates every dir/file ConfigManager.__post_init__ would otherwise create,
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

    if tokens is not None:
        _write_json(cw / "tokens.json", tokens)

    return alpha


def _write_corrupt_tokens(home: Path) -> None:
    (home / ".claudewheel" / "tokens.json").write_text("{invalid json")


class _FakeHomeMixin:
    """Patch every path constant + Path.home + $HOME onto a tmp fake home."""

    def _patch_env(self, home: Path, *, detect):
        """Start patches redirecting all IO to *home*. Returns nothing; uses addCleanup.

        *detect* is the replacement for config.detect_terminal_background
        (a return_value string, or a Mock whose call should never happen).
        """
        cw = home / ".claudewheel"
        import claudewheel.config as cfg_mod
        import claudewheel.discovery as disc_mod
        import claudewheel.profile as prof_mod

        config_consts = {
            "CONFIG_DIR": cw,
            "CONFIG_FILE": cw / "config.json",
            "SEGMENTS_FILE": cw / "segments.json",
            "OPTIONS_FILE": cw / "options.json",
            "STATE_FILE": cw / "state.json",
            "THEMES_DIR": cw / "themes",
            "HOOKS_DIR": cw / "hooks",
            "SCRIPTS_DIR": cw / "scripts",
            "SHARED_SETTINGS_FILE": cw / "shared-settings.json",
        }
        patches = [patch_obj for patch_obj in (
            *[mock.patch.object(cfg_mod, name, value)
              for name, value in config_consts.items()],
            mock.patch.object(prof_mod, "TOKENS_FILE", cw / "tokens.json"),
            mock.patch.object(disc_mod, "PROFILES_DIR", cw / "profiles"),
            mock.patch.object(disc_mod, "TOKENS_FILE", cw / "tokens.json"),
            mock.patch.object(Path, "home", classmethod(lambda cls: home)),
            mock.patch.dict(os.environ, {"HOME": str(home)}),
        )]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # config.detect_terminal_background: spy (raises) or stub ("dark").
        if isinstance(detect, mock.Mock):
            dp = mock.patch.object(cfg_mod, "detect_terminal_background", detect)
        else:
            dp = mock.patch.object(
                cfg_mod, "detect_terminal_background", return_value=detect,
            )
        dp.start()
        self.addCleanup(dp.stop)


class ReadOnlyResolutionContractTests(_FakeHomeMixin, unittest.TestCase):
    """resolve_profile must resolve a profile on a read-only tree with no writes/no TTY IO."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.alpha_dir = _build_fake_home(self.home, tokens={"alpha": "tok-alpha"})
        # Lock the whole tree down: dirs r-x, files r--.
        _set_tree_mode(self.home, dir_mode=0o555, file_mode=0o444)

    def tearDown(self) -> None:
        # Restore write bits BEFORE TemporaryDirectory cleanup, else rmtree fails.
        _set_tree_mode(self.home, dir_mode=0o755, file_mode=0o644)
        self._tmp.cleanup()

    @unittest.expectedFailure
    def test_readonly_resolution_zero_writes_zero_tty(self) -> None:
        """RED today: ConfigManager's schema migration writes config.json ->
        OSError [Errno 30] Read-only file system (verified undecorated).

        Future contract: pure read-only resolution -- returns the profile dir and
        token from tokens.json, performs zero writes, and never queries the TTY.
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
    """corrupt tokens.json -> hard error; missing file / missing entry -> fine."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.alpha_dir = self.home / ".claudewheel" / "profiles" / "alpha"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @unittest.expectedFailure
    def test_corrupt_tokens_is_hard_error(self) -> None:
        """RED today: corrupt tokens.json is silently swallowed (no exception,
        env returned without a token), so assertRaises fails (verified undecorated).

        Future contract: a corrupt tokens.json raises an error naming the file.
        """
        _build_fake_home(self.home, tokens=None)  # writable tree; ConfigManager can init
        _write_corrupt_tokens(self.home)
        self._patch_env(self.home, detect="dark")

        with self.assertRaises(Exception) as ctx:
            resolve_profile("alpha")
        self.assertIn("tokens.json", str(ctx.exception))

    def test_missing_tokens_file_is_fine(self) -> None:
        """No tokens.json at all: succeeds, returns env WITHOUT a token."""
        _build_fake_home(self.home, tokens=None)
        self.assertFalse((self.home / ".claudewheel" / "tokens.json").exists())
        self._patch_env(self.home, detect="dark")

        env = resolve_profile("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_tokens_present_but_no_entry_is_fine(self) -> None:
        """tokens.json exists but has no entry for the profile: succeeds, no token."""
        _build_fake_home(self.home, tokens={"someone-else": "tok-other"})
        self._patch_env(self.home, detect="dark")

        env = resolve_profile("alpha")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.alpha_dir))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)


def _snapshot(root: Path) -> dict[str, tuple[float, int]]:
    """Map each file under *root* to (mtime_ns-ish, size) for change detection."""
    snap: dict[str, tuple[float, int]] = {}
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            p = Path(dp) / f
            st = p.stat()
            snap[str(p)] = (st.st_mtime, st.st_size)
    return snap


if __name__ == "__main__":
    unittest.main()
