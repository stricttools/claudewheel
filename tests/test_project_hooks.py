"""Tests for the project-hooks reader (claudewheel.project_hooks).

Covers Phase 2.1: reading ``.claude/settings.json`` and ``settings.local.json``,
extracting each file's hooks section, fingerprint stability/distinctness, the
malformed-JSON hard error naming the file, and the flattened human-readable
listing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claudewheel.project_hooks import (
    MalformedProjectHooksError,
    ProjectHooks,
    read_project_hooks,
    target_directory,
)

_POPULATED = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo pre"}],
            }
        ],
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "echo submit"}]}
        ],
    }
}


class _ProjectDir:
    """A throwaway project directory with an optional ``.claude`` config."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)

    def write(self, name: str, data: Any, *, raw: str | None = None) -> None:
        claude = self.path / ".claude"
        claude.mkdir(exist_ok=True)
        target = claude / name
        if raw is not None:
            target.write_text(raw)
        else:
            target.write_text(json.dumps(data))

    def cleanup(self) -> None:
        self._tmp.cleanup()


class ReadProjectHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proj = _ProjectDir()
        self.addCleanup(self.proj.cleanup)

    def test_absent_config_has_no_hooks(self) -> None:
        ph = read_project_hooks(str(self.proj.path))
        self.assertFalse(ph.has_hooks)
        self.assertEqual(ph.sources, {})

    def test_absent_hooks_key_has_no_hooks(self) -> None:
        self.proj.write("settings.json", {"permissions": {"deny": []}})
        ph = read_project_hooks(str(self.proj.path))
        self.assertFalse(ph.has_hooks)

    def test_empty_hooks_section_has_no_hooks(self) -> None:
        self.proj.write("settings.json", {"hooks": {}})
        ph = read_project_hooks(str(self.proj.path))
        self.assertFalse(ph.has_hooks)

    def test_populated_config_has_hooks(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        ph = read_project_hooks(str(self.proj.path))
        self.assertTrue(ph.has_hooks)
        self.assertIn("settings.json", ph.sources)
        self.assertEqual(ph.sources["settings.json"], _POPULATED["hooks"])

    def test_both_files_are_combined(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        self.proj.write(
            "settings.local.json",
            {"hooks": {"Stop": [{"hooks": [{"command": "echo stop"}]}]}},
        )
        ph = read_project_hooks(str(self.proj.path))
        self.assertEqual(set(ph.sources), {"settings.json", "settings.local.json"})

    def test_malformed_settings_json_raises_with_filename(self) -> None:
        self.proj.write("settings.json", None, raw="{not valid json")
        with self.assertRaises(MalformedProjectHooksError) as cm:
            read_project_hooks(str(self.proj.path))
        self.assertEqual(cm.exception.filename, "settings.json")

    def test_malformed_settings_local_json_raises_with_filename(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        self.proj.write("settings.local.json", None, raw="}{")
        with self.assertRaises(MalformedProjectHooksError) as cm:
            read_project_hooks(str(self.proj.path))
        self.assertEqual(cm.exception.filename, "settings.local.json")


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proj = _ProjectDir()
        self.addCleanup(self.proj.cleanup)

    def test_same_content_same_fingerprint(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        fp1 = read_project_hooks(str(self.proj.path)).fingerprint
        fp2 = read_project_hooks(str(self.proj.path)).fingerprint
        self.assertEqual(fp1, fp2)

    def test_key_order_does_not_change_fingerprint(self) -> None:
        # Canonical serialization sorts keys, so re-ordering must not matter.
        a = ProjectHooks(sources={"settings.json": {"A": 1, "B": 2}})
        b = ProjectHooks(sources={"settings.json": {"B": 2, "A": 1}})
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_distinct_content_distinct_fingerprint(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        fp_pop = read_project_hooks(str(self.proj.path)).fingerprint

        other = _ProjectDir()
        self.addCleanup(other.cleanup)
        other.write(
            "settings.json",
            {"hooks": {"Stop": [{"hooks": [{"command": "rm -rf /"}]}]}},
        )
        fp_other = read_project_hooks(str(other.path)).fingerprint
        self.assertNotEqual(fp_pop, fp_other)

    def test_moving_hook_between_files_changes_fingerprint(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        fp_in_main = read_project_hooks(str(self.proj.path)).fingerprint

        moved = _ProjectDir()
        self.addCleanup(moved.cleanup)
        moved.write("settings.local.json", _POPULATED)
        fp_in_local = read_project_hooks(str(moved.path)).fingerprint
        self.assertNotEqual(fp_in_main, fp_in_local)

    def test_no_hooks_fingerprint_is_stable(self) -> None:
        absent = read_project_hooks(str(self.proj.path)).fingerprint
        self.proj.write("settings.json", {"hooks": {}})
        empty = read_project_hooks(str(self.proj.path)).fingerprint
        self.assertEqual(absent, empty)


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proj = _ProjectDir()
        self.addCleanup(self.proj.cleanup)

    def test_listing_covers_events_matchers_commands(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        lines = read_project_hooks(str(self.proj.path)).listing_lines()
        blob = "\n".join(lines)
        self.assertIn("PreToolUse", blob)
        self.assertIn("matcher: Bash", blob)
        self.assertIn("echo pre", blob)
        self.assertIn("UserPromptSubmit", blob)
        self.assertIn("echo submit", blob)

    def test_entry_without_matcher_omits_matcher_text(self) -> None:
        self.proj.write(
            "settings.json",
            {"hooks": {"Stop": [{"hooks": [{"command": "echo stop"}]}]}},
        )
        lines = read_project_hooks(str(self.proj.path)).listing_lines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("matcher", lines[0])
        self.assertIn("Stop", lines[0])
        self.assertIn("echo stop", lines[0])

    def test_listing_tolerates_missing_command(self) -> None:
        # An entry with a matcher but no commands still yields a line.
        self.proj.write(
            "settings.json",
            {"hooks": {"PreToolUse": [{"matcher": "Write"}]}},
        )
        lines = read_project_hooks(str(self.proj.path)).listing_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("PreToolUse", lines[0])
        self.assertIn("matcher: Write", lines[0])

    def test_listing_is_deterministic(self) -> None:
        self.proj.write("settings.json", _POPULATED)
        a = read_project_hooks(str(self.proj.path)).listing_lines()
        b = read_project_hooks(str(self.proj.path)).listing_lines()
        self.assertEqual(a, b)


class TargetDirectoryTests(unittest.TestCase):
    def test_directory_selection_expanded(self) -> None:
        self.assertEqual(
            target_directory({"directory": "~/somewhere"}),
            str(Path("~/somewhere").expanduser()),
        )

    def test_falls_back_to_cwd(self) -> None:
        import os

        self.assertEqual(target_directory({}), os.getcwd())
        self.assertEqual(target_directory({"directory": None}), os.getcwd())


if __name__ == "__main__":
    unittest.main()
