"""Tests for the autospec enforcement gate (scripts/gates/check-autospec).

The checker is an executable script (no ``.py`` suffix), so we load it as a
module via importlib from its filesystem path, then exercise its classification
functions against small synthetic sources covering every category and every
syntactic patch form (decorator, context manager, ``.start()`` assignment).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "gates" / "check-autospec"
    loader = importlib.machinery.SourceFileLoader("check_autospec", str(script))
    spec = importlib.util.spec_from_loader("check_autospec", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass() introspection can resolve the module.
    sys.modules["check_autospec"] = module
    loader.exec_module(module)
    return module


checker = _load_checker()


def _categories(source: str) -> list[str]:
    return [r.category for r in checker.classify_source(source)]


def _one(source: str) -> str:
    cats = _categories(source)
    assert len(cats) == 1, f"expected exactly one patch site, got {cats}"
    return cats[0]


class BareDetectionTests(unittest.TestCase):
    def test_bare_patch_string_target(self) -> None:
        self.assertEqual(_one("patch('mod.thing')"), checker.BARE)

    def test_bare_mock_patch(self) -> None:
        self.assertEqual(_one("mock.patch('mod.thing')"), checker.BARE)

    def test_bare_patch_object(self) -> None:
        self.assertEqual(_one("patch.object(Foo, 'bar')"), checker.BARE)

    def test_bare_mock_patch_object(self) -> None:
        self.assertEqual(_one("mock.patch.object(Foo, 'bar')"), checker.BARE)

    def test_bare_with_unrelated_kwargs(self) -> None:
        # return_value / side_effect do NOT spec the mock.
        self.assertEqual(
            _one("patch('mod.thing', return_value=5)"), checker.BARE
        )

    def test_autospec_false_is_bare(self) -> None:
        self.assertEqual(_one("patch('mod.thing', autospec=False)"), checker.BARE)

    def test_autospec_none_is_bare(self) -> None:
        self.assertEqual(_one("patch('mod.thing', autospec=None)"), checker.BARE)


class AutospecTests(unittest.TestCase):
    def test_autospec_true(self) -> None:
        self.assertEqual(_one("patch('mod.thing', autospec=True)"), checker.AUTOSPEC)

    def test_autospec_object(self) -> None:
        self.assertEqual(
            _one("patch.object(Foo, 'bar', autospec=True)"), checker.AUTOSPEC
        )

    def test_autospec_with_return_value(self) -> None:
        self.assertEqual(
            _one("patch.object(Foo, 'bar', autospec=True, return_value=1)"),
            checker.AUTOSPEC,
        )


class SpecTests(unittest.TestCase):
    def test_spec(self) -> None:
        self.assertEqual(_one("patch('mod.thing', spec=Foo)"), checker.SPEC)

    def test_spec_set(self) -> None:
        self.assertEqual(_one("patch('mod.thing', spec_set=Foo)"), checker.SPEC)


class DictTests(unittest.TestCase):
    def test_patch_dict(self) -> None:
        self.assertEqual(_one("patch.dict(os.environ, {'X': '1'})"), checker.DICT)

    def test_mock_patch_dict(self) -> None:
        self.assertEqual(
            _one("mock.patch.dict('os.environ', {'X': '1'})"), checker.DICT
        )


class NewTests(unittest.TestCase):
    def test_new_kwarg(self) -> None:
        self.assertEqual(_one("patch('mod.thing', new=sentinel)"), checker.NEW)

    def test_new_callable_kwarg(self) -> None:
        self.assertEqual(
            _one("patch('mod.thing', new_callable=MagicMock)"), checker.NEW
        )

    def test_positional_new_on_patch(self) -> None:
        # patch(target, new) -- second positional is the replacement.
        self.assertEqual(_one("patch('mod.thing', my_replacement)"), checker.NEW)

    def test_positional_new_on_patch_object(self) -> None:
        # patch.object(target, attr, new) -- third positional is the replacement.
        self.assertEqual(
            _one("patch.object(Foo, 'bar', my_replacement)"), checker.NEW
        )

    def test_two_positionals_on_object_is_bare(self) -> None:
        # patch.object(target, attr) with no replacement is still bare.
        self.assertEqual(_one("patch.object(Foo, 'bar')"), checker.BARE)


class SyntacticFormTests(unittest.TestCase):
    """Every syntactic patch form reduces to the same Call node."""

    def test_decorator_bare(self) -> None:
        src = (
            "@patch('mod.thing')\n"
            "def test_it(mock_thing):\n"
            "    pass\n"
        )
        self.assertEqual(_one(src), checker.BARE)

    def test_decorator_autospec(self) -> None:
        src = (
            "@mock.patch('mod.thing', autospec=True)\n"
            "def test_it(mock_thing):\n"
            "    pass\n"
        )
        self.assertEqual(_one(src), checker.AUTOSPEC)

    def test_context_manager_bare(self) -> None:
        src = (
            "def test_it():\n"
            "    with patch('mod.thing') as m:\n"
            "        m()\n"
        )
        self.assertEqual(_one(src), checker.BARE)

    def test_context_manager_autospec(self) -> None:
        src = (
            "def test_it():\n"
            "    with mock.patch.object(Foo, 'bar', autospec=True) as m:\n"
            "        m()\n"
        )
        self.assertEqual(_one(src), checker.AUTOSPEC)

    def test_start_assignment_bare(self) -> None:
        src = (
            "def setUp(self):\n"
            "    self._p = patch('mod.thing')\n"
            "    self._p.start()\n"
        )
        # Only the patch(...) call is a patch site; .start() is not counted.
        self.assertEqual(_one(src), checker.BARE)

    def test_start_assignment_autospec(self) -> None:
        src = (
            "def setUp(self):\n"
            "    self._p = patch.object(Foo, 'bar', autospec=True, return_value=1)\n"
            "    self._p.start()\n"
        )
        self.assertEqual(_one(src), checker.AUTOSPEC)


class NonPatchTests(unittest.TestCase):
    """Things that merely look like patches must not be classified."""

    def test_start_call_not_counted(self) -> None:
        self.assertEqual(_categories("self._home_patch.start()"), [])

    def test_variable_ending_in_patch_not_counted(self) -> None:
        self.assertEqual(_categories("my_patch('x')"), [])

    def test_unrelated_object_method_not_counted(self) -> None:
        self.assertEqual(_categories("thing.dict('x')"), [])

    def test_chained_start_counts_inner_only(self) -> None:
        # mock.patch(...).start() -- the inner patch call is the only site.
        self.assertEqual(_categories("mock.patch('mod.thing').start()"), [checker.BARE])


class ChmodAndCliTests(unittest.TestCase):
    def test_script_is_executable(self) -> None:
        import os
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "gates" / "check-autospec"
        self.assertTrue(os.access(script, os.X_OK), "checker must be chmod +x")

    def test_main_returns_nonzero_on_bare(self) -> None:
        # Point main at this test file's own dir via a temp file with a bare patch.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.py"
            p.write_text("patch('x')\n")
            rc = checker.main(["check-autospec", str(p)])
        self.assertEqual(rc, 1)

    def test_main_returns_zero_when_clean(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "good.py"
            p.write_text("patch('x', autospec=True)\n")
            rc = checker.main(["check-autospec", str(p)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
