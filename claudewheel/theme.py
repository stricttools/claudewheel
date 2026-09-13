"""Parse hex color themes into pre-computed ANSI escape sequences."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constants import fg_rgb, bg_rgb
from .lifecycle import STATES


def parse_hex(hex_str: str | None) -> tuple[int, int, int] | None:
    """Convert '#RRGGBB' to (R, G, B) tuple. Returns None for None/invalid input."""
    if not hex_str or not isinstance(hex_str, str):
        return None
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _hex_to_fg(hex_str: str | None) -> str:
    """Convert hex color to ANSI foreground sequence, or empty string if None."""
    rgb = parse_hex(hex_str)
    return fg_rgb(*rgb) if rgb else ""


def _hex_to_bg(hex_str: str | None) -> str:
    """Convert hex color to ANSI background sequence, or empty string if None."""
    rgb = parse_hex(hex_str)
    return bg_rgb(*rgb) if rgb else ""


@dataclass
class ThemeColors:
    """Pre-parsed ANSI escape sequences for all theme colors."""

    global_fg: str  # ANSI fg sequence
    label_fg: str  # ANSI fg for labels
    separator_fg: str  # ANSI fg for separators
    separator_char: str  # literal string like " | "
    empty_value_fg: str  # ANSI fg for "---"
    empty_value_text: str  # literal string like "---"
    # Per-segment colors: dict mapping segment key to dict of ANSI sequences
    segment_colors: dict[str, dict[str, str]] = field(default_factory=dict)
    # Search colors
    search_cursor_fg: str = ""
    search_match_fg: str = ""
    search_no_match_fg: str = ""
    # Overflow chrome colors (edge arrows and minimap)
    overflow_arrow_fg: str = ""
    overflow_minimap_fg: str = ""
    overflow_minimap_focused_bg: str = ""
    overflow_minimap_char: str = "▪"
    # Form wizard colors
    forms_title_fg: str = ""
    forms_focus_bg: str = ""
    forms_focus_fg: str = ""
    forms_field_fg: str = ""
    forms_error_fg: str = ""
    forms_hint_fg: str = ""
    forms_cursor_fg: str = ""
    forms_readonly_fg: str = ""
    # Sessions table colors
    sessions_frame_fg: str = ""
    sessions_header_fg: str = ""
    sessions_row_fg: str = ""
    sessions_focus_bg: str = ""
    sessions_focus_fg: str = ""
    sessions_detail_fg: str = ""
    sessions_hint_fg: str = ""
    sessions_message_fg: str = ""
    # One entry per claudewheel.lifecycle state, keyed by the state name as it
    # is written (hyphen included, e.g. "on-hold"). Always complete: a theme
    # declaring none still gets every key, mapped to the empty sequence.
    sessions_state_fg: dict[str, str] = field(default_factory=dict)


def parse_theme(theme_dict: dict[str, Any]) -> ThemeColors:
    """Parse a raw theme dict into a ThemeColors instance with ANSI sequences."""
    g = theme_dict.get("global", {})

    segment_colors: dict[str, dict[str, str]] = {}
    for seg_key, seg_theme in theme_dict.get("segments", {}).items():
        segment_colors[seg_key] = {
            "value_fg": _hex_to_fg(seg_theme.get("value_fg")),
            "focus_bg": _hex_to_bg(seg_theme.get("focus_bg")),
            "focus_fg": _hex_to_fg(seg_theme.get("focus_fg")),
            "option_fg": _hex_to_fg(seg_theme.get("option_fg")),
            "unavailable_fg": _hex_to_fg(seg_theme.get("unavailable_fg")),
        }

    search = theme_dict.get("search", {})
    overflow = theme_dict.get("overflow", {})
    forms = theme_dict.get("forms", {})
    sessions = theme_dict.get("sessions", {})

    # The state colours are derived from the state list, never hand-listed: a
    # state added to claudewheel.lifecycle gets a key here without an edit.
    sessions_state_fg = {
        state: _hex_to_fg(sessions.get(f"state_{state.replace('-', '_')}_fg"))
        for state in STATES
    }

    return ThemeColors(
        global_fg=_hex_to_fg(g.get("fg")),
        label_fg=_hex_to_fg(g.get("label_fg")),
        separator_fg=_hex_to_fg(g.get("separator_fg")),
        separator_char=g.get("separator_char", " | "),
        empty_value_fg=_hex_to_fg(g.get("empty_value_fg")),
        empty_value_text=g.get("empty_value_text", "---"),
        segment_colors=segment_colors,
        search_cursor_fg=_hex_to_fg(search.get("cursor_fg")),
        search_match_fg=_hex_to_fg(search.get("match_fg")),
        search_no_match_fg=_hex_to_fg(search.get("no_match_fg")),
        overflow_arrow_fg=_hex_to_fg(overflow.get("arrow_fg")),
        overflow_minimap_fg=_hex_to_fg(overflow.get("minimap_fg")),
        overflow_minimap_focused_bg=_hex_to_bg(overflow.get("minimap_focused_bg")),
        overflow_minimap_char=overflow.get("minimap_char", "▪"),
        forms_title_fg=_hex_to_fg(forms.get("title_fg")),
        forms_focus_bg=_hex_to_bg(forms.get("focus_bg")),
        forms_focus_fg=_hex_to_fg(forms.get("focus_fg")),
        forms_field_fg=_hex_to_fg(forms.get("field_fg")),
        forms_error_fg=_hex_to_fg(forms.get("error_fg")),
        forms_hint_fg=_hex_to_fg(forms.get("hint_fg")),
        forms_cursor_fg=_hex_to_fg(forms.get("cursor_fg")),
        forms_readonly_fg=_hex_to_fg(forms.get("readonly_fg")),
        sessions_frame_fg=_hex_to_fg(sessions.get("frame_fg")),
        sessions_header_fg=_hex_to_fg(sessions.get("header_fg")),
        sessions_row_fg=_hex_to_fg(sessions.get("row_fg")),
        sessions_focus_bg=_hex_to_bg(sessions.get("focus_bg")),
        sessions_focus_fg=_hex_to_fg(sessions.get("focus_fg")),
        sessions_detail_fg=_hex_to_fg(sessions.get("detail_fg")),
        sessions_hint_fg=_hex_to_fg(sessions.get("hint_fg")),
        sessions_message_fg=_hex_to_fg(sessions.get("message_fg")),
        sessions_state_fg=sessions_state_fg,
    )
