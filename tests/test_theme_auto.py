"""Tests for theme auto-detection at the UI boundary (not during store construction).

Phase 5.2 moved terminal-querying theme resolution OUT of ``AppConfigStore``
construction. The store performs zero terminal I/O; ``resolve_theme_name`` (a
module-level function) is called at the UI boundary, and ``store.load_theme``
reads the resolved theme file. These tests split accordingly:

- a construction test proving no terminal query happens when building the store;
- boundary-resolution tests exercising ``resolve_theme_name`` + ``load_theme``
  with terminal detection mocked.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch, Mock

from claudewheel.config import AppConfigStore, resolve_theme_name
from claudewheel.workspace import Workspace
from claudewheel.defaults import DEFAULT_CONFIG
from tests.wheelhelpers import setup_temp_config_dir as _setup_temp_config_dir


def _store(paths: dict[str, Path]) -> AppConfigStore:
    return Workspace.open(paths["CONFIG_DIR"]).appconfig()


class StoreConstructionNoTTYTests(unittest.TestCase):
    """AppConfigStore construction performs no terminal I/O, even with theme=auto."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_construction_never_queries_terminal(self) -> None:
        """Building the store must not call detect_terminal_background."""
        paths = _setup_temp_config_dir(
            self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"}
        )
        spy = Mock(
            side_effect=AssertionError("terminal I/O attempted during construction")
        )
        with patch("claudewheel.config.detect_terminal_background", spy):
            _store(paths)
        self.assertFalse(spy.called, "construction must not query the terminal")


class BoundaryThemeResolutionTests(unittest.TestCase):
    """resolve_theme_name + store.load_theme reproduce the old auto-detect behavior."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _resolve_and_load(
        self, paths: dict[str, Path], detect_result: str | None
    ) -> dict[str, Any]:
        """Build the store (no TTY), then resolve+load the theme at the boundary."""
        store = _store(paths)
        with patch(
            "claudewheel.config.detect_terminal_background",
            autospec=True,
            return_value=detect_result,
        ):
            name = resolve_theme_name(store.config.get("theme", "auto"))
        return store.load_theme(name)

    def test_auto_theme_detects_dark(self) -> None:
        """theme=auto + detection 'dark' -> dark theme at the boundary."""
        paths = _setup_temp_config_dir(
            self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"}
        )
        theme = self._resolve_and_load(paths, detect_result="dark")
        self.assertEqual(theme["name"], "dark")

    def test_auto_theme_detects_light(self) -> None:
        """theme=auto + detection 'light' -> light theme at the boundary."""
        paths = _setup_temp_config_dir(
            self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"}
        )
        theme = self._resolve_and_load(paths, detect_result="light")
        self.assertEqual(theme["name"], "light")

    def test_auto_theme_detection_returns_none_falls_back_to_dark(self) -> None:
        """theme=auto + detection None (unsupported terminal) -> dark."""
        paths = _setup_temp_config_dir(
            self.tmp, config={**DEFAULT_CONFIG, "theme": "auto"}
        )
        theme = self._resolve_and_load(paths, detect_result=None)
        self.assertEqual(theme["name"], "dark")

    def test_explicit_dark_respected(self) -> None:
        """theme=dark: detection is not called; dark theme loaded."""
        paths = _setup_temp_config_dir(
            self.tmp, config={**DEFAULT_CONFIG, "theme": "dark"}
        )
        store = _store(paths)
        with patch(
            "claudewheel.config.detect_terminal_background", autospec=True
        ) as mock_detect:
            name = resolve_theme_name(store.config.get("theme", "auto"))
        mock_detect.assert_not_called()
        self.assertEqual(store.load_theme(name)["name"], "dark")

    def test_explicit_light_respected(self) -> None:
        """theme=light: detection is not called; light theme loaded."""
        paths = _setup_temp_config_dir(
            self.tmp, config={**DEFAULT_CONFIG, "theme": "light"}
        )
        store = _store(paths)
        with patch(
            "claudewheel.config.detect_terminal_background", autospec=True
        ) as mock_detect:
            name = resolve_theme_name(store.config.get("theme", "auto"))
        mock_detect.assert_not_called()
        self.assertEqual(store.load_theme(name)["name"], "light")

    def test_default_config_has_auto_theme(self) -> None:
        """DEFAULT_CONFIG sets theme to auto for new installations."""
        self.assertEqual(DEFAULT_CONFIG["theme"], "auto")

    def test_new_install_uses_auto_detection(self) -> None:
        """A fresh install (DEFAULT_CONFIG) triggers auto-detection at the boundary."""
        paths = _setup_temp_config_dir(self.tmp)  # uses DEFAULT_CONFIG (theme=auto)
        theme = self._resolve_and_load(paths, detect_result="light")
        self.assertEqual(theme["name"], "light")


class ResolveThemeNameTests(unittest.TestCase):
    """resolve_theme_name() module-function unit tests."""

    def test_dark_passthrough(self) -> None:
        with patch("claudewheel.config.detect_terminal_background", autospec=True) as m:
            result = resolve_theme_name("dark")
        m.assert_not_called()
        self.assertEqual(result, "dark")

    def test_light_passthrough(self) -> None:
        with patch("claudewheel.config.detect_terminal_background", autospec=True) as m:
            result = resolve_theme_name("light")
        m.assert_not_called()
        self.assertEqual(result, "light")

    def test_auto_calls_detection(self) -> None:
        with patch(
            "claudewheel.config.detect_terminal_background",
            autospec=True,
            return_value="dark",
        ) as m:
            result = resolve_theme_name("auto")
        m.assert_called_once()
        self.assertEqual(result, "dark")

    def test_auto_with_none_falls_back_to_dark(self) -> None:
        with patch(
            "claudewheel.config.detect_terminal_background",
            autospec=True,
            return_value=None,
        ):
            result = resolve_theme_name("auto")
        self.assertEqual(result, "dark")

    def test_custom_theme_name_passthrough(self) -> None:
        """A custom theme name (e.g. 'solarized') passes through unchanged."""
        with patch("claudewheel.config.detect_terminal_background", autospec=True) as m:
            result = resolve_theme_name("solarized")
        m.assert_not_called()
        self.assertEqual(result, "solarized")


if __name__ == "__main__":
    unittest.main()
