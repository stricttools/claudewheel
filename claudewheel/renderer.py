"""Draw the segment bar, fan-out options, minimap, and scroll arrows."""

from __future__ import annotations

from .constants import (CLEAR_SCREEN, RESET, BOLD, DIM, move_to)
from .fuzzy import fuzzy_match_positions
from .segment import Segment, SegmentBar
from .terminal import Terminal
from .theme import ThemeColors

# Chars reserved on each side for scroll arrows (e.g. "<99 " or " 99>")
ARROW_MARGIN = 4

# Provenance overlay: source -> single-character glyph
PROVENANCE_GLYPHS: dict[str, str] = {
    "discovered": "*",
    "pinned": "^",
    "defaults": ".",
    "ephemeral": "~",
}


class Renderer:
    """Renders the segment bar centered, with fan-out options and themed colors."""

    def __init__(self, terminal: Terminal, theme: ThemeColors, minimap_mode: str = "auto"):
        self.term = terminal
        self.theme = theme
        self.minimap_mode = minimap_mode
        self._show_provenance: bool = False
        # Tracks where each segment's value is drawn, for fan-out alignment
        self._segment_positions: dict[str, tuple[int, int]] = {}

    def render(self, bar: SegmentBar, flash: str = "", *, show_provenance: bool = False) -> None:
        self._show_provenance = show_provenance
        buf: list[str] = [CLEAR_SCREEN]
        center_row = self.term.rows // 2
        self._render_center_line(buf, bar, center_row)
        self._render_arrows(buf, center_row)
        self._render_fan_out(buf, bar, center_row)
        self._render_status(buf, bar, flash)
        self._render_minimap(buf, bar)
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
            # Append a pending indicator when discovery results are buffered
            pending_marker = "*" if seg.has_pending else ""
            label_str = seg.label + pending_marker + ": "

            # Determine what to display as the value (mirrors rendering logic).
            # Use selected_value (not value) so virtual entries like "+"
            # still render their text instead of the empty placeholder.
            if is_focused and seg.creating:
                raw_value = seg.create_buffer
            elif is_focused and seg.searchable and seg.search_buffer:
                raw_value = seg.search_buffer
            elif seg.selected_value is not None:
                raw_value = seg.selected_value
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

    def _compute_viewport(self, layout: list[dict], total_width: int) -> int:
        """Compute horizontal viewport start offset for narrow terminals.

        When the bar fits in the terminal, returns 0. Otherwise, centers the
        viewport on the focused segment and reserves ARROW_MARGIN on each side
        for scroll indicators.
        """
        if total_width <= self.term.cols:
            return 0

        usable = self.term.cols - 2 * ARROW_MARGIN
        if usable <= 0:
            return 0

        # Find focused segment
        focused = None
        for item in layout:
            if item["is_focused"]:
                focused = item
                break

        # Fallback: if no segment is focused (shouldn't happen), start at 0
        if focused is None:
            return 0

        seg_center = focused["col"] + (focused["label_width"] + focused["value_width"]) // 2
        vp_start = seg_center - usable // 2
        vp_start = max(0, min(vp_start, total_width - usable))
        return vp_start

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

        # Compute viewport offset for horizontal scrolling
        vp_start = self._compute_viewport(layout, total_width)
        self._viewport_start = vp_start
        self._scrolling = total_width > self.term.cols

        for li, seg in zip(layout, bar.segments):
            col = li["col"]
            label_str = li["label_str"]
            display_value = li["display_value"]
            is_focused = li["is_focused"]
            sc = self._seg_colors(seg.key)

            # When scrolling, translate logical column to screen column
            # and skip segments entirely outside the visible area.
            if self._scrolling:
                screen_col = col - vp_start + ARROW_MARGIN
                cursor_extra = 1 if li["has_cursor"] else 0
                seg_right = screen_col + li["label_width"] + li["value_width"] + cursor_extra
                right_margin = self.term.cols - ARROW_MARGIN
                # Visible if right edge past left margin AND left edge before right margin
                if seg_right <= ARROW_MARGIN or screen_col >= right_margin:
                    continue
                render_col = screen_col

                # --- Edge clipping for partially visible segments ---
                # Right clipping: truncate label/value if they extend past right_margin
                max_chars = right_margin - render_col
                full_width = li["label_width"] + li["value_width"] + cursor_extra
                if max_chars < full_width:
                    if max_chars <= 0:
                        continue
                    if max_chars < len(label_str):
                        label_str = label_str[:max_chars]
                        display_value = ""
                    else:
                        avail_for_value = max_chars - len(label_str)
                        display_value = display_value[:avail_for_value]
                    # Suppress cursor if there's no room after value
                    li = dict(li)
                    li["has_cursor"] = False

                # Left clipping: skip leading characters that fall before ARROW_MARGIN
                if render_col < ARROW_MARGIN:
                    skip = ARROW_MARGIN - render_col
                    total_text = len(label_str) + len(display_value)
                    if skip >= total_text:
                        continue
                    if skip >= len(label_str):
                        # Label entirely off-screen, trim value from the left
                        value_skip = skip - len(label_str)
                        label_str = ""
                        display_value = display_value[value_skip:]
                    else:
                        label_str = label_str[skip:]
                    render_col = ARROW_MARGIN
                    li = dict(li)
                    li["has_cursor"] = False
            else:
                render_col = col

            # Record position of the value (not label) for fan-out alignment
            # Uses screen-relative coords when scrolling so fan-out aligns correctly
            value_col = render_col + len(label_str)
            self._segment_positions[seg.key] = (value_col, len(display_value))

            buf.append(move_to(center_row, render_col))

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
                if seg.selected_value == "+":
                    # Virtual "+" sentinel rendered dim when not focused
                    buf.append(DIM)
                    buf.append(display_value)
                    buf.append(RESET)
                elif seg.value is None:
                    buf.append(th.empty_value_fg)
                    buf.append(display_value)
                    buf.append(RESET)
                elif seg.state.has_installed and not seg.state.is_installed(seg.value):
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
            # Compute separator col from the actual (possibly clipped) strings
            sep_col = render_col + len(label_str) + len(display_value) + (1 if li["has_cursor"] else 0)
            if seg is not bar.segments[-1]:
                # When scrolling, skip separators that fall outside the visible area
                if self._scrolling:
                    if sep_col + len(sep) <= ARROW_MARGIN or sep_col >= self.term.cols - ARROW_MARGIN:
                        continue
                buf.append(move_to(center_row, sep_col))
                buf.append(th.separator_fg)
                buf.append(sep)
                buf.append(RESET)

    def _count_offscreen(self) -> tuple[int, int]:
        """Count segments fully off-screen to the left and right of the viewport."""
        left_count = 0
        right_count = 0
        right_margin = self.term.cols - ARROW_MARGIN
        for seg in self._bar_layout:
            screen_col = seg["col"] - self._viewport_start + ARROW_MARGIN
            seg_right = screen_col + seg["label_width"] + seg["value_width"]
            if seg_right <= ARROW_MARGIN:
                left_count += 1
            elif screen_col >= right_margin:
                right_count += 1
        return left_count, right_count

    def _render_arrows(self, buf: list[str], center_row: int) -> None:
        """Render edge arrows with off-screen segment counts when scrolling."""
        if not self._scrolling:
            return
        # In degenerate terminals (width < 2*ARROW_MARGIN+1), there is no room
        # for arrows plus even one column of content -- bail out.
        if self.term.cols < 2 * ARROW_MARGIN + 1:
            return
        left_count, right_count = self._count_offscreen()
        if left_count > 0:
            text = f"<{left_count}"
            buf.append(move_to(center_row, 1))
            buf.append(self.theme.overflow_arrow_fg)
            buf.append(text)
            buf.append(RESET)
        if right_count > 0:
            text = f"{right_count}>"
            buf.append(move_to(center_row, self.term.cols - len(text)))
            buf.append(self.theme.overflow_arrow_fg)
            buf.append(text)
            buf.append(RESET)

    def _render_fan_out(
        self, buf: list[str], bar: SegmentBar, center_row: int
    ) -> None:
        """Render non-selected options vertically above/below the focused segment."""
        seg = bar.focused
        if not seg.show_options or len(seg.display_options) <= 1:
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
            opts = seg.display_options
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
            # Clip fan-out options at screen edges when scrolling
            if self._scrolling:
                if value_col < 1:
                    continue
                if value_col + len(display) > self.term.cols:
                    avail = self.term.cols - value_col
                    if avail <= 0:
                        continue
                    display = display[:avail]
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
            # Clip fan-out options at screen edges when scrolling
            if self._scrolling:
                if value_col < 1:
                    continue
                if value_col + len(display) > self.term.cols:
                    avail = self.term.cols - value_col
                    if avail <= 0:
                        continue
                    display = display[:avail]
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

        When the provenance overlay is active, a dim single-character glyph
        is prepended to indicate the option's source collection.
        """
        # Provenance prefix: dim glyph + space when overlay is active
        provenance_prefix = ""
        if self._show_provenance and opt_text not in (self.theme.empty_value_text, "+"):
            source = seg.state.source_of(opt_text)
            glyph = PROVENANCE_GLYPHS.get(source or "", " ")
            provenance_prefix = glyph + " "
            # Shorten display to compensate for the 2-char prefix
            display = display[:max(0, len(display) - 2)]

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

        # Emit provenance prefix (dim) before the option content
        if provenance_prefix:
            buf.append(DIM)
            buf.append(provenance_prefix)
            buf.append(RESET)

        if seg.state.has_installed and not seg.state.is_installed(opt_text):
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

    def _render_minimap(self, buf: list[str], bar: SegmentBar) -> None:
        """Render a minimap of colored blocks in the top-right corner.

        Each segment is one block character. Focused segment gets a highlight
        background; segments with no value are muted; others use their accent color.
        """
        # Decide whether to show
        if self.minimap_mode == "always":
            pass  # always show
        elif self.minimap_mode == "auto":
            if not getattr(self, "_scrolling", False):
                return
        else:
            # Unrecognized mode: treat as "auto"
            if not getattr(self, "_scrolling", False):
                return

        num_segments = len(bar.segments)
        start_col = self.term.cols - num_segments
        if start_col < 1:
            return  # terminal too narrow for minimap

        buf.append(move_to(1, start_col))
        for i, seg in enumerate(bar.segments):
            sc = self._seg_colors(seg.key)
            value_fg = sc.get("value_fg", "")

            if i == bar.focus_idx:
                # Focused: accent foreground + highlight background
                buf.append(self.theme.overflow_minimap_focused_bg + value_fg)
            elif seg.value is None:
                # No value selected: muted foreground
                buf.append(self.theme.overflow_minimap_fg)
            else:
                # Has a value, not focused: segment accent color
                buf.append(value_fg)

            buf.append(self.theme.overflow_minimap_char)
            buf.append(RESET)

    def _render_status(self, buf: list[str], bar: SegmentBar, flash: str = "") -> None:
        buf.append(move_to(self.term.rows, 2))
        if flash:
            # Show flash message prominently (bold + empty_value_fg for visibility)
            buf.append(BOLD + self.theme.empty_value_fg)
            buf.append(flash[: self.term.cols - 4])
            buf.append(RESET)
            return
        # Provenance legend replaces normal hints when overlay is active
        if self._show_provenance:
            legend = "* discovered  ^ pinned  . default  ~ ephemeral   ?: hide"
            buf.append(DIM)
            buf.append(legend[: self.term.cols - 4])
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
                hints = "type: search   tab: accept   esc: clear   ?: sources   q: quit"
        else:
            hints = "arrows: navigate   enter: launch   ?: sources   q: quit"
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
