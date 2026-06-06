"""Tests for profile.resolve_profile()."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from claudewheel.profile import resolve_profile


def _fake_config_manager(options_def: dict):
    """Return a mock ConfigManager with the given options_def."""
    mgr = mock.MagicMock()
    mgr.options_def = options_def
    return mgr


class ResolveProfileTests(unittest.TestCase):

    def _options_with_profile(self, name: str, config_dir: str) -> dict:
        return {
            "profile": {
                "values": [name],
                "metadata": {name: {"config_dir": config_dir}},
            }
        }

    @mock.patch("claudewheel.profile.ConfigManager")
    def test_valid_profile_with_token(self, mock_cm_cls: mock.MagicMock) -> None:
        """Returns both CLAUDE_CONFIG_DIR and CLAUDE_CODE_OAUTH_TOKEN."""
        mock_cm_cls.return_value = _fake_config_manager(
            self._options_with_profile("work", "~/.claudewheel/profiles/work")
        )
        tokens = {"work": "tok_abc123"}
        with mock.patch("claudewheel.profile.TOKENS_FILE") as mock_tf:
            mock_tf.is_file.return_value = True
            mock_tf.read_text.return_value = json.dumps(tokens)
            result = resolve_profile("work")

        self.assertEqual(
            result["CLAUDE_CONFIG_DIR"],
            str(Path("~/.claudewheel/profiles/work").expanduser()),
        )
        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_abc123")

    @mock.patch("claudewheel.profile.ConfigManager")
    def test_valid_profile_with_dict_token(self, mock_cm_cls: mock.MagicMock) -> None:
        """Handles {name: {token, created}} token format."""
        mock_cm_cls.return_value = _fake_config_manager(
            self._options_with_profile("work", "~/.claudewheel/profiles/work")
        )
        tokens = {"work": {"token": "tok_dict", "created": "2025-01-01"}}
        with mock.patch("claudewheel.profile.TOKENS_FILE") as mock_tf:
            mock_tf.is_file.return_value = True
            mock_tf.read_text.return_value = json.dumps(tokens)
            result = resolve_profile("work")

        self.assertEqual(result["CLAUDE_CODE_OAUTH_TOKEN"], "tok_dict")

    @mock.patch("claudewheel.profile.ConfigManager")
    def test_valid_profile_without_token(self, mock_cm_cls: mock.MagicMock) -> None:
        """Returns only CLAUDE_CONFIG_DIR when no token exists."""
        mock_cm_cls.return_value = _fake_config_manager(
            self._options_with_profile("personal", "~/.claudewheel/profiles/personal")
        )
        with mock.patch("claudewheel.profile.TOKENS_FILE") as mock_tf:
            mock_tf.is_file.return_value = False
            result = resolve_profile("personal")

        self.assertEqual(
            result["CLAUDE_CONFIG_DIR"],
            str(Path("~/.claudewheel/profiles/personal").expanduser()),
        )
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", result)

    @mock.patch("claudewheel.profile.ConfigManager")
    def test_missing_profile_raises(self, mock_cm_cls: mock.MagicMock) -> None:
        """Raises ValueError with a helpful message for unknown profiles."""
        mock_cm_cls.return_value = _fake_config_manager(
            self._options_with_profile("work", "~/.claudewheel/profiles/work")
        )
        with self.assertRaises(ValueError) as ctx:
            resolve_profile("nonexistent")

        self.assertIn("nonexistent", str(ctx.exception))
        self.assertIn("work", str(ctx.exception))

    @mock.patch("claudewheel.profile.ConfigManager")
    def test_profile_missing_config_dir_raises(self, mock_cm_cls: mock.MagicMock) -> None:
        """Raises ValueError when profile metadata has no config_dir."""
        mock_cm_cls.return_value = _fake_config_manager({
            "profile": {
                "values": ["broken"],
                "metadata": {"broken": {"some_other_key": "val"}},
            }
        })
        with self.assertRaises(ValueError) as ctx:
            resolve_profile("broken")

        self.assertIn("config_dir", str(ctx.exception))

    @mock.patch("claudewheel.profile.ConfigManager")
    def test_corrupt_tokens_file_ignored(self, mock_cm_cls: mock.MagicMock) -> None:
        """Corrupt tokens.json is silently ignored -- returns only CLAUDE_CONFIG_DIR."""
        mock_cm_cls.return_value = _fake_config_manager(
            self._options_with_profile("work", "~/.claudewheel/profiles/work")
        )
        with mock.patch("claudewheel.profile.TOKENS_FILE") as mock_tf:
            mock_tf.is_file.return_value = True
            mock_tf.read_text.return_value = "not valid json{"
            result = resolve_profile("work")

        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", result)
        self.assertIn("CLAUDE_CONFIG_DIR", result)


if __name__ == "__main__":
    unittest.main()
