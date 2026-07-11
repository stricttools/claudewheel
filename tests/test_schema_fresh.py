"""Schema-freshness guard: the committed .strictcli/schema.json must match the
live CLI structure, modulo the non-structural version/project_id fields.

The strictcli schema is checked into the repo and consumed by selfdoc. If the
CLI surface (commands, groups, flags, args, help text) drifts from the committed
schema, this test fails so the schema gets re-dumped.

The fresh schema is obtained in-process via ``App.dump_schema_dict()`` (strictcli
>= 0.27.0): no subprocess, no throwaway temp cwd, no filesystem access. The
returned dict is byte-identical to the written ``.strictcli/schema.json`` with
the ``project_id`` field removed, so the comparison normalizes out both
``project_id`` (absent from the in-process dump, present in the committed file)
and ``version`` (changes every release, not structural).
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from claudewheel.binaries import BinaryLocator
from claudewheel.cli import _build_app
from claudewheel.workspace import Workspace

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMMITTED_SCHEMA = _REPO_ROOT / ".strictcli" / "schema.json"


def _normalize(schema: dict) -> dict:
    """Drop the non-structural fields (version + project_id) for comparison.

    ``version`` changes every release; ``project_id`` is added only by the
    ``--dump-schema`` writer path (from pyproject.toml) and is absent from the
    in-process ``dump_schema_dict()`` result.
    """
    stripped = dict(schema)
    stripped.pop("version", None)
    stripped.pop("project_id", None)
    return stripped


def _fresh_schema() -> dict:
    """Return the live CLI schema in-process. ``Workspace.default()`` and
    ``BinaryLocator.default()`` are pure value construction (no filesystem or
    terminal I/O), matching how ``main()`` builds the app."""
    app = _build_app(Workspace.default(), BinaryLocator.default())
    return app.dump_schema_dict()


class SchemaFreshnessTests(unittest.TestCase):
    def test_committed_schema_matches_fresh_dump(self) -> None:
        committed = json.loads(_COMMITTED_SCHEMA.read_text())
        self.assertEqual(
            _normalize(_fresh_schema()),
            _normalize(committed),
            "committed .strictcli/schema.json is stale -- re-run "
            "`claudewheel --dump-schema` from the repo root and commit it",
        )

    def test_guard_detects_structural_drift(self) -> None:
        # Meta-test: prove the normalized comparison is not a no-op by injecting
        # a fake command into a copy of the committed schema and asserting the
        # guard would reject it.
        committed = json.loads(_COMMITTED_SCHEMA.read_text())
        mutated = copy.deepcopy(committed)
        mutated["commands"]["__meta_test_fake_command__"] = {
            "name": "__meta_test_fake_command__",
            "help": "injected by the freshness meta-test",
        }
        self.assertNotEqual(
            _normalize(mutated),
            _normalize(committed),
            "the freshness comparison failed to detect an injected command; "
            "the guard would be a no-op",
        )

    def test_guard_detects_flag_change(self) -> None:
        # A second mutation shape: changing a flag/help value must also be
        # caught by the normalized-equality comparison.
        committed = json.loads(_COMMITTED_SCHEMA.read_text())
        mutated = copy.deepcopy(committed)
        mutated["help"] = committed.get("help", "") + " MUTATED"
        self.assertNotEqual(
            _normalize(mutated),
            _normalize(committed),
        )


if __name__ == "__main__":
    unittest.main()
