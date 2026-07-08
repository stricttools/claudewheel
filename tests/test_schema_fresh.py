"""Schema-freshness guard: the committed .strictcli/schema.json must match a
fresh `claudewheel --dump-schema`, modulo the version field.

The strictcli schema is checked into the repo and consumed by selfdoc. If the
CLI surface (commands, groups, flags, args, help text) drifts from the committed
schema, this test fails so the schema gets re-dumped. Only the ``version`` field
is normalized out -- it changes every release and is not structural.

The fresh dump runs once at module scope. strictcli's --dump-schema reads
``[project].name`` from a pyproject.toml in the cwd and writes
``<cwd>/.strictcli/schema.json``; the test runs it in a throwaway temp dir so it
never touches the committed file.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMMITTED_SCHEMA = _REPO_ROOT / ".strictcli" / "schema.json"

# Populated by setUpModule so the (relatively slow) subprocess dump runs once.
_FRESH_SCHEMA: dict | None = None


def _strip_version(schema: dict) -> dict:
    """Return a copy of the schema with the non-structural version field removed."""
    stripped = dict(schema)
    stripped.pop("version", None)
    return stripped


def setUpModule() -> None:
    global _FRESH_SCHEMA
    tmp = tempfile.mkdtemp(prefix="cw-schema-fresh-")
    tmp_path = Path(tmp)
    # strictcli derives project_id from [project].name; it must match the
    # committed schema's project_id for a structural comparison.
    committed = json.loads(_COMMITTED_SCHEMA.read_text())
    project_id = committed["project_id"]
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "{project_id}"\nversion = "0.0.0"\n'
    )
    result = subprocess.run(
        [sys.executable, "-m", "claudewheel", "--dump-schema"],
        cwd=tmp,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"--dump-schema failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    fresh_path = tmp_path / ".strictcli" / "schema.json"
    _FRESH_SCHEMA = json.loads(fresh_path.read_text())


class SchemaFreshnessTests(unittest.TestCase):
    def test_committed_schema_matches_fresh_dump(self) -> None:
        committed = json.loads(_COMMITTED_SCHEMA.read_text())
        self.assertEqual(
            _strip_version(_FRESH_SCHEMA),
            _strip_version(committed),
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
            _strip_version(mutated),
            _strip_version(committed),
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
            _strip_version(mutated),
            _strip_version(committed),
        )


if __name__ == "__main__":
    unittest.main()
