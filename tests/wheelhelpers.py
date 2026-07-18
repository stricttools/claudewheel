"""Shared test infrastructure for the claudewheel suite.

This module centralizes the two things every filesystem-touching test needs:

1. A sandboxed fake ``$HOME`` (``SandboxHomeTestCase``) that both sets the
   ``HOME`` environment variable AND patches ``pathlib.Path.home`` so any
   runtime ``Path.home()`` call in production code resolves into a tmpdir,
   never the real home. Path resolution is fully workspace-driven now, so
   poisoning ``Path.home`` is sufficient: no module holds import-time path
   copies that need per-module rebinding.

2. A config-dir builder (``setup_temp_config_dir``) shared by the migration and
   theme-auto tests.

Naming note: this file is deliberately named ``wheelhelpers.py`` (not
``test_*.py``) so pytest does not collect it as a test module.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch

from claudewheel.shared_store import SharedStore
from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_OPTIONS,
    DEFAULT_SEGMENTS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
)

# Real home captured at import time, BEFORE any test patches Path.home. Used by
# the meta-test to prove that sandbox writes never touch the real home.
REAL_HOME: Path = Path(os.path.expanduser("~"))


def write_json(path: Path, data: dict[str, Any] | list[Any]) -> None:
    """Write *data* to *path* as pretty JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Filesystem snapshot / chmod helpers (hoisted from test_workspace_contracts,
# test_profile, and test_migration so the sandbox-escape guard and the
# read-only contract tests share one implementation).
# ---------------------------------------------------------------------------


class _Missing:
    """Sentinel for a file absent from a :func:`hash_snapshot`.

    A single module-level instance (:data:`MISSING`) is reused so that two
    snapshots compare equal when the same file is absent in both -- equality is
    by identity, and the readable ``repr`` keeps failure diffs legible.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<MISSING>"


MISSING = _Missing()


def set_tree_mode(root: Path, dir_mode: int, file_mode: int) -> None:
    """chmod every dir/file under *root* (inclusive). Files first, then dirs."""
    dirs: list[Path] = [root]
    files: list[Path] = []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            dirs.append(Path(dp) / d)
        for f in fns:
            files.append(Path(dp) / f)
    for fp in files:
        os.chmod(fp, file_mode)
    for dp2 in dirs:
        os.chmod(dp2, dir_mode)


def snapshot_tree(root: Path) -> dict[str, tuple[float, int]]:
    """Map each file under *root* to ``(mtime, size)`` for change detection.

    Walks with :func:`os.walk` (which does NOT follow symlinks), so only real
    files under *root* are recorded. Cheap, but blind to same-size in-place
    rewrites -- use :func:`hash_snapshot` when byte-level fidelity matters.
    """
    snap: dict[str, tuple[float, int]] = {}
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            p = Path(dp) / f
            st = p.stat()
            snap[str(p)] = (st.st_mtime, st.st_size)
    return snap


def hash_snapshot(paths: Iterable[Path]) -> dict[str, str | _Missing]:
    """Content-hash an EXPLICIT set of files: ``{str(path): sha256-hex | MISSING}``.

    Unlike :func:`snapshot_tree`, this takes an explicit iterable of individual
    file paths (not a tree root) and records the SHA-256 of each file's bytes,
    so an in-place rewrite that preserves mtime and size is still detected. A
    path that does not resolve to a regular file is recorded as :data:`MISSING`
    (equal across snapshots by sentinel identity). Only affordable because the
    caller monitors a bounded handful of small files, never a whole tree.
    """
    snap: dict[str, str | _Missing] = {}
    for path in paths:
        key = str(path)
        if path.is_file():
            snap[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            snap[key] = MISSING
    return snap


# ---------------------------------------------------------------------------
# Config-dir helpers (hoisted from test_migration.py and test_theme_auto.py)
# ---------------------------------------------------------------------------


def setup_temp_config_dir(
    tmp: Path,
    *,
    config: dict[str, Any] | None = None,
    segments: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    theme: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Create a ``~/.claudewheel``-shaped config dir under *tmp*.

    Returns a dict mapping path-constant names to the paths inside *tmp*,
    suitable for constructing a ``Workspace`` rooted at ``CONFIG_DIR``. Any
    parameter left as ``None`` gets a sensible default that will not cause
    ``AppConfigStore.__post_init__`` to error. Both ``dark.json`` and
    ``light.json`` are always written so theme resolution (auto/light/dark)
    works regardless of the config's chosen theme.
    """
    launcher_dir = tmp / "claudewheel"
    themes_dir = launcher_dir / "themes"
    hooks_dir = launcher_dir / "hooks"
    scripts_dir = launcher_dir / "scripts"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    themes_dir.mkdir(exist_ok=True)
    hooks_dir.mkdir(exist_ok=True)
    scripts_dir.mkdir(exist_ok=True)

    config_file = launcher_dir / "config.json"
    segments_file = launcher_dir / "segments.json"
    options_file = launcher_dir / "options.json"
    state_file = launcher_dir / "state.json"
    theme_file = themes_dir / "dark.json"
    shared_settings_file = launcher_dir / "shared-settings.json"

    write_json(config_file, config if config is not None else DEFAULT_CONFIG)
    write_json(segments_file, segments if segments is not None else DEFAULT_SEGMENTS)
    write_json(options_file, options if options is not None else DEFAULT_OPTIONS)
    write_json(state_file, state if state is not None else DEFAULT_STATE)
    write_json(theme_file, theme if theme is not None else DEFAULT_THEME_DARK)
    write_json(themes_dir / "light.json", DEFAULT_THEME_LIGHT)

    return {
        "CONFIG_DIR": launcher_dir,
        "CONFIG_FILE": config_file,
        "SEGMENTS_FILE": segments_file,
        "OPTIONS_FILE": options_file,
        "STATE_FILE": state_file,
        "THEMES_DIR": themes_dir,
        "HOOKS_DIR": hooks_dir,
        "SCRIPTS_DIR": scripts_dir,
        "SHARED_SETTINGS_FILE": shared_settings_file,
    }


# ---------------------------------------------------------------------------
# Sandbox home base class
# ---------------------------------------------------------------------------


class SandboxHomeTestCase(unittest.TestCase):
    """Base class providing a tmpdir-backed fake ``$HOME`` and workspace.

    On :meth:`setUp` it:

    - creates ``<tmp_home>/.claudewheel`` with ``profiles/``, ``shared/``
      (plus the ``SharedStore.SHARED_SUBDIRS`` subdirs), ``skills/``, ``themes/``,
      ``scripts/``, ``hooks/`` and minimal valid ``config.json``,
      ``state.json``, ``options.json``, ``segments.json``, ``tokens.json``,
      ``shared-settings.json``, and ``themes/{dark,light}.json``;
    - points the ``HOME`` env var at the fake home;
    - patches ``pathlib.Path.home`` to return the fake home (POISONED HOME) so
      runtime ``Path.home()`` calls resolve into the sandbox.

    Subclasses that need the built-in ``~/.claude`` default profile populated
    should set the class attribute ``populate_default_profile = True``.

    ``self.sandbox_paths`` maps every path-constant name to its sandbox value,
    for tests that need to reference a specific sandbox path directly.
    """

    # Subclasses may override to populate ~/.claude with a default profile.
    populate_default_profile: bool = False

    def setUp(self) -> None:  # noqa: D102
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.launcher_dir = self.home / ".claudewheel"

        self._build_sandbox()

        # A workspace rooted at the sandbox. Because Path.home is poisoned below,
        # Workspace.default() would resolve here too, but the explicit open() is
        # clearer and independent of env state.
        from claudewheel.workspace import Workspace

        self.ws = Workspace.open(self.launcher_dir, claude_dir=self.home / ".claude")

        # HOME env var (affects os.path.expanduser)
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

        # POISONED HOME: runtime Path.home() resolves into the sandbox.
        self._home_patch = patch.object(
            Path, "home", autospec=True, return_value=self.home
        )
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def _restore_home(self) -> None:
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home

    def _build_sandbox(self) -> None:
        """Populate the fake ``~/.claudewheel`` (and optionally ``~/.claude``)."""
        ld = self.launcher_dir
        profiles_dir = ld / "profiles"
        shared_dir = ld / "shared"
        skills_dir = ld / "skills"
        themes_dir = ld / "themes"
        scripts_dir = ld / "scripts"
        hooks_dir = ld / "hooks"
        for d in (
            profiles_dir,
            shared_dir,
            skills_dir,
            themes_dir,
            scripts_dir,
            hooks_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        for sub in SharedStore.SHARED_SUBDIRS:
            (shared_dir / sub).mkdir(parents=True, exist_ok=True)

        write_json(ld / "config.json", DEFAULT_CONFIG)
        write_json(ld / "segments.json", DEFAULT_SEGMENTS)
        write_json(ld / "options.json", DEFAULT_OPTIONS)
        write_json(ld / "state.json", DEFAULT_STATE)
        write_json(ld / "tokens.json", {})
        write_json(ld / "shared-settings.json", {})
        write_json(themes_dir / "dark.json", DEFAULT_THEME_DARK)
        write_json(themes_dir / "light.json", DEFAULT_THEME_LIGHT)

        # Path constants, mapped by their name in claudewheel.constants.
        self.sandbox_paths: dict[str, Path] = {
            "CONFIG_DIR": ld,
            "CONFIG_FILE": ld / "config.json",
            "SEGMENTS_FILE": ld / "segments.json",
            "OPTIONS_FILE": ld / "options.json",
            "STATE_FILE": ld / "state.json",
            "THEMES_DIR": themes_dir,
            "HOOKS_DIR": hooks_dir,
            "TOKENS_FILE": ld / "tokens.json",
            "PROFILES_DIR": profiles_dir,
            "SHARED_SETTINGS_FILE": ld / "shared-settings.json",
            "SCRIPTS_DIR": scripts_dir,
            "SHARED_DIR": shared_dir,
            "INODES_FILE": shared_dir / "inodes.json",
            "SKILLS_DIR": skills_dir,
        }

        if self.populate_default_profile:
            default_dir = self.home / ".claude"
            default_dir.mkdir(parents=True, exist_ok=True)
            (default_dir / ".credentials.json").write_text("{}")

    def make_profile(self, name: str, *, credentials: bool = True) -> Path:
        """Create ``<sandbox>/.claudewheel/profiles/<name>/`` and return it."""
        pdir = self.sandbox_paths["PROFILES_DIR"] / name
        pdir.mkdir(parents=True, exist_ok=True)
        if credentials:
            (pdir / ".credentials.json").write_text("{}")
        return pdir
