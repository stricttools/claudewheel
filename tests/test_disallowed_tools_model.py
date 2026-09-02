"""Tests for the disallowed-tools model and the docs table generated from it.

``claudewheel.guardrail.DISALLOWED_TOOL_ENTRIES`` is the single authority for
the tools claudewheel strips from every launched session. These pin its shape
(non-empty name and reason, unique, alphabetical), pin that
``claudewheel.defaults.DISALLOWED_TOOLS`` is derived from it rather than
maintained separately, and exercise the selfdoc directive that renders it into
the guardrails page so a broken directive fails here instead of silently
emitting a failure sentinel into the docs.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

from claudewheel import guardrail
from claudewheel.defaults import DISALLOWED_TOOLS
from claudewheel.guardrail import DISALLOWED_TOOL_ENTRIES


# Repo root derived from this file, never from the process cwd (the test
# isolation floor chdirs each test into its own temp directory).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIRECTIVE_PATH = _REPO_ROOT / "docs" / "_directives" / "disallowed_tools_table.py"

# selfdoc renders a failed custom directive as a visible blockquote sentinel
# instead of raising; a resolve() that returns one is a failure, not a table.
_FAILURE_SENTINEL_PREFIX = "> *[selfdoc:"


def _load_directive() -> ModuleType:
    """Load the selfdoc directive module by file path and return it."""
    module_name = "disallowed_tools_table_under_test"
    spec = importlib.util.spec_from_file_location(module_name, _DIRECTIVE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DisallowedToolEntriesTests(unittest.TestCase):
    """The canonical entries tuple's own invariants."""

    def test_every_entry_has_a_name_and_a_reason(self) -> None:
        for entry in DISALLOWED_TOOL_ENTRIES:
            self.assertTrue(entry.name.strip(), f"empty name: {entry!r}")
            self.assertTrue(entry.why.strip(), f"empty why for {entry.name}")

    def test_names_are_unique(self) -> None:
        names = [entry.name for entry in DISALLOWED_TOOL_ENTRIES]
        self.assertEqual(len(names), len(set(names)))

    def test_names_are_alphabetical(self) -> None:
        names = [entry.name for entry in DISALLOWED_TOOL_ENTRIES]
        self.assertEqual(names, sorted(names))


class DerivationTests(unittest.TestCase):
    """defaults.DISALLOWED_TOOLS is derived, not maintained separately."""

    def test_defaults_list_matches_the_model(self) -> None:
        self.assertEqual(
            DISALLOWED_TOOLS,
            [entry.name for entry in guardrail.DISALLOWED_TOOL_ENTRIES],
        )

    def test_helper_returns_the_names_in_order(self) -> None:
        self.assertEqual(
            guardrail.disallowed_tool_names(),
            [entry.name for entry in guardrail.DISALLOWED_TOOL_ENTRIES],
        )


class DirectiveTests(unittest.TestCase):
    """The selfdoc directive resolves to a real table over the model."""

    def test_resolve_renders_every_tool(self) -> None:
        module = _load_directive()
        rendered = module.resolve({}, {}, [])

        self.assertFalse(
            rendered.startswith(_FAILURE_SENTINEL_PREFIX),
            f"directive returned a selfdoc failure sentinel: {rendered!r}",
        )
        self.assertIn("Tool", rendered)
        self.assertIn("Why", rendered)
        for entry in DISALLOWED_TOOL_ENTRIES:
            self.assertIn(entry.name, rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
