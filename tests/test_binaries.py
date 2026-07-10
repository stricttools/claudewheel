"""Tests for the BinaryLocator Claude Code binary locator."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from claudewheel.binaries import BinaryLocator

from .wheelhelpers import SandboxHomeTestCase


class BinaryLocatorTest(SandboxHomeTestCase):
    """Exercise BinaryLocator against a sandboxed fake home and versions dir."""

    def setUp(self) -> None:
        super().setUp()
        # Fixture: a versions dir with two fake version binaries plus a
        # stray subdirectory (must be ignored -- only files count).
        self.versions_dir = self.home / ".local/share/claude/versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        (self.versions_dir / "2.1.9").write_text("#!/bin/sh\n")
        (self.versions_dir / "2.1.120").write_text("#!/bin/sh\n")
        (self.versions_dir / "not-a-version-dir").mkdir()

        self.symlink_path = self.home / ".local/bin/claude"
        self.symlink_path.parent.mkdir(parents=True, exist_ok=True)

        self.loc = BinaryLocator(
            versions_dir=self.versions_dir,
            claude_symlink=self.symlink_path,
        )

    # -- default() ---------------------------------------------------------

    def test_default_computes_under_home_at_call_time(self) -> None:
        loc = BinaryLocator.default()
        self.assertEqual(loc.versions_dir, self.home / ".local/share/claude/versions")
        self.assertEqual(loc.claude_symlink, self.home / ".local/bin/claude")

    # -- binary_for --------------------------------------------------------

    def test_binary_for_composes_version_path(self) -> None:
        self.assertEqual(
            self.loc.binary_for("2.1.120"),
            self.versions_dir / "2.1.120",
        )

    def test_binary_for_matches_launch_composition(self) -> None:
        # launch.py builds VERSIONS_DIR / version; binary_for must match.
        version = "2.1.9"
        self.assertEqual(self.loc.binary_for(version), self.versions_dir / version)

    # -- fallback ----------------------------------------------------------

    def test_fallback_is_symlink(self) -> None:
        self.assertEqual(self.loc.fallback, self.symlink_path)

    # -- installed_versions ------------------------------------------------

    def test_installed_versions_sorted_descending_files_only(self) -> None:
        # Numeric version sort: 2.1.120 sorts above 2.1.9. Directory excluded.
        self.assertEqual(self.loc.installed_versions(), ["2.1.120", "2.1.9"])

    def test_installed_versions_empty_when_dir_missing(self) -> None:
        loc = BinaryLocator(
            versions_dir=self.home / "nonexistent",
            claude_symlink=self.symlink_path,
        )
        self.assertEqual(loc.installed_versions(), [])

    # -- symlink_target ----------------------------------------------------

    def test_symlink_target_resolves_when_present(self) -> None:
        self.symlink_path.symlink_to(self.versions_dir / "2.1.120")
        target = self.loc.symlink_target()
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "2.1.120")

    def test_symlink_target_none_when_absent(self) -> None:
        self.assertIsNone(self.loc.symlink_target())

    def test_symlink_target_none_when_broken(self) -> None:
        # Broken symlink: exists() is False, is_symlink() is True; resolve
        # returns a path, but .name still reflects the dangling target.
        self.symlink_path.symlink_to(self.versions_dir / "gone")
        target = self.loc.symlink_target()
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "gone")

    # -- frozen dataclass --------------------------------------------------

    def test_is_frozen_dataclass(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(self.loc))
        params = self.loc.__dataclass_params__
        self.assertTrue(params.frozen)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.loc.versions_dir = Path("/elsewhere")  # type: ignore[misc]
