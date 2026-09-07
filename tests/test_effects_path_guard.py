"""The effects layer refuses a unittest.mock object handed to it as a path.

An unspecced ``MagicMock`` answers every attribute access with another mock,
and ``__fspath__`` on one of those returns a repr string -- so ``os.fspath``
succeeds and a filesystem effect quietly creates a real file or directory named
after the mock.  One such attribute access (``mock.scripts_dir`` forwarded into
``effects.mkdir``) materialized a directory tree in the repository root.  A mock
is not a path, so every path operand is refused at the boundary.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import effects

# ---------------------------------------------------------------------------
# The classification of every public effect function
# ---------------------------------------------------------------------------
#
# _PATH_INVOCATIONS maps each path-taking public effect to one call per path
# operand, each written with the mock in that operand's position and a real
# path in the others.  _NON_PATH_EFFECTS names the rest.  Together they must
# cover the module's whole public surface, which is what
# ``test_public_effect_surface_is_fully_classified`` asserts: a new public
# effect function has to be classified here before the suite goes green again,
# so this table cannot silently stop covering the module.
#
# Each invocation takes (mock_path, real_dir) and must raise TypeError in BOTH
# live and preview mode -- the hole this closes was a preview-mode branch that
# reached the handle without the guard, recorded nothing, and returned.

_Invocation = Callable[[Any, Path], object]

_PATH_INVOCATIONS: dict[str, tuple[_Invocation, ...]] = {
    "open_write": (lambda m, r: effects.open_write(m),),
    "write_text": (lambda m, r: effects.write_text(m, "x"),),
    "write_bytes": (lambda m, r: effects.write_bytes(m, b"x"),),
    "write": (lambda m, r: effects.write(m, "x"),),
    "write_text_atomic": (lambda m, r: effects.write_text_atomic(m, "x"),),
    "write_json_atomic": (lambda m, r: effects.write_json_atomic(m, {"a": 1}),),
    "write_json_atomic_secret": (
        lambda m, r: effects.write_json_atomic_secret(m, {"a": 1}),
    ),
    "mkdir": (lambda m, r: effects.mkdir(m),),
    "remove": (lambda m, r: effects.remove(m, missing_ok=True),),
    "rmdir": (lambda m, r: effects.rmdir(m),),
    "rmtree": (lambda m, r: effects.rmtree(m),),
    "chmod": (lambda m, r: effects.chmod(m, 0o644),),
    "rename": (
        lambda m, r: effects.rename(m, r / "dst"),
        lambda m, r: effects.rename(r / "src", m),
    ),
    "move": (
        lambda m, r: effects.move(m, r / "dst"),
        lambda m, r: effects.move(r / "src", m),
    ),
    "symlink": (
        lambda m, r: effects.symlink(m, r / "target"),
        lambda m, r: effects.symlink(r / "link", m),
    ),
    "copy_file": (
        lambda m, r: effects.copy_file(m, r / "dst"),
        lambda m, r: effects.copy_file(r / "src", m),
    ),
    "copytree": (
        lambda m, r: effects.copytree(m, r / "dst"),
        lambda m, r: effects.copytree(r / "src", m),
    ),
}

# Public effects that take no filesystem path operand: the mode/context
# predicates, the two output surfaces, the process effects (argv and an
# optional cwd, never a path the guard owns) and the HTTP effects.
_NON_PATH_EFFECTS = frozenset(
    {
        "bound",
        "unsettled",
        "previewing",
        "issue",
        "payload",
        "info",
        "run",
        "exec_replace",
        "kill",
        "run_under_pty",
        "http_read",
        "http_status",
        "http_stream",
        "http",
    }
)


def _public_effect_functions() -> set[str]:
    """Every public function defined in ``claudewheel.effects`` itself."""
    return {
        name
        for name, obj in vars(effects).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == effects.__name__
    }


class _RecordingHandle:
    """A stand-in for strictcli's effects handle that only records calls.

    Any method name is accepted, so the handle answers whatever branch of the
    chokepoint reaches it -- and ``calls`` is then the evidence of whether a
    refused operand still got as far as being recorded.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


class _PreviewCtx:
    """The minimum dispatch context ``effects._handle`` accepts as previewing."""

    def __init__(self, handle: _RecordingHandle) -> None:
        self.dry_run = True
        self.effects = handle


@contextmanager
def _preview() -> Iterator[_RecordingHandle]:
    """Bind a preview-mode dispatch context and yield its recording handle."""
    handle = _RecordingHandle()
    with effects.bound(_PreviewCtx(handle)):
        yield handle


class EffectsMockPathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _mock_path(self) -> object:
        """A mock attribute of the shape that materialized a real tree."""
        return mock.MagicMock(name="config")().scripts_dir

    def test_mkdir_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError) as cm:
            effects.mkdir(self._mock_path(), parents=True, exist_ok=True)
        self.assertIn("unittest.mock", str(cm.exception))
        # Nothing was created anywhere: the guard runs before the mkdir.
        self.assertEqual(sorted(p.name for p in self.tmp_path.iterdir()), [])

    def test_write_text_atomic_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.write_text_atomic(self._mock_path(), "content")

    def test_write_json_atomic_secret_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.write_json_atomic_secret(self._mock_path(), {"token": "s"})

    def test_write_text_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.write_text(self._mock_path(), "content")

    def test_remove_refuses_a_mock(self) -> None:
        with self.assertRaises(TypeError):
            effects.remove(self._mock_path(), missing_ok=True)

    def test_rename_refuses_a_mock_destination(self) -> None:
        src = self.tmp_path / "src.txt"
        src.write_text("x")
        with self.assertRaises(TypeError):
            effects.rename(src, self._mock_path())
        self.assertTrue(src.exists())

    def test_specced_path_mock_is_refused_too(self) -> None:
        # A spec'd mock still is not a path: passing the mock itself (rather
        # than a real path it returns) must fail loudly, not write somewhere.
        specced = mock.create_autospec(Path, instance=True)
        with self.assertRaises(TypeError):
            effects.mkdir(specced)

    def test_real_paths_still_work(self) -> None:
        target_dir = self.tmp_path / "real"
        effects.mkdir(target_dir)
        effects.write_text_atomic(target_dir / "a.txt", "content")
        effects.write_json_atomic_secret(target_dir / "b.json", {"token": "s"})
        self.assertEqual((target_dir / "a.txt").read_text(), "content")
        self.assertEqual((target_dir / "b.json").read_text(), '{\n  "token": "s"\n}\n')
        # A plain string operand is a path too, and must keep working.
        effects.write_text(str(target_dir / "c.txt"), "plain\n")
        self.assertEqual((target_dir / "c.txt").read_text(), "plain\n")


class EveryPathTakingEffectRefusesAMockTests(unittest.TestCase):
    """The guard is a property of the surface, not of the functions we recall.

    ``copytree`` had the guard on its live branch only, so a preview accepted a
    mock, recorded nothing and returned it -- the whole class of hole this
    enumeration closes, operand by operand, in both modes.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _mock_path(self) -> object:
        return mock.MagicMock(name="config")().scripts_dir

    def test_public_effect_surface_is_fully_classified(self) -> None:
        """A new public effect function must be classified before it ships."""
        classified = set(_PATH_INVOCATIONS) | set(_NON_PATH_EFFECTS)
        self.assertEqual(
            _public_effect_functions(),
            classified,
            "claudewheel.effects gained or lost a public function: add it to "
            "_PATH_INVOCATIONS (with one invocation per path operand) or to "
            "_NON_PATH_EFFECTS in this file",
        )

    def test_live_mode_refuses_a_mock_in_every_path_operand(self) -> None:
        for name, invocations in _PATH_INVOCATIONS.items():
            for index, invoke in enumerate(invocations):
                with self.subTest(effect=name, operand=index):
                    with self.assertRaises(TypeError) as cm:
                        invoke(self._mock_path(), self.tmp_path)
                    self.assertIn("unittest.mock", str(cm.exception))
        # Refused at the boundary means refused before anything was created.
        self.assertEqual(sorted(p.name for p in self.tmp_path.iterdir()), [])

    def test_preview_mode_refuses_a_mock_in_every_path_operand(self) -> None:
        for name, invocations in _PATH_INVOCATIONS.items():
            for index, invoke in enumerate(invocations):
                with self.subTest(effect=name, operand=index):
                    with _preview() as handle:
                        with self.assertRaises(TypeError) as cm:
                            invoke(self._mock_path(), self.tmp_path)
                        self.assertIn("unittest.mock", str(cm.exception))
                        # Nothing was recorded either: a preview that logged the
                        # mock's repr as a path would be a lie about the run.
                        self.assertEqual(handle.calls, [])
        self.assertEqual(sorted(p.name for p in self.tmp_path.iterdir()), [])

    def test_preview_mode_still_records_a_real_path(self) -> None:
        """The guard refuses mocks without disarming the preview itself."""
        with _preview() as handle:
            effects.write_text(self.tmp_path / "a.txt", "content")
        self.assertEqual([c[0] for c in handle.calls], ["write"])
        self.assertFalse((self.tmp_path / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
