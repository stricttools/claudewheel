"""Tests for claudewheel.permission and the permission CLI handlers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import contextlib
from typing import Any
from unittest import mock

from claudewheel import permission, cli
from claudewheel.profile_store import Profile


# ---------------------------------------------------------------------------
# Unit tests: validate_rule
# ---------------------------------------------------------------------------


class ValidateRuleTests(unittest.TestCase):
    def test_valid_bash_with_pattern(self) -> None:
        permission.validate_rule("Bash(git push:*)")

    def test_valid_bare_tool(self) -> None:
        permission.validate_rule("WebSearch")

    def test_valid_mcp_tool(self) -> None:
        permission.validate_rule("mcp__tree-sitter__find_text")

    def test_valid_read_with_glob(self) -> None:
        permission.validate_rule("Read(//home/m/**)")

    def test_valid_bash_with_spaces(self) -> None:
        permission.validate_rule("Bash(git add .)")

    def test_reject_empty_string(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("")

    def test_reject_whitespace_only(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("   ")

    def test_reject_unmatched_open_paren(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("Bash(ls")

    def test_reject_unmatched_close_paren(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("Bash ls)")

    def test_reject_empty_tool_name(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("(ls)")

    def test_reject_empty_parens(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("Bash()")

    def test_reject_trailing_text_after_close(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("Bash(ls) extra")

    def test_reject_leading_digit(self) -> None:
        with self.assertRaises(ValueError):
            permission.validate_rule("123Bash")


# ---------------------------------------------------------------------------
# Unit tests: add_rule
# ---------------------------------------------------------------------------


class AddRuleTests(unittest.TestCase):
    def test_add_to_empty_list(self) -> None:
        data: dict[str, Any] = {"permissions": {"allow": []}}
        result = permission.add_rule(data, "allow", "Bash")
        self.assertEqual(result, "added")
        self.assertIn("Bash", data["permissions"]["allow"])

    def test_add_to_existing_list(self) -> None:
        data: dict[str, Any] = {"permissions": {"allow": ["Read"]}}
        result = permission.add_rule(data, "allow", "Bash")
        self.assertEqual(result, "added")
        self.assertEqual(data["permissions"]["allow"], ["Read", "Bash"])

    def test_add_duplicate(self) -> None:
        data: dict[str, Any] = {"permissions": {"allow": ["Bash"]}}
        result = permission.add_rule(data, "allow", "Bash")
        self.assertEqual(result, "already present")
        self.assertEqual(data["permissions"]["allow"], ["Bash"])

    def test_add_creates_missing_category(self) -> None:
        data: dict[str, Any] = {"permissions": {}}
        result = permission.add_rule(data, "allow", "Bash")
        self.assertEqual(result, "added")
        self.assertEqual(data["permissions"]["allow"], ["Bash"])

    def test_add_creates_missing_permissions_key(self) -> None:
        data: dict[str, Any] = {}
        result = permission.add_rule(data, "deny", "Bash")
        self.assertEqual(result, "added")
        self.assertEqual(data["permissions"]["deny"], ["Bash"])

    def test_add_invalid_category(self) -> None:
        data: dict[str, Any] = {"permissions": {}}
        with self.assertRaises(ValueError):
            permission.add_rule(data, "foo", "Bash")


# ---------------------------------------------------------------------------
# Unit tests: remove_rule
# ---------------------------------------------------------------------------


class RemoveRuleTests(unittest.TestCase):
    def test_remove_existing(self) -> None:
        data: dict[str, Any] = {"permissions": {"allow": ["Bash", "Read"]}}
        result = permission.remove_rule(data, "allow", "Bash")
        self.assertEqual(result, "removed")
        self.assertEqual(data["permissions"]["allow"], ["Read"])

    def test_remove_nonexistent(self) -> None:
        data: dict[str, Any] = {"permissions": {"allow": ["Read"]}}
        result = permission.remove_rule(data, "allow", "Bash")
        self.assertEqual(result, "not found")
        self.assertEqual(data["permissions"]["allow"], ["Read"])

    def test_remove_invalid_category(self) -> None:
        data: dict[str, Any] = {"permissions": {}}
        with self.assertRaises(ValueError):
            permission.remove_rule(data, "foo", "Bash")


# ---------------------------------------------------------------------------
# Unit tests: load_settings
# ---------------------------------------------------------------------------


class LoadSettingsTests(unittest.TestCase):
    def test_load_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            p.write_text('{"permissions": {"allow": ["Bash"]}}\n')
            data = permission.load_settings(p)
            self.assertIsInstance(data, dict)
            self.assertEqual(data["permissions"]["allow"], ["Bash"])

    def test_load_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "missing.json"
            with self.assertRaises(FileNotFoundError):
                permission.load_settings(p)

    def test_load_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            p.write_text("{not valid json")
            with self.assertRaises(json.JSONDecodeError):
                permission.load_settings(p)


# ---------------------------------------------------------------------------
# Unit tests: save_settings
# ---------------------------------------------------------------------------


class SaveSettingsTests(unittest.TestCase):
    def test_save_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            permission.save_settings(p, {"key": "value"})
            self.assertTrue(p.exists())
            loaded = json.loads(p.read_text())
            self.assertEqual(loaded, {"key": "value"})

    def test_save_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            permission.save_settings(p, {"a": 1})
            tmp_file = p.with_suffix(".tmp")
            self.assertFalse(
                tmp_file.exists(), ".tmp file should not remain after save"
            )

    def test_save_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            permission.save_settings(p, {"a": 1})
            content = p.read_text()
            # 2-space indent
            self.assertIn("  ", content)
            # Trailing newline
            self.assertTrue(content.endswith("\n"))
            # Verify the indent is exactly 2 (not 4)
            expected = json.dumps({"a": 1}, indent=2) + "\n"
            self.assertEqual(content, expected)

    def test_round_trip_preserves_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            original = {"permissions": {"allow": ["Bash", "Read"], "deny": []}}
            permission.save_settings(p, original)
            content_after_save = p.read_text()
            loaded = permission.load_settings(p)
            permission.save_settings(p, loaded)
            content_after_round_trip = p.read_text()
            self.assertEqual(content_after_save, content_after_round_trip)


# ---------------------------------------------------------------------------
# Unit tests: resolve_profiles
# ---------------------------------------------------------------------------


class ResolveProfilesTests(unittest.TestCase):
    def _make_profiles(self) -> list[Profile]:
        return [
            Profile(
                name="work",
                path=Path("/fake/work"),
                has_credentials=True,
                has_token=False,
            ),
            Profile(
                name="personal",
                path=Path("/fake/personal"),
                has_credentials=True,
                has_token=False,
            ),
        ]

    def _ws_with(self, profiles: list[Profile]) -> mock.MagicMock:
        ws = mock.MagicMock()
        ws.profiles.enumerate.return_value = profiles
        return ws

    def test_resolve_single_profile(self) -> None:
        ws = self._ws_with(self._make_profiles())
        result = permission.resolve_profiles(ws, "work", False)
        self.assertEqual(len(result), 1)
        name, settings_path = result[0]
        self.assertEqual(name, "work")
        self.assertEqual(settings_path, Path("/fake/work/settings.json"))

    def test_resolve_all_profiles(self) -> None:
        ws = self._ws_with(self._make_profiles())
        result = permission.resolve_profiles(ws, None, True)
        self.assertEqual(len(result), 2)
        names = [n for n, _ in result]
        self.assertIn("work", names)
        self.assertIn("personal", names)

    def test_resolve_unknown_profile_exits(self) -> None:
        ws = self._ws_with(self._make_profiles())
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            permission.resolve_profiles(ws, "nonexistent", False)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("nonexistent", err.getvalue())


# ---------------------------------------------------------------------------
# CLI integration tests: permission add / remove / list
# ---------------------------------------------------------------------------


class _PermissionCLIBase(unittest.TestCase):
    """Shared setup for CLI handler tests: temp dir with settings.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile_dir = Path(self._tmp.name) / "profiles" / "testprofile"
        self.profile_dir.mkdir(parents=True)
        self.settings_path = self.profile_dir / "settings.json"
        self.ws = mock.MagicMock()

    def _write_settings(self, data: dict[str, Any]) -> None:
        self.settings_path.write_text(json.dumps(data, indent=2) + "\n")

    def _read_settings(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.settings_path.read_text())
        return data

    def _fake_profiles(self) -> list[Profile]:
        return [
            Profile(
                name="testprofile",
                path=self.profile_dir,
                has_credentials=True,
                has_token=False,
            ),
        ]


class PermissionCLIAddTests(_PermissionCLIBase):
    def test_add_permission(self) -> None:
        self._write_settings({"permissions": {"allow": []}})
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_add(
                    self.ws, "allow", "Bash", "testprofile", False
                )
        self.assertEqual(rc, 0)
        self.assertIn("added", buf.getvalue())
        data = self._read_settings()
        self.assertIn("Bash", data["permissions"]["allow"])

    def test_add_permission_all_profiles(self) -> None:
        # Create a second profile
        second_dir = Path(self._tmp.name) / "profiles" / "second"
        second_dir.mkdir(parents=True)
        second_settings = second_dir / "settings.json"
        self._write_settings({"permissions": {"allow": []}})
        second_settings.write_text(
            json.dumps({"permissions": {"allow": []}}, indent=2) + "\n"
        )

        profiles = self._fake_profiles() + [
            Profile(
                name="second", path=second_dir, has_credentials=True, has_token=False
            ),
        ]
        self.ws.profiles.enumerate.return_value = profiles
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_add(self.ws, "allow", "Read", "", True)

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("testprofile", out)
        self.assertIn("second", out)
        # Both settings files updated
        d1 = self._read_settings()
        d2 = json.loads(second_settings.read_text())
        self.assertIn("Read", d1["permissions"]["allow"])
        self.assertIn("Read", d2["permissions"]["allow"])

    def test_add_duplicate_permission(self) -> None:
        self._write_settings({"permissions": {"allow": ["Bash"]}})
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_add(
                    self.ws, "allow", "Bash", "testprofile", False
                )
        self.assertEqual(rc, 0)
        self.assertIn("already", buf.getvalue())


class PermissionCLIRemoveTests(_PermissionCLIBase):
    def test_remove_permission(self) -> None:
        self._write_settings({"permissions": {"allow": ["Bash", "Read"]}})
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_remove(
                    self.ws, "allow", "Bash", "testprofile", False
                )
        self.assertEqual(rc, 0)
        self.assertIn("removed", buf.getvalue())
        data = self._read_settings()
        self.assertNotIn("Bash", data["permissions"]["allow"])
        self.assertIn("Read", data["permissions"]["allow"])

    def test_remove_nonexistent(self) -> None:
        self._write_settings({"permissions": {"allow": ["Read"]}})
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_remove(
                    self.ws, "allow", "Bash", "testprofile", False
                )
        self.assertEqual(rc, 0)
        self.assertIn("not found", buf.getvalue())


class PermissionCLIListTests(_PermissionCLIBase):
    def test_list_grouped_format(self) -> None:
        self._write_settings(
            {
                "permissions": {
                    "allow": ["Bash", "Read"],
                    "deny": ["WebSearch"],
                    "ask": [],
                },
            }
        )
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_list(
                    self.ws, "testprofile", False, format="grouped", category=""
                )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("allow:", out)
        self.assertIn("deny:", out)
        self.assertIn("Bash", out)
        self.assertIn("Read", out)
        self.assertIn("WebSearch", out)

    def test_list_flat_format(self) -> None:
        self._write_settings(
            {
                "permissions": {"allow": ["Bash"], "deny": ["WebSearch"], "ask": []},
            }
        )
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_list(
                    self.ws, "testprofile", False, format="flat", category=""
                )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # Flat format: category<tab>rule
        self.assertIn("allow\tBash", out)
        self.assertIn("deny\tWebSearch", out)

    def test_list_json_format(self) -> None:
        self._write_settings(
            {
                "permissions": {"allow": ["Bash"], "deny": [], "ask": []},
            }
        )
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_list(
                    self.ws, "testprofile", False, format="json", category=""
                )
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIn("allow", parsed)
        self.assertEqual(parsed["allow"], ["Bash"])

    def test_list_with_category_filter(self) -> None:
        self._write_settings(
            {
                "permissions": {
                    "allow": ["Bash", "Read"],
                    "deny": ["WebSearch"],
                    "ask": [],
                },
            }
        )
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._handle_permission_list(
                    self.ws, "testprofile", False, format="grouped", category="deny"
                )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("deny:", out)
        self.assertIn("WebSearch", out)
        # Should NOT contain other categories
        self.assertNotIn("allow:", out)
        self.assertNotIn("ask:", out)

    def test_malformed_settings_json_errors(self) -> None:
        self.settings_path.write_text("{broken json")
        self.ws.profiles.enumerate.return_value = self._fake_profiles()
        with contextlib.nullcontext():
            with self.assertRaises(json.JSONDecodeError):
                cli._handle_permission_list(
                    self.ws, "testprofile", False, format="grouped", category=""
                )

    def test_unknown_profile_errors(self) -> None:
        profiles = self._fake_profiles()
        self.ws.profiles.enumerate.return_value = profiles
        with contextlib.nullcontext():
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                cli._handle_permission_add(
                    self.ws, "allow", "Bash", "nonexistent", False
                )
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("nonexistent", err.getvalue())


if __name__ == "__main__":
    unittest.main()
