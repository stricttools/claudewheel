---
title: Theming
description: "How claudewheel themes work: the hex color system, theme file structure, built-in dark and light themes, auto-detection, and creating custom themes."
nav_group: "Concepts"
order: 6
---

# Theming

claudewheel uses a JSON-based theming system that converts hex colors into
24-bit ANSI escape sequences at startup. Theme files live in
`~/.claudewheel/themes/` and are loaded by the config store before the TUI
renders.

## Theme selection

The `"theme"` key in `~/.claudewheel/config.json` controls which theme is
active. It accepts three kinds of values:

- `"auto"` (default) -- queries the terminal's background color via OSC 11
  and selects `"dark"` or `"light"` accordingly. If the terminal does not
  support OSC 11 or the query times out, falls back to `"dark"`.
- `"dark"` or `"light"` -- loads the corresponding built-in theme directly,
  skipping terminal detection.
- Any other string (e.g. `"solarized"`) -- loads
  `~/.claudewheel/themes/<name>.json`. Missing keys are backfilled from the
  dark theme defaults.

Theme resolution happens at the UI boundary, not during config store
construction. The store performs zero terminal I/O -- the `resolve_theme_name`
function is called just before rendering begins.

### Live theme switching

Terminals that support Mode 2031 (theme-change notifications) can trigger a
live theme switch while the TUI is running. When the terminal reports a
transition to dark or light mode, claudewheel reloads the corresponding theme
file and re-renders immediately without restarting.

## Hex color system

All colors in theme files are specified as `#RRGGBB` hex strings. The theme
parser converts each hex value to a 24-bit ANSI escape sequence
(`\033[38;2;R;G;Bm` for foreground, `\033[48;2;R;G;Bm` for background) at
load time. The pre-computed sequences are stored in a `ThemeColors` dataclass
so the renderer never parses colors during drawing.

A `null` or missing color value produces an empty string, which causes the
terminal to use its own default for that element.

## Theme file structure

A theme file is a JSON object with a `name` string plus five top-level
sections: `global`, `segments`, `search`, `overflow`, and `forms`. Sections
and individual keys may be omitted; a missing color key parses to an empty
escape sequence, so the terminal's own default applies to that element. The
skeleton below shows one representative key per section, taken from the
built-in dark theme; each section's full key set is documented under its own
heading.

```json
{
  "name": "dark",
  "global": {
    "fg": "#e0e0e0"
  },
  "segments": {
    "profile": {
      "value_fg": "#7ec8e3"
    }
  },
  "search": {
    "match_fg": "#ffff00"
  },
  "overflow": {
    "arrow_fg": "#666666"
  },
  "forms": {
    "title_fg": "#7ec8e3"
  }
}
```

### global

Controls the base appearance of the segment bar.

| Key | Type | Description |
| --- | --- | --- |
| `fg` | hex | Default foreground color for text |
| `label_fg` | hex | Color for segment labels (e.g. "Profile:", "Model:") |
| `separator_fg` | hex | Color for the separator between segments |
| `separator_char` | string | Literal separator string (default `" \| "`) |
| `empty_value_fg` | hex | Color for the empty placeholder |
| `empty_value_text` | string | Placeholder text when no value is selected (default `"---"`) |

### segments

A dict keyed by segment name (`profile`, `github`, `version`, `model`,
`directory`, `mcp`, `permissions`). Each segment entry supports:

| Key | Type | Description |
| --- | --- | --- |
| `value_fg` | hex | Foreground for the selected value when not focused |
| `focus_bg` | hex | Background when the segment is focused |
| `focus_fg` | hex | Foreground when the segment is focused |
| `option_fg` | hex | Color for fan-out options (non-selected values above/below the bar) |
| `unavailable_fg` | hex | Color for unavailable options (uninstalled versions, unauthenticated profiles) |

Each segment has its own color palette, giving the bar a distinct accent per
section. The renderer uses `value_fg` for the segment's minimap block as well.

### search

Controls colors during fuzzy search within a segment.

| Key | Type | Description |
| --- | --- | --- |
| `cursor_fg` | hex | Color of the text cursor (`_`) in search/create mode |
| `match_fg` | hex | Highlight color for characters that match the search query |
| `no_match_fg` | hex | Color for the search buffer when zero options match |

### overflow

Controls the scroll indicators that appear when the segment bar overflows
the terminal width. See the dedicated section below.

| Key | Type | Description |
| --- | --- | --- |
| `arrow_fg` | hex | Color for the edge arrows (`<2` and `3>`) |
| `minimap_fg` | hex | Color for minimap blocks of segments with no value |
| `minimap_focused_bg` | hex | Background highlight for the focused segment's minimap block |
| `minimap_char` | string | Character used for each minimap block (default `"▪"`, a small filled square) |

### forms

Controls the profile creation wizard and other form-based UI screens.

| Key | Type | Description |
| --- | --- | --- |
| `title_fg` | hex | Form title color |
| `focus_bg` | hex | Background for the focused field |
| `focus_fg` | hex | Foreground for the focused field |
| `field_fg` | hex | Default field text color |
| `error_fg` | hex | Validation error message color |
| `hint_fg` | hex | Hint text color at the bottom of forms |
| `cursor_fg` | hex | Text cursor color in input fields |
| `readonly_fg` | hex | Color for read-only field values |

## Built-in themes

claudewheel ships two built-in themes materialized as files during first run:

**dark** (`~/.claudewheel/themes/dark.json`) -- designed for terminals with
dark backgrounds. Uses muted blue, green, gold, purple, and red accents for
segments, with low-contrast focus backgrounds (e.g. `#2a2a4e` for profile)
and bright white focus text.

**light** (`~/.claudewheel/themes/light.json`) -- designed for terminals with
light backgrounds. Uses saturated dark accents for segment values and pale
tinted backgrounds for focus highlights (e.g. `#d0e8f0` for profile) with
black focus text.

Both themes are complete: every key in every section is populated. On
startup, the config store deep-merges any missing keys from the built-in
defaults into the on-disk files, so a user who deletes a key gets the default
back on next launch.

## Overflow section styling

When the segment bar is wider than the terminal, the renderer activates
viewport scrolling. The overflow theme section controls three visual elements
that appear in this mode:

**Edge arrows** -- displayed at the left and right margins of the center row.
They show a count of off-screen segments (e.g. `<2` on the left means two
segments are scrolled off to the left, `3>` on the right means three more to
the right). Colored with `arrow_fg`. Four characters are reserved on each
side (`ARROW_MARGIN = 4`) for these indicators.

**Minimap** -- a row of colored block characters in the top-right corner, one
per segment. Each block uses the segment's `value_fg` color. The focused
segment's block gets the `minimap_focused_bg` background. Segments with no
selected value use the `minimap_fg` muted color. The block character is
configurable via `minimap_char`.

The minimap's visibility is controlled by the `"minimap"` key in
`config.json`:
- `"auto"` (default) -- only shown when the bar is scrolling
- `"always"` -- shown even when the bar fits the terminal width

## Creating custom themes

To create a custom theme:

1. Copy an existing theme file as a starting point:
   ```
   cp ~/.claudewheel/themes/dark.json ~/.claudewheel/themes/solarized.json
   ```

2. Edit the new file, changing hex colors to your preference. All five
   sections (`global`, `segments`, `search`, `overflow`, `forms`) should be
   present, though missing keys will be backfilled from the dark theme
   defaults.

3. Set the theme in `~/.claudewheel/config.json`:
   ```json
   { "theme": "solarized" }
   ```

4. Launch claudewheel. The custom theme file is loaded and any missing keys
   are filled from `DEFAULT_THEME_DARK`.

Custom themes are not overwritten by upgrades or migrations. The config
store's deep-merge logic only adds keys that are absent -- it never replaces
existing values. If a future version adds a new theme key, it will appear in
the custom theme file on next startup with its dark-theme default.
