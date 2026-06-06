"""Parse hex color themes into pre-computed ANSI escape sequences."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import fg_rgb, bg_rgb


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

    global_fg: str          # ANSI fg sequence
    label_fg: str           # ANSI fg for labels
    separator_fg: str       # ANSI fg for separators
    separator_char: str     # literal string like " | "
    empty_value_fg: str     # ANSI fg for "---"
    empty_value_text: str   # literal string like "---"
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


def parse_theme(theme_dict: dict) -> ThemeColors:
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
    )
