"""Tests for claudewheel._detect_version() resolution order.

The package version must come from the repo-root package.json FIRST (source /
editable / npm checkouts), falling back to installed metadata only when that
file is absent (PyPI wheels), then to "unknown". This inversion matters because
editable-install metadata can be stale while package.json is authoritative.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import claudewheel


class DetectVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write_package_json(self, version: str) -> Path:
        p = self.tmp / "package.json"
        p.write_text(json.dumps({"name": "claudewheel", "version": version}))
        return p

    def test_file_wins_over_stale_metadata(self) -> None:
        # (a) file present + metadata present-but-stale -> file wins.
        pkg = self._write_package_json("0.20.0")
        result = claudewheel._detect_version(
            package_json=pkg,
            metadata_version=lambda name: "0.18.2",
        )
        self.assertEqual(result, "0.20.0")

    def test_metadata_used_when_file_absent(self) -> None:
        # (b) file absent + metadata present -> metadata used (PyPI wheel).
        missing = self.tmp / "package.json"  # never created
        self.assertFalse(missing.exists())
        result = claudewheel._detect_version(
            package_json=missing,
            metadata_version=lambda name: "0.19.1",
        )
        self.assertEqual(result, "0.19.1")

    def test_file_used_when_metadata_absent(self) -> None:
        # (c) npm-style: file present + no Python metadata -> file used.
        pkg = self._write_package_json("0.20.0")

        def _raise(name: str) -> str:
            raise PackageNotFoundError(name)

        result = claudewheel._detect_version(
            package_json=pkg,
            metadata_version=_raise,
        )
        self.assertEqual(result, "0.20.0")

    def test_unknown_when_nothing_available(self) -> None:
        # Final fallback: file absent + metadata raising -> "unknown".
        missing = self.tmp / "package.json"

        def _raise(name: str) -> str:
            raise PackageNotFoundError(name)

        result = claudewheel._detect_version(
            package_json=missing,
            metadata_version=_raise,
        )
        self.assertEqual(result, "unknown")

    def test_malformed_file_falls_back_to_metadata(self) -> None:
        # File present but unparseable -> fall through to metadata.
        p = self.tmp / "package.json"
        p.write_text("{ not valid json")
        result = claudewheel._detect_version(
            package_json=p,
            metadata_version=lambda name: "0.19.1",
        )
        self.assertEqual(result, "0.19.1")

    def test_real_checkout_reports_repo_version(self) -> None:
        # End-to-end: in this source checkout __version__ must match package.json.
        repo_pkg = claudewheel._default_package_json()
        self.assertTrue(repo_pkg.exists())
        expected = json.loads(repo_pkg.read_text())["version"]
        self.assertEqual(claudewheel.__version__, expected)


if __name__ == "__main__":
    unittest.main()
