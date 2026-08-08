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

import contextlib
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable, Iterator
from unittest.mock import patch

from claudewheel.binaries import BinaryLocator
from claudewheel.config import AppConfigStore
from claudewheel.shared_store import SharedStore
from claudewheel.terminal import Terminal
from claudewheel.workspace import Workspace
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


class FakeTerminal(Terminal):
    """A mock Terminal that feeds pre-recorded keystrokes and captures output.

    The single shared test double for :class:`claudewheel.terminal.Terminal`.
    It records lifecycle calls (``enter_raw_calls``, ``exit_raw_called``,
    ``closed``) so tests can assert on raw-mode transitions, and buffers all
    written text in ``output``. ``read_key`` returns pre-recorded keys and, once
    exhausted, yields ``"ESC"`` as a safety net so an interactive loop cancels
    instead of hanging.
    """

    def __init__(self, keys: list[str], in_raw: bool = False) -> None:
        self._keys = list(keys)
        self._index = 0
        self.rows = 40
        self.cols = 120
        self.output: list[str] = []
        self.enter_raw_calls: list[bool] = []
        self.exit_raw_called = False
        self.closed = False
        self._in_raw = in_raw
        # cooked() reads this to restore the alt-screen flag symmetrically.
        self._alt_screen = True

    def enter_raw(self, alt_screen: bool = True) -> None:
        self.enter_raw_calls.append(alt_screen)
        self._in_raw = True

    def exit_raw(self) -> None:
        self.exit_raw_called = True
        self._in_raw = False

    def close(self) -> None:
        self.closed = True

    def get_size(self) -> tuple[int, int]:
        return self.rows, self.cols

    def read_key(self) -> str:
        if self._index >= len(self._keys):
            # Safety net: if keys are exhausted, cancel the interactive loop.
            return "ESC"
        key = self._keys[self._index]
        self._index += 1
        return key

    def write(self, text: str) -> None:
        self.output.append(text)

    def flush(self) -> None:
        pass


class FakeAppConfigStore(AppConfigStore):
    """A no-I/O :class:`claudewheel.config.AppConfigStore` for launch-path tests.

    The real store's ``__post_init__`` is eager: it creates directories, reads
    four JSON files, runs migrations and materializes shared-settings. Tests
    that drive ``cli._do_launch_sequence`` need none of that -- they need an
    object carrying the three attributes the launch path reads. Overriding
    ``__init__`` (and never calling ``super().__init__``) skips the dataclass
    constructor and its ``__post_init__`` entirely, the same way
    :class:`FakeTerminal` subclasses ``Terminal`` without adopting its I/O.

    Subclassing the real type rather than duck-typing a bare stand-in is what
    lets call sites pass this where an ``AppConfigStore`` is annotated, with no
    ``type: ignore`` at the boundary.

    ``theme`` is present but inert: every launch-path reader spells the lookup
    ``cfg.config.get("theme", "auto")``, so its presence changes nothing, and
    :meth:`load_theme` returns an empty dict instead of touching the themes
    directory the real store would have built.
    """

    def __init__(self) -> None:
        # health_check_on_launch False so the health block is a no-op and tests
        # do not need to stub run_health_check.
        self.config: dict[str, Any] = {
            "health_check_on_launch": False,
            "default_flags": [],
            "clients": {},
            "theme": "auto",
        }
        self.options_def: dict[str, Any] = {}
        self.state: dict[str, Any] = {}

    def load_theme(self, name: str) -> dict[str, Any]:  # pragma: no cover - inert
        return {}


def inert_workspace(root: Path) -> Workspace:
    """A real :class:`Workspace` at *root* for tests that never read its paths.

    ``Workspace.open`` is pure value assembly -- zero filesystem I/O -- so this
    hands back the genuine type without creating anything. *claude_dir* is
    pinned under *root* rather than defaulted, so no ``Path.home()`` lookup
    leaks into a test that has not sandboxed its home.
    """
    return Workspace.open(root=root, claude_dir=root / ".claude")


def inert_locator(root: Path) -> BinaryLocator:
    """A real :class:`BinaryLocator` under *root* for tests that never read it.

    Like :func:`inert_workspace`, construction is pure: ``BinaryLocator`` is a
    dataclass of two paths and touches neither at build time.
    """
    return BinaryLocator(
        versions_dir=root / "versions",
        claude_symlink=root / "bin" / "claude",
    )


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
# Write canary: prove "cw never writes ~/.claude" at the write chokepoint.
#
# All production settings/state writes funnel through the atomic writers in
# ``claudewheel.fsutil`` (``write_text_atomic``, ``write_json_atomic``,
# ``write_json_atomic_secret``). Each one commits its result with a single
# ``tmp.rename(target)`` -- so ``pathlib.Path.rename`` is the one shared seam
# every writer passes through, no matter how the calling module imported the
# writer. Interposing there catches any commit whose destination lands under
# ``claude_dir``, which is the invariant a vanilla-default launch must uphold.
# ---------------------------------------------------------------------------


class ClaudeDirWriteViolation(BaseException):
    """Raised by :func:`claude_dir_write_canary` on a write under ``claude_dir``.

    Deliberately subclasses ``BaseException`` -- NOT ``Exception`` -- so that a
    broad ``except Exception`` in a launch step (e.g. the non-fatal reconcile
    heal in ``preflight._reconcile_guardrails_run``) cannot silently swallow the
    canary and let a genuine violation slip through as a green test. It has to
    tear all the way out to the test body.

    ``offending_path`` is the destination path that tripped the canary.
    ``stray_tmp_files`` is populated by the canary's exit cleanup with any
    ``*.tmp`` staging files that a tripped atomic writer left behind under
    ``claude_dir`` (the writer stages ``<target>.tmp`` BEFORE the rename that
    trips, so the stray can outlive the aborted commit). It is an empty list
    when nothing was left behind.
    """

    def __init__(self, path: Path) -> None:
        self.offending_path = Path(path)
        self.stray_tmp_files: list[Path] = []
        super().__init__(
            "CLAUDE_DIR WRITE CANARY TRIPPED: production code attempted to "
            f"write under the vanilla ~/.claude at {self.offending_path!s}. "
            "Nothing may write to claude_dir during a launch flow except the "
            "sanctioned opt-in guardrail injection "
            "(preflight.ensure_vanilla_guardrails / remove_vanilla_guardrails)."
        )


def _path_is_under(path: Path, root: Path) -> bool:
    """True when *path* resolves to *root* or a descendant of *root*.

    Both sides are resolved so a tmpdir behind a symlinked ``/tmp`` (or a
    ``.tmp`` staging path) compares correctly against ``claude_dir``.
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# The staging-file suffix that ``claudewheel.fsutil`` atomic writers use before
# their committing rename (``path.with_suffix(".tmp")``). The byte-level
# ``write_text``/``write_bytes`` guard skips these so it does not double-fire on
# a sanctioned atomic writer's own staging write -- that write is authoritative
# only once it reaches the rename seam, which is guarded separately.
_FSUTIL_STAGING_SUFFIX = ".tmp"


def _scan_files_under(root: Path) -> set[str]:
    """Return the resolved-string paths of every regular file under *root*.

    Walks with :func:`os.walk` (no symlink following). Used to snapshot
    ``claude_dir`` at canary entry so exit cleanup can identify -- and only
    ever delete -- files that did NOT exist at entry.
    """
    found: set[str] = set()
    if not root.exists():
        return found
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            found.add(str(Path(dp) / f))
    return found


@contextlib.contextmanager
def claude_dir_write_canary(claude_dir: Path) -> Iterator[None]:
    """Trip loudly if any production write lands a file under *claude_dir*.

    Interposes three ``pathlib.Path`` seams and raises
    :class:`ClaudeDirWriteViolation` naming the destination whenever a write
    targets a path inside *claude_dir*:

    - ``Path.rename`` -- the shared commit seam of every ``claudewheel.fsutil``
      atomic writer (``write_text_atomic``/``write_json_atomic``/
      ``write_json_atomic_secret``). This is the primary seam: every sanctioned
      settings/state/token write funnels through a ``tmp.rename(target)``.
    - ``Path.write_text`` and ``Path.write_bytes`` -- byte-level writers that do
      NOT rename-commit and would otherwise bypass the rename seam entirely
      (e.g. ``hook_scripts.deploy_scripts`` uses ``dest.write_text(...)``
      directly). These guards skip the fsutil ``*.tmp`` staging convention
      (see ``_FSUTIL_STAGING_SUFFIX``) so an atomic writer's own staging write
      passes through and is caught at its rename instead -- keeping the tripped
      ``offending_path`` the real target, not the ``.tmp``.

    Writes whose destination is outside *claude_dir* delegate to the real
    ``Path`` method unchanged, so writes to the ``~/.claudewheel`` store
    (managed profiles, shared-settings, state) behave exactly as in production.

    Known, deliberate limit: this canary does NOT interpose ``os.open`` or
    ``builtins.open``. Those are too broad -- the fsutil secret writer and
    ``install.py`` open raw fds, and every ``tempfile`` allocation would churn
    through them -- so a hypothetical raw-fd writer that targets *claude_dir*
    without a rename commit would slip past. The interposed seams (rename +
    Path byte writers) cover every writer that exists in production today.

    Exit cleanup: fsutil stages ``<target>.tmp`` under the target's directory
    BEFORE the committing rename, so a rename that trips at *claude_dir* can
    leave that ``.tmp`` stray behind. On context exit (in a ``finally``) the
    canary rescans *claude_dir* and deletes -- via direct ``unlink`` -- every
    regular file that did NOT exist at entry, leaving the guarded tree byte-for-
    byte as it was found. This cleanup is deterministic and only ever touches
    files under *claude_dir* that appeared during the context. When a violation
    was raised, the ``*.tmp`` strays among the removed files are reported on the
    exception's ``stray_tmp_files``.
    """
    claude_dir = Path(claude_dir)
    orig_rename = Path.rename
    orig_write_text = Path.write_text
    orig_write_bytes = Path.write_bytes

    def _guarded_rename(self: Path, target: Any) -> Any:
        dest = Path(target)
        if _path_is_under(dest, claude_dir):
            raise ClaudeDirWriteViolation(dest)
        return orig_rename(self, target)

    def _guarded_write_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(_FSUTIL_STAGING_SUFFIX):
            return orig_write_text(self, *args, **kwargs)
        if _path_is_under(self, claude_dir):
            raise ClaudeDirWriteViolation(self)
        return orig_write_text(self, *args, **kwargs)

    def _guarded_write_bytes(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith(_FSUTIL_STAGING_SUFFIX):
            return orig_write_bytes(self, *args, **kwargs)
        if _path_is_under(self, claude_dir):
            raise ClaudeDirWriteViolation(self)
        return orig_write_bytes(self, *args, **kwargs)

    entry_files = _scan_files_under(claude_dir)
    violation: ClaudeDirWriteViolation | None = None
    try:
        with (
            patch.object(Path, "rename", new=_guarded_rename),
            patch.object(Path, "write_text", new=_guarded_write_text),
            patch.object(Path, "write_bytes", new=_guarded_write_bytes),
        ):
            try:
                yield
            except ClaudeDirWriteViolation as exc:
                violation = exc
                raise
    finally:
        # Deterministic exit cleanup: remove only files that appeared under
        # claude_dir during the context (strays a tripped writer left behind).
        strays: list[Path] = []
        for f in sorted(_scan_files_under(claude_dir) - entry_files):
            p = Path(f)
            if not _path_is_under(p, claude_dir):
                continue  # defensive: never delete outside the guarded root
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            strays.append(p)
        if violation is not None:
            violation.stray_tmp_files = [
                p for p in strays if p.suffix == _FSUTIL_STAGING_SUFFIX
            ]


class ClaudeDirWriteCanaryMixin:
    """TestCase mixin exposing :meth:`claude_dir_write_canary` as a helper.

    Mix into a ``TestCase`` that carries a ``self.claude_dir`` (all the launch
    integration cases do) and wrap the flow-under-test::

        with self.claude_dir_write_canary():
            self._launch("default")

    The canary defaults to ``self.claude_dir`` but accepts an override for the
    rare case that needs a different root.
    """

    claude_dir: Path

    def claude_dir_write_canary(
        self, claude_dir: Path | None = None
    ) -> "contextlib.AbstractContextManager[None]":
        return claude_dir_write_canary(
            self.claude_dir if claude_dir is None else claude_dir
        )


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
