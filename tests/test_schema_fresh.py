"""Schema-freshness guard: the committed .strictmetadata/.cli-schema/schema.json
must match the live CLI structure, modulo the non-structural version field.

The strictcli schema is checked into the repo and consumed by selfdoc. If the
CLI surface (commands, groups, flags, args, help text) drifts from the committed
schema, this test fails so the schema gets rewritten.

The committed file is the stdout of ``claudewheel help --json``, which rlsbl's
release step writes there. The fresh document is the same stdout, obtained
in-process through ``App.test(["help", "--json"])``: no subprocess and no
filesystem access. The comparison normalizes out ``version``, which the release
stamps with the version it ships.
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
_SCHEMA_RELPATH = Path(".strictmetadata") / ".cli-schema" / "schema.json"
_COMMITTED_SCHEMA = _REPO_ROOT / _SCHEMA_RELPATH


def _normalize(schema: dict[str, object]) -> dict[str, object]:
    """Drop the non-structural ``version`` field for comparison: the release
    stamps the committed document with the version it ships."""
    stripped = dict(schema)
    stripped.pop("version", None)
    return stripped


def _fresh_schema() -> dict[str, object]:
    """Return the live help document, as ``claudewheel help --json`` prints it
    on stdout. ``Workspace.default()`` and ``BinaryLocator.default()`` are pure
    value construction (no filesystem or terminal I/O), matching how ``main()``
    builds the app."""
    app = _build_app(Workspace.default(), BinaryLocator.default())
    result = app.test(["help", "--json"])
    assert result.exit_code == 0, result.stderr
    document: dict[str, object] = json.loads(result.stdout)
    return document


class SchemaFreshnessTests(unittest.TestCase):
    def test_committed_schema_matches_fresh_dump(self) -> None:
        committed = json.loads(_COMMITTED_SCHEMA.read_text())
        self.assertEqual(
            _normalize(_fresh_schema()),
            _normalize(committed),
            "committed .strictmetadata/.cli-schema/schema.json is stale -- "
            "write the stdout of `claudewheel help --json` to it and commit it",
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
