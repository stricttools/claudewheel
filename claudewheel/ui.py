"""Reusable raw-mode selection form: a vertical list navigated with arrow keys."""

from __future__ import annotations

import signal

from .constants import (
    BOLD, CLEAR_LINE, CLEAR_SCREEN, DIM, RESET,
    csi, fg_rgb, move_to,
)
from .terminal import Terminal

# Color constants shared by raw-mode forms (the wizard imports these too).
ACCENT = (107, 138, 255)  # #6B8AFF
DIM_CLR = (136, 136, 136)  # #888888

_HINTS = "Up/Down: navigate  Enter: select  Esc: cancel"


def _option_line(label: str, focused: bool) -> str:
    """Return one styled option line with a focus pointer."""
    if focused:
        return f"{BOLD}{fg_rgb(*ACCENT)}> {label}{RESET}"
    return f"{fg_rgb(*DIM_CLR)}  {label}{RESET}"


def _render_alt(term: Terminal, title: str,
                options: list[tuple[str, str]], focus: int) -> None:
    """Render the form centered on the alt screen (wizard-style)."""
    rows, cols = term.get_size()
    total_height = 2 + len(options)  # title + blank + options; hints on bottom row
    start_row = max(1, (rows - total_height) // 2)
    buf: list[str] = [CLEAR_SCREEN]

    title_col = max(1, (cols - len(title)) // 2)
    buf.append(move_to(start_row, title_col)
               + BOLD + fg_rgb(*ACCENT) + title + RESET)

    col = max(1, (cols - 60) // 2)  # left-align options within a 60-col area
    row = start_row + 2
    for i, (_key, label) in enumerate(options):
        buf.append(move_to(row, col) + CLEAR_LINE + _option_line(label, i == focus))
        row += 1

    hints_col = max(1, (cols - len(_HINTS)) // 2)
    buf.append(move_to(rows, hints_col) + CLEAR_LINE
               + DIM + fg_rgb(*DIM_CLR) + _HINTS + RESET)
    term.write("".join(buf))


def _inline_line_count(options: list[tuple[str, str]]) -> int:
    """Number of lines the inline form occupies: title, blank, options, blank, hints."""
    return len(options) + 4


def _render_inline(term: Terminal, title: str, options: list[tuple[str, str]],
                   focus: int, redraw: bool) -> None:
    """Render the form in place at the cursor position (no alt screen).

    On redraw, the cursor sits just below the form, so move up N lines and
    reprint each line (clearing it first).
    """
    lines = [BOLD + fg_rgb(*ACCENT) + title + RESET, ""]
    for i, (_key, label) in enumerate(options):
        lines.append(_option_line(label, i == focus))
    lines.append("")
    lines.append(DIM + fg_rgb(*DIM_CLR) + _HINTS + RESET)

    buf: list[str] = []
    if redraw:
        buf.append(csi(f"{len(lines)}A"))  # cursor up to the form's first line
    for line in lines:
        buf.append("\r" + CLEAR_LINE + line + "\n")
    term.write("".join(buf))


def _erase_inline(term: Terminal, line_count: int) -> None:
    """Erase the inline form so subsequent output flows cleanly."""
    # Cursor is just below the form: move up, then clear to end of screen.
    term.write(csi(f"{line_count}A") + "\r" + csi("0J"))


def run_selection(title: str, options: list[tuple[str, str]],
                  use_alt_screen: bool = True,
                  initial_key: str | None = None) -> str | None:
    """Run a vertical selection list and return the chosen option's key.

    ``options`` is a list of ``(key, label)`` pairs. Returns the focused
    option's key on Enter, or None on Esc/Ctrl-C. ``initial_key`` pre-focuses
    the matching option (falls back to the first option if absent).
    """
    if not options:
        return None

    focus = 0
    if initial_key is not None:
        for i, (key, _label) in enumerate(options):
            if key == initial_key:
                focus = i
                break

    term = Terminal()
    drawn_inline = False

    def render() -> None:
        nonlocal drawn_inline
        if use_alt_screen:
            _render_alt(term, title, options, focus)
        else:
            _render_inline(term, title, options, focus, redraw=drawn_inline)
            drawn_inline = True

    def on_resize(signum, frame):
        term.rows, term.cols = term.get_size()
        render()

    # Install before entering raw mode / drawing so the previous owner's
    # resize handler can't draw over the form.
    prev_handler = signal.signal(signal.SIGWINCH, on_resize)
    term.enter_raw(alt_screen=use_alt_screen)

    try:
        render()
        while True:
            try:
                key = term.read_key()
            except KeyboardInterrupt:
                return None

            if key in ("ESC", "CTRL_C"):
                return None
            if key == "ENTER":
                return options[focus][0]
            if key == "DOWN":
                focus = (focus + 1) % len(options)
                render()
            elif key == "UP":
                focus = (focus - 1) % len(options)
                render()
    finally:
        if not use_alt_screen and drawn_inline:
            _erase_inline(term, _inline_line_count(options))
        term.exit_raw()
        signal.signal(signal.SIGWINCH, prev_handler or signal.SIG_DFL)
        term.close()
