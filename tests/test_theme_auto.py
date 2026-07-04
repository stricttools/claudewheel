"""Tests for theme auto-detection integration in ConfigManager."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claudewheel.config import ConfigManager
from claudewheel.defaults import (
    DEFAULT_CONFIG,
    DEFAULT_OPTIONS,
    DEFAULT_SEGMENTS,
    DEFAULT_STATE,
    DEFAULT_THEME_DARK,
    DEFAULT_THEME_LIGHT,
)


def _write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _setup_temp_config_dir(
    tmp: Path,
    config: dict | None = None,
) -> dict[str, Path]:
    """Create config files in *tmp* and return path constants dict."""
    launcher_dir = tmp / "claudewheel"
    themes_dir = launcher_dir / "themes"
    hooks_dir = launcher_dir / "hooks"
    scripts_dir = launcher_dir / "scripts"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    themes_dir.mkdir(exist_ok=True)
    hooks_dir.mkdir(exist_ok=True)
    scripts_dir.mkdir(exist_ok=True)

    config_file = launcher_dir / "config.json"
    segments_file = launcher_dir / "segments.json"
    options_file = launcher_dir / "options.json"
    state_file = launcher_dir / "state.json"
    shared_settings_file = launcher_dir / "shared-settings.json"

    _write_json(config_file, config if config is not None else DEFAULT_CONFIG)
    _write_json(segments_file, DEFAULT_SEGMENTS)
    _write_json(options_file, DEFAULT_OPTIONS)
    _write_json(state_file, DEFAULT_STATE)
    _write_json(themes_dir / "dark.json", DEFAULT_THEME_DARK)
    _write_json(themes_dir / "light.json", DEFAULT_THEME_LIGHT)

    return {
        "CONFIG_DIR": launcher_dir,
        "CONFIG_FILE": config_file,
        "SEGMENTS_FILE": segments_file,
        "OPTIONS_FILE": options_file,
        "STATE_FILE": state_file,
        "THEMES_DIR": themes_dir,
        "HOOKS_DIR": hooks_dir,
        "SCRIPTS_DIR": scripts_dir,
        "SHARED_SETTINGS_FILE": shared_settings_file,
    }


def _patch_constants(paths: dict[str, Path]):
    import claudewheel.config as cfg_mod
    return [
        patch.object(cfg_mod, name, value)
        for name, value in paths.items()
    ]


class ThemeAutoDetectionTests(unittest.TestCase):
    """ConfigManager resolves 'auto' theme via terminal detection."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _make_cm(self, paths: dict[str, Path], detect_result: str | None = None) -> ConfigManager:
        patches = _patch_constants(paths)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        detect_patch = patch(
            "claudewheel.config.detect_terminal_background",
            return_value=detect_result,
        )
        detect_patch.start()
        self.addCleanup(detect_patch.stop)
        return ConfigManager()

    def test_auto_theme_detects_dark(self) -> None:
        """When config has theme=auto and detection returns dark, use dark theme."""
        paths = _setup_temp_config_dir(self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"})
        cm = self._make_cm(paths, detect_result="dark")
        # The loaded theme should be the dark one
        self.assertEqual(cm.theme["name"], "dark")

    def test_auto_theme_detects_light(self) -> None:
        """When config has theme=auto and detection returns light, use light theme."""
        paths = _setup_temp_config_dir(self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"})
        cm = self._make_cm(paths, detect_result="light")
        self.assertEqual(cm.theme["name"], "light")

    def test_auto_theme_detection_returns_none_falls_back_to_dark(self) -> None:
        """When detection returns None (unsupported terminal), fall back to dark."""
        paths = _setup_temp_config_dir(self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"})
        cm = self._make_cm(paths, detect_result=None)
        self.assertEqual(cm.theme["name"], "dark")

    def test_explicit_dark_respected(self) -> None:
        """When config explicitly says dark, detection is not called."""
        paths = _setup_temp_config_dir(self.tmp, config={**DEFAULT_CONFIG, "theme": "dark"})
        with patch("claudewheel.config.detect_terminal_background") as mock_detect:
            patches = _patch_constants(paths)
            for p in patches:
                p.start()
                self.addCleanup(p.stop)
            cm = ConfigManager()
            mock_detect.assert_not_called()
        self.assertEqual(cm.theme["name"], "dark")

    def test_explicit_light_respected(self) -> None:
        """When config explicitly says light, detection is not called."""
        paths = _setup_temp_config_dir(self.tmp, config={**DEFAULT_CONFIG, "theme": "light"})
        with patch("claudewheel.config.detect_terminal_background") as mock_detect:
            patches = _patch_constants(paths)
            for p in patches:
                p.start()
                self.addCleanup(p.stop)
            cm = ConfigManager()
            mock_detect.assert_not_called()
        self.assertEqual(cm.theme["name"], "light")

    def test_default_config_has_auto_theme(self) -> None:
        """DEFAULT_CONFIG sets theme to auto for new installations."""
        self.assertEqual(DEFAULT_CONFIG["theme"], "auto")

    def test_new_install_uses_auto_detection(self) -> None:
        """A fresh install (DEFAULT_CONFIG) triggers auto-detection."""
        paths = _setup_temp_config_dir(self.tmp)  # uses DEFAULT_CONFIG
        cm = self._make_cm(paths, detect_result="light")
        self.assertEqual(cm.theme["name"], "light")


class ResolveThemeNameTests(unittest.TestCase):
    """ConfigManager._resolve_theme_name unit tests."""

    def test_dark_passthrough(self) -> None:
        with patch("claudewheel.config.detect_terminal_background") as m:
            result = ConfigManager._resolve_theme_name("dark")
        m.assert_not_called()
        self.assertEqual(result, "dark")

    def test_light_passthrough(self) -> None:
        with patch("claudewheel.config.detect_terminal_background") as m:
            result = ConfigManager._resolve_theme_name("light")
        m.assert_not_called()
        self.assertEqual(result, "light")

    def test_auto_calls_detection(self) -> None:
        with patch("claudewheel.config.detect_terminal_background", return_value="dark") as m:
            result = ConfigManager._resolve_theme_name("auto")
        m.assert_called_once()
        self.assertEqual(result, "dark")

    def test_auto_with_none_falls_back_to_dark(self) -> None:
        with patch("claudewheel.config.detect_terminal_background", return_value=None):
            result = ConfigManager._resolve_theme_name("auto")
        self.assertEqual(result, "dark")

    def test_custom_theme_name_passthrough(self) -> None:
        """A custom theme name (e.g. 'solarized') passes through unchanged."""
        with patch("claudewheel.config.detect_terminal_background") as m:
            result = ConfigManager._resolve_theme_name("solarized")
        m.assert_not_called()
        self.assertEqual(result, "solarized")


if __name__ == "__main__":
    unittest.main()
