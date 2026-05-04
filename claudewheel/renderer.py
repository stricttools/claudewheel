"""Renderer class for drawing the segment bar TUI."""

from __future__ import annotations

from .constants import (CLEAR_SCREEN, RESET, BOLD, DIM, move_to)
from .fuzzy import fuzzy_match_positions
from .segment import Segment, SegmentBar
from .terminal import Terminal
from .theme import ThemeColors


class Renderer:
    """Renders the segment bar centered, with fan-out options and themed colors."""

    def __init__(self, terminal: Terminal, theme: ThemeColors):
        self.term = terminal
        self.theme = theme
        # Tracks where each segment's value is drawn, for fan-out alignment
        self._segment_positions: dict[str, tuple[int, int]] = {}

    def render(self, bar: SegmentBar, flash: str = "") -> None:
        buf: list[str] = [CLEAR_SCREEN]
        center_row = self.term.rows // 2
        self._render_center_line(buf, bar, center_row)
        self._render_fan_out(buf, bar, center_row)
        self._render_status(buf, bar, flash)
        self.term.write("".join(buf))
        self.term.flush()

    def _seg_colors(self, key: str) -> dict[str, str]:
        """Get per-segment color dict, falling back to empty strings."""
        return self.theme.segment_colors.get(key, {})

    def _compute_bar_layout(
        self, bar: SegmentBar
    ) -> tuple[list[dict], int]:
        """Compute column positions and widths for each segment without rendering.

        Returns (layout, total_width) where layout is a list of dicts with keys:
        key, col, label_str, label_width, display_value, value_width, has_cursor,
        is_focused.
        """
        th = self.theme
        col = 2  # 1-indexed, with left margin
        sep = th.separator_char
        layout: list[dict] = []

        for i, seg in enumerate(bar.segments):
            is_focused = i == bar.focus_idx
            label_str = seg.label + ": "

            # Determine what to display as the value (mirrors rendering logic)
            if is_focused and seg.creating:
                raw_value = seg.create_buffer
            elif is_focused and seg.searchable and seg.search_buffer:
                raw_value = seg.search_buffer
            elif seg.value is not None:
                raw_value = seg.value
            else:
                raw_value = th.empty_value_text
            display_value = self._fit_value(raw_value, seg.min_width, seg.max_width)

            has_cursor = is_focused and (
                seg.creating or (seg.searchable and seg.search_buffer)
            )
            extra = 1 if has_cursor else 0

            layout.append({
                "key": seg.key,
                "col": col,
                "label_str": label_str,
                "label_width": len(label_str),
                "display_value": display_value,
                "value_width": len(display_value),
                "has_cursor": has_cursor,
                "is_focused": is_focused,
            })

            col += len(label_str) + len(display_value) + extra

            # Separator width (not after the last segment)
            if i < len(bar.segments) - 1:
                col += len(sep)

        return layout, col

    def _render_center_line(
        self, buf: list[str], bar: SegmentBar, center_row: int
    ) -> None:
        self._segment_positions.clear()
        th = self.theme
        sep = th.separator_char

        # Pre-compute layout so later phases can access positions without rendering
        layout, total_width = self._compute_bar_layout(bar)
        self._bar_layout = layout
        self._bar_total_width = total_width

        for li, seg in zip(layout, bar.segments):
            col = li["col"]
            label_str = li["label_str"]
            display_value = li["display_value"]
            is_focused = li["is_focused"]
            sc = self._seg_colors(seg.key)

            # Record position of the value (not label) for fan-out alignment
            value_col = col + li["label_width"]
            self._segment_positions[seg.key] = (value_col, li["value_width"])

            buf.append(move_to(center_row, col))

            if is_focused:
                focus_bg = sc.get("focus_bg", "")
                focus_fg = sc.get("focus_fg", "")
                buf.append(focus_bg + focus_fg)
                buf.append(label_str)
                if seg.creating:
                    # Render creation text input + cursor character
                    buf.append(display_value)
                    buf.append(focus_bg + th.search_cursor_fg + "_" + RESET)
                elif seg.searchable and seg.search_buffer:
                    # Render search text + cursor character. If the search
                    # matches zero options, paint the buffer text red so the
                    # user sees their typing isn't matching anything.
                    if not seg.filtered_options:
                        no_match_fg = th.search_no_match_fg or focus_fg
                        # Re-establish bg + no-match fg (overriding focus_fg above)
                        buf.append(focus_bg + no_match_fg)
                    buf.append(display_value)
                    buf.append(focus_bg + th.search_cursor_fg + "_" + RESET)
                else:
                    buf.append(display_value)
                    buf.append(RESET)
            else:
                # Label in label color, value in per-segment value color
                buf.append(th.label_fg)
                buf.append(label_str)
                buf.append(RESET)
                if seg.value is None:
                    buf.append(th.empty_value_fg)
                    buf.append(display_value)
                    buf.append(RESET)
                elif seg.value == "+":
                    # "+" sentinel rendered dim when not focused
                    buf.append(DIM)
                    buf.append(display_value)
                    buf.append(RESET)
                elif seg.installed and seg.value not in seg.installed:
                    # Uninstalled option: render dimly with unavailable color
                    unavail = sc.get("unavailable_fg", "") or DIM
                    buf.append(unavail)
                    buf.append(display_value)
                    buf.append(RESET)
                elif seg.unavailable and seg.value in seg.unavailable:
                    # Option unavailable due to cross-segment requirement
                    unavail = sc.get("unavailable_fg", "") or DIM
                    buf.append(unavail)
                    buf.append(display_value)
                    buf.append(RESET)
                else:
                    buf.append(sc.get("value_fg", ""))
                    buf.append(display_value)
                    buf.append(RESET)

            # Separator between segments (not after the last one)
            # Compute separator col from layout: after value + cursor
            sep_col = col + li["label_width"] + li["value_width"] + (1 if li["has_cursor"] else 0)
            if seg is not bar.segments[-1]:
                buf.append(move_to(center_row, sep_col))
                buf.append(th.separator_fg)
                buf.append(sep)
                buf.append(RESET)

    def _render_fan_out(
        self, buf: list[str], bar: SegmentBar, center_row: int
    ) -> None:
        """Render non-selected options vertically above/below the focused segment."""
        seg = bar.focused
        if not seg.show_options or len(seg.options) <= 1:
            return
        if seg.key not in self._segment_positions:
            return

        value_col, display_width = self._segment_positions[seg.key]
        sc = self._seg_colors(seg.key)
        option_fg = sc.get("option_fg", DIM)
        unavail_fg = sc.get("unavailable_fg", "") or DIM

        # Determine which options list and selected index to use
        if seg.search_buffer:
            opts = seg.filtered_options
            if not opts:
                return
            # In search mode, the center line shows the search text,
            # and ALL filtered options fan out below
            above: list[str] = []
            below: list[str] = opts
        else:
            opts = seg.options
            if seg.selected_idx < 0:
                # Nothing selected: all options fan out below
                above = []
                below = opts
            else:
                sel_idx = seg.selected_idx
                # Options before selected go above (reversed so closest is nearest)
                above = list(reversed(opts[:sel_idx]))
                # Options after selected go below
                below = opts[sel_idx + 1:]
                # Show "---" (blank state) in fan-out when wrap is enabled
                if seg.wrap:
                    above.append(self.theme.empty_value_text)

        # Render above options
        for offset, opt_text in enumerate(above, start=1):
            row = center_row - offset
            if row < 1:
                break
            display = self._fit_value(opt_text, seg.min_width, seg.max_width)
            buf.append(move_to(row, value_col))
            self._render_option(
                buf, seg, opt_text, display, option_fg, unavail_fg
            )

        # Render below options
        for offset, opt_text in enumerate(below, start=1):
            row = center_row + offset
            if row >= self.term.rows:
                break
            display = self._fit_value(opt_text, seg.min_width, seg.max_width)
            buf.append(move_to(row, value_col))
            self._render_option(
                buf, seg, opt_text, display, option_fg, unavail_fg
            )

    def _render_option(
        self,
        buf: list[str],
        seg: Segment,
        opt_text: str,
        display: str,
        option_fg: str,
        unavail_fg: str,
    ) -> None:
        """Render a single fan-out option, applying per-char match highlighting
        when a search buffer is active and the option is a normal (non-special)
        entry. Special entries ("+" sentinel, empty placeholder, unavailable,
        uninstalled) keep their dedicated color and are not highlighted.
        """
        # Special entries: own color, no match highlighting
        if opt_text == self.theme.empty_value_text:
            buf.append(self.theme.empty_value_fg)
            buf.append(display)
            buf.append(RESET)
            return
        if opt_text == "+":
            buf.append(DIM)
            buf.append(display)
            buf.append(RESET)
            return
        if seg.installed and opt_text not in seg.installed:
            buf.append(unavail_fg)
            buf.append(display)
            buf.append(RESET)
            return
        if seg.unavailable and opt_text in seg.unavailable:
            buf.append(unavail_fg)
            buf.append(display)
            buf.append(RESET)
            return

        # Normal option: highlight matched chars if a search is active
        if seg.search_buffer:
            self._render_highlighted_option(
                buf, seg.search_buffer, opt_text, display, option_fg
            )
        else:
            buf.append(option_fg)
            buf.append(display)
            buf.append(RESET)

    def _render_highlighted_option(
        self,
        buf: list[str],
        query: str,
        opt_text: str,
        display: str,
        base_fg: str,
    ) -> None:
        """Append `display` to buf with chars at fuzzy-matched positions
        rendered in search_match_fg, others in base_fg. Positions are computed
        against opt_text but applied to display (which may be truncated/padded);
        positions outside display's range are dropped, and we exclude the final
        ellipsis char if the value was truncated.
        """
        th = self.theme
        match_fg = th.search_match_fg or base_fg
        positions = fuzzy_match_positions(query, opt_text)
        # If display was truncated (ends with ellipsis), the last char is the
        # ellipsis -- never highlight it. Cap valid positions at len(display)-1
        # if truncated, or len(display) otherwise (padding is fine to skip).
        truncated = len(opt_text) > len(display) and display.endswith("\u2026")
        max_pos = (len(display) - 1) if truncated else len(display)
        pos_set = {p for p in positions if p < max_pos}

        # Walk display chars, switching between match_fg and base_fg as needed
        current = base_fg
        buf.append(current)
        for i, ch in enumerate(display):
            target = match_fg if i in pos_set else base_fg
            if target != current:
                buf.append(target)
                current = target
            buf.append(ch)
        buf.append(RESET)

    def _render_status(self, buf: list[str], bar: SegmentBar, flash: str = "") -> None:
        buf.append(move_to(self.term.rows, 2))
        if flash:
            # Show flash message prominently (bold + empty_value_fg for visibility)
            buf.append(BOLD + self.theme.empty_value_fg)
            buf.append(flash[: self.term.cols - 4])
            buf.append(RESET)
            return
        seg = bar.focused
        if seg.creating:
            hints = "type: name   enter: confirm   esc: cancel"
        elif seg.is_on_plus:
            hints = "enter: create new   arrows: navigate   q: quit"
        elif seg.freeform and seg.search_buffer:
            hints = "type: path   enter: use   tab: match   esc: clear   backspace: delete"
        elif seg.searchable:
            if seg.search_buffer:
                hints = "type: search   tab: accept   esc: clear   backspace: delete   enter: launch"
            else:
                hints = "type: search   tab: accept   esc: clear   q: quit"
        else:
            hints = "arrows: navigate   enter: launch   q: quit"
        buf.append(DIM)
        buf.append(hints[: self.term.cols - 4])
        buf.append(RESET)

    def _fit_value(self, value: str, min_w: int, max_w: int) -> str:
        """Truncate with ellipsis or pad with spaces to fit width constraints."""
        if len(value) > max_w:
            return value[: max_w - 1] + "\u2026"
        if len(value) < min_w:
            return value + " " * (min_w - len(value))
        return value
