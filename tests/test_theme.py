"""Tests for theme parsing: parse_theme, ThemeColors, and the forms section."""

from __future__ import annotations

import unittest

from claudewheel.constants import bg_rgb, fg_rgb
from claudewheel.defaults import DEFAULT_THEME_DARK, DEFAULT_THEME_LIGHT
from claudewheel.theme import ThemeColors, parse_hex, parse_theme

FORMS_KEYS = (
    "title_fg",
    "focus_bg",
    "focus_fg",
    "field_fg",
    "error_fg",
    "hint_fg",
    "cursor_fg",
    "readonly_fg",
)


class ParseHexTests(unittest.TestCase):
    def test_valid_hex(self) -> None:
        self.assertEqual(parse_hex("#ff6464"), (255, 100, 100))

    def test_invalid_hex(self) -> None:
        self.assertIsNone(parse_hex("nope"))
        self.assertIsNone(parse_hex(None))
        self.assertIsNone(parse_hex("#fff"))


class FormsParsingTests(unittest.TestCase):
    """parse_theme() handling of the "forms" theme section."""

    def test_full_forms_section_produces_ansi_sequences(self) -> None:
        """A complete forms section yields the exact ANSI sequences for each key."""
        theme = {
            "forms": {
                "title_fg": "#7ec8e3",
                "focus_bg": "#2a2a4e",
                "focus_fg": "#ffffff",
                "field_fg": "#e0e0e0",
                "error_fg": "#ff6464",
                "hint_fg": "#555555",
                "cursor_fg": "#ffffff",
            },
        }
        colors = parse_theme(theme)
        self.assertEqual(colors.forms_title_fg, fg_rgb(0x7E, 0xC8, 0xE3))
        self.assertEqual(colors.forms_focus_bg, bg_rgb(0x2A, 0x2A, 0x4E))
        self.assertEqual(colors.forms_focus_fg, fg_rgb(0xFF, 0xFF, 0xFF))
        self.assertEqual(colors.forms_field_fg, fg_rgb(0xE0, 0xE0, 0xE0))
        self.assertEqual(colors.forms_error_fg, fg_rgb(0xFF, 0x64, 0x64))
        self.assertEqual(colors.forms_hint_fg, fg_rgb(0x55, 0x55, 0x55))
        self.assertEqual(colors.forms_cursor_fg, fg_rgb(0xFF, 0xFF, 0xFF))

    def test_focus_bg_is_background_sequence(self) -> None:
        """focus_bg goes through _hex_to_bg, not _hex_to_fg."""
        colors = parse_theme({"forms": {"focus_bg": "#123456"}})
        self.assertIn("48;2;", colors.forms_focus_bg)
        self.assertNotIn("38;2;", colors.forms_focus_bg)

    def test_empty_theme_yields_empty_forms_fields(self) -> None:
        """parse_theme({}) yields empty strings for all forms fields."""
        colors = parse_theme({})
        for attr in (
            "forms_title_fg",
            "forms_focus_bg",
            "forms_focus_fg",
            "forms_field_fg",
            "forms_error_fg",
            "forms_hint_fg",
            "forms_cursor_fg",
            "forms_readonly_fg",
        ):
            self.assertEqual(getattr(colors, attr), "", f"{attr} should be empty")

    def test_theme_colors_constructs_without_forms_kwargs(self) -> None:
        """ThemeColors keyword defaults keep pre-forms constructions working."""
        colors = ThemeColors(
            global_fg="",
            label_fg="",
            separator_fg="",
            separator_char=" | ",
            empty_value_fg="",
            empty_value_text="---",
        )
        self.assertEqual(colors.forms_title_fg, "")
        self.assertEqual(colors.forms_focus_bg, "")
        self.assertEqual(colors.forms_focus_fg, "")
        self.assertEqual(colors.forms_field_fg, "")
        self.assertEqual(colors.forms_error_fg, "")
        self.assertEqual(colors.forms_hint_fg, "")
        self.assertEqual(colors.forms_cursor_fg, "")
        self.assertEqual(colors.forms_readonly_fg, "")


class DefaultThemeFormsTests(unittest.TestCase):
    """Both bundled themes carry a complete forms section that parses cleanly."""

    def test_dark_theme_has_all_forms_keys(self) -> None:
        for key in FORMS_KEYS:
            self.assertIn(key, DEFAULT_THEME_DARK["forms"], f"dark missing {key}")

    def test_light_theme_has_all_forms_keys(self) -> None:
        for key in FORMS_KEYS:
            self.assertIn(key, DEFAULT_THEME_LIGHT["forms"], f"light missing {key}")

    def test_default_themes_parse_to_nonempty_forms_sequences(self) -> None:
        for theme in (DEFAULT_THEME_DARK, DEFAULT_THEME_LIGHT):
            colors = parse_theme(theme)
            for attr in (
                "forms_title_fg",
                "forms_focus_bg",
                "forms_focus_fg",
                "forms_field_fg",
                "forms_error_fg",
                "forms_hint_fg",
                "forms_cursor_fg",
                "forms_readonly_fg",
            ):
                self.assertNotEqual(
                    getattr(colors, attr),
                    "",
                    f"{theme['name']}: {attr} should be non-empty",
                )


if __name__ == "__main__":
    unittest.main()
