"""Themed widget layer: form fields, a form runner, and fullscreen pages."""

from __future__ import annotations

import contextlib
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import FrameType
from typing import Any

from .constants import (
    BOLD, CLEAR_LINE, CLEAR_SCREEN, RESET,
    csi, move_to,
)
from .terminal import Terminal
from .theme import ThemeColors

def _erase_inline(term: Terminal, line_count: int) -> None:
    """Erase the inline form so subsequent output flows cleanly."""
    # Cursor is just below the form: move up, then clear to end of screen.
    term.write(csi(f"{line_count}A") + "\r" + csi("0J"))


def run_selection(title: str, options: list[tuple[str, str]],
                  theme: ThemeColors, terminal: Terminal,
                  use_alt_screen: bool = True,
                  initial_key: str | None = None) -> str | None:
    """Run a vertical selection list and return the chosen option's key.

    ``options`` is a list of ``(key, label)`` pairs. Returns the focused
    option's key on Enter, or None on Esc/Ctrl-C. ``initial_key`` pre-focuses
    the matching option (falls back to the first option if absent).

    Built on run_form: theme and terminal are required, and the terminal
    semantics (borrowed when already raw, owned raw cycle otherwise) apply.
    """
    if not options:
        return None

    keys = [key for key, _label in options]
    value = initial_key if initial_key in keys else keys[0]
    field = FormField("choice", "select", value=value, options=list(options))
    result = run_form(title, [field], theme, terminal,
                      use_alt_screen=use_alt_screen)
    if result is None:
        return None
    choice = result["choice"]
    return choice if isinstance(choice, str) else None


# ---------------------------------------------------------------------------
# Widget layer: FormField definitions, themed rendering, and the form runner.
# ---------------------------------------------------------------------------

# The left-aligned field area is centered within this many columns.
_FIELD_AREA_WIDTH = 60

# Context-sensitive keyboard hints per field type (lowercase, status-row style).
_FIELD_HINTS = {
    "text": "type: edit   tab: next   enter: submit   esc: cancel",
    "radio": "left/right: cycle   tab: next   esc: cancel",
    "checkbox": "space: toggle   tab: next   esc: cancel",
    "button": "enter: select   tab: next   esc: cancel",
    "select": "up/down: navigate   enter: select   esc: cancel",
}


@dataclass(eq=False)
class FormField:
    """One widget in a form: text input, radio group, checkbox, button,
    readonly line, or selection list.

    ``options`` holds plain strings for radio groups and ``(key, label)``
    pairs for selection lists (whose ``value`` is the focused option's key).
    ``visible`` (if set) is called with the full field list and hides the
    field when it returns False. ``on_change`` (if set) is called with the
    full field list after every text edit, so dependent fields can update.
    """

    key: str
    field_type: str  # "text" | "radio" | "checkbox" | "readonly" | "button" | "select"
    label: str = ""
    value: str | bool | None = None
    options: list[Any] | None = None
    visible: Callable[[list["FormField"]], bool] | None = None
    on_change: Callable[[list["FormField"]], None] | None = None


def get_field(fields: list[FormField], key: str) -> FormField:
    """Look up a form field by its key."""
    for f in fields:
        if f.key == key:
            return f
    raise KeyError(f"No field with key {key!r}")


def _visible_indices(fields: list[FormField]) -> set[int]:
    """Indices of fields whose ``visible`` predicate allows rendering."""
    return {i for i, f in enumerate(fields)
            if f.visible is None or f.visible(fields)}


def _focusable_indices(fields: list[FormField]) -> list[int]:
    """Indices of fields that are visible and interactive."""
    visible = _visible_indices(fields)
    return [i for i, f in enumerate(fields)
            if i in visible and f.field_type != "readonly"]


def _move_focus(fields: list[FormField], focus: int, step: int) -> int:
    """Move focus by *step* through focusable fields, wrapping at both ends."""
    focusable = _focusable_indices(fields)
    if focus in focusable:
        pos = focusable.index(focus)
        return focusable[(pos + step) % len(focusable)]
    return focusable[0]


def _cycle_radio(f: FormField, step: int) -> None:
    """Cycle a radio field's value by *step*, wrapping."""
    opts = f.options or []
    if not opts:
        return
    idx = opts.index(f.value) if f.value in opts else 0
    f.value = opts[(idx + step) % len(opts)]


def _cycle_select(f: FormField, step: int) -> None:
    """Cycle a select field's focused option key by *step*, wrapping."""
    keys = [key for key, _label in (f.options or [])]
    if not keys:
        return
    idx = keys.index(f.value) if f.value in keys else 0
    f.value = keys[(idx + step) % len(keys)]


def _hints_for_field(f: FormField) -> str:
    """Return keyboard hint text for the focused field."""
    return _FIELD_HINTS.get(f.field_type, "esc: cancel")


def _field_lines(f: FormField, focused: bool, th: ThemeColors) -> list[str]:
    """Styled display lines for one field (selects span multiple lines).

    Focused widgets render as a forms_focus_bg + forms_focus_fg span then
    RESET (the segment-bar focus idiom), never bold-foreground.
    """
    if f.field_type == "select":
        lines: list[str] = []
        for key, label in (f.options or []):
            pointer = "> " if key == f.value else "  "
            if focused and key == f.value:
                style = th.forms_focus_bg + th.forms_focus_fg
            else:
                style = th.forms_field_fg
            lines.append(f"{style}{pointer}{label}{RESET}")
        return lines

    label_style = (th.forms_focus_bg + th.forms_focus_fg) if focused \
        else th.forms_field_fg

    if f.field_type == "button":
        return [f"{label_style}[ {f.label} ]{RESET}"]
    if f.field_type == "text":
        cursor = f"{th.forms_cursor_fg}_{RESET}" if focused else ""
        return [f"{label_style}{f.label}:{RESET} "
                f"{th.forms_field_fg}[{f.value or ''}{RESET}{cursor}"
                f"{th.forms_field_fg}]{RESET}"]
    if f.field_type == "readonly":
        return [f"{th.forms_field_fg}{f.label}:{RESET} {th.forms_readonly_fg}{f.value}{RESET}"]
    if f.field_type == "radio":
        parts: list[str] = []
        for opt in (f.options or []):
            if opt == f.value:
                parts.append(f"{BOLD}{th.forms_field_fg}(*) {opt}{RESET}")
            else:
                parts.append(f"{th.forms_field_fg}( ) {opt}{RESET}")
        return [f"{label_style}{f.label}:{RESET} " + "  ".join(parts)]
    if f.field_type == "checkbox":
        marker = "[x]" if f.value else "[ ]"
        return [f"{label_style}{marker} {f.label}{RESET}"]
    return [f"{label_style}{f.label}{RESET}"]


def _render_form_alt(term: Terminal, th: ThemeColors, title: str,
                     fields: list[FormField], focus: int, error: str) -> None:
    """Render the form centered on a full screen (absolute positioning)."""
    rows, cols = term.get_size()
    visible = _visible_indices(fields)

    line_count = 0
    for i in sorted(visible):
        f = fields[i]
        line_count += len(f.options or []) if f.field_type == "select" else 1

    total_height = 2 + line_count  # title + blank + field lines
    start_row = max(1, (rows - total_height) // 2)
    buf: list[str] = [CLEAR_SCREEN]

    title_col = max(1, (cols - len(title)) // 2)
    buf.append(move_to(start_row, title_col)
               + BOLD + th.forms_title_fg + title + RESET)

    left_col = max(1, (cols - _FIELD_AREA_WIDTH) // 2)
    row = start_row + 2
    for i, f in enumerate(fields):
        if i not in visible:
            continue
        if f.field_type == "button":
            col = max(1, (cols - len(f.label) - 4) // 2)
        else:
            col = left_col
        for line in _field_lines(f, i == focus, th):
            buf.append(move_to(row, col) + line)
            row += 1

    # Error message on the row above the hints (bottom - 1)
    if error:
        error_col = max(1, (cols - len(error)) // 2)
        buf.append(move_to(rows - 1, error_col)
                   + BOLD + th.forms_error_fg + error + RESET)

    # Keyboard hints on the bottom row, column 2 (the status-row convention)
    hints = _hints_for_field(fields[focus])
    buf.append(move_to(rows, 2) + th.forms_hint_fg + hints + RESET)
    term.write("".join(buf))


def _render_form_inline(term: Terminal, th: ThemeColors, title: str,
                        fields: list[FormField], focus: int, error: str,
                        prev_lines: int) -> int:
    """Render the form in place at the cursor position (no alt screen).

    On redraw the cursor sits just below the form: move up *prev_lines* and
    reprint. Returns the new line count (visibility changes can alter it).
    """
    lines = [BOLD + th.forms_title_fg + title + RESET, ""]
    visible = _visible_indices(fields)
    for i, f in enumerate(fields):
        if i not in visible:
            continue
        lines.extend(_field_lines(f, i == focus, th))
    lines.append("")
    lines.append(BOLD + th.forms_error_fg + error + RESET if error else "")
    lines.append(th.forms_hint_fg + _hints_for_field(fields[focus]) + RESET)

    buf: list[str] = []
    if prev_lines:
        buf.append(csi(f"{prev_lines}A"))  # cursor up to the form's first line
    for line in lines:
        buf.append("\r" + CLEAR_LINE + line + "\n")
    # Clear leftovers when the previous frame was taller (visibility collapse)
    buf.append(csi("0J"))
    term.write("".join(buf))
    return len(lines)


@contextlib.contextmanager
def _form_session(terminal: Terminal, use_alt_screen: bool, render: Callable[[], None]) -> Iterator[None]:
    """Signal swap and raw-mode ownership around a form or page.

    Borrowed mode (terminal already raw): render only -- never enter or exit
    raw mode, and never close. Owned mode (cooked terminal): enter_raw at
    start, exit_raw at end -- but never close a terminal we didn't create.
    The resize handler is installed before entering raw mode so the previous
    owner's handler can't draw over the form.

    SIGTERM and SIGHUP are caught so the terminal can be restored on
    abrupt termination. In owned mode the handler calls exit_raw before
    raising SystemExit; in borrowed mode it raises SystemExit directly
    (the outer owner is responsible for terminal cleanup).
    """
    def on_resize(signum: int, frame: FrameType | None) -> None:
        terminal.rows, terminal.cols = terminal.get_size()
        render()

    borrowed = bool(getattr(terminal, "_in_raw", False))

    def on_term(signum: int, frame: FrameType | None) -> None:
        if not borrowed:
            terminal.exit_raw()
        raise SystemExit(1)

    prev_winch = signal.signal(signal.SIGWINCH, on_resize)
    prev_term = signal.signal(signal.SIGTERM, on_term)
    prev_hup = signal.signal(signal.SIGHUP, on_term)
    if not borrowed:
        terminal.enter_raw(alt_screen=use_alt_screen)
    try:
        yield
    finally:
        if not borrowed:
            terminal.exit_raw()
        signal.signal(signal.SIGWINCH, prev_winch or signal.SIG_DFL)
        signal.signal(signal.SIGTERM, prev_term or signal.SIG_DFL)
        signal.signal(signal.SIGHUP, prev_hup or signal.SIG_DFL)


def run_form(title: str, fields: list[FormField], theme: ThemeColors,
             terminal: Terminal, *, use_alt_screen: bool = True,
             validate: Callable[[list[FormField]], str | None] | None = None,
             ) -> dict[str, object] | None:
    """Run a form's key loop and return ``{key: value}`` or None on cancel.

    The runner owns focus traversal (TAB/SHIFT_TAB/UP/DOWN), radio cycling
    (LEFT/RIGHT/SPACE), checkbox toggling (SPACE), text editing, conditional
    field visibility, ENTER submit (from text, button, or select fields), and
    ESC/CTRL_C cancel. ``validate`` runs on submit; a returned error string
    blocks the submit and is shown in forms_error_fg.

    If *terminal* is already raw, the form renders borrowed (fullscreen pages
    in the existing screen, regardless of ``use_alt_screen``). Otherwise the
    runner enters raw mode itself -- with an alt screen when ``use_alt_screen``
    is True, or inline (in-place, line-based) when False.
    """
    focusable = _focusable_indices(fields)
    if not focusable:
        raise ValueError("run_form needs at least one focusable field")
    focus = focusable[0]
    error = ""
    drawn_lines = 0  # inline mode: line count of the last frame

    borrowed = bool(getattr(terminal, "_in_raw", False))
    fullscreen = borrowed or use_alt_screen

    def render() -> None:
        nonlocal drawn_lines
        if fullscreen:
            _render_form_alt(terminal, theme, title, fields, focus, error)
        else:
            drawn_lines = _render_form_inline(
                terminal, theme, title, fields, focus, error, drawn_lines)

    def try_submit() -> dict[str, object] | None:
        nonlocal error
        err = validate(fields) if validate else None
        if err:
            error = err
            return None
        return {f.key: f.value for f in fields}

    with _form_session(terminal, use_alt_screen, render):
        try:
            render()
            while True:
                try:
                    key = terminal.read_key()
                except KeyboardInterrupt:
                    return None

                if key in ("ESC", "CTRL_C"):
                    return None

                f = fields[focus]

                if f.field_type == "select":
                    if key == "ENTER":
                        result = try_submit()
                        if result is not None:
                            return result
                    elif key == "DOWN":
                        _cycle_select(f, 1)
                    elif key == "UP":
                        _cycle_select(f, -1)
                    elif key == "TAB":
                        error = ""
                        focus = _move_focus(fields, focus, 1)
                    elif key == "SHIFT_TAB":
                        error = ""
                        focus = _move_focus(fields, focus, -1)
                    render()
                    continue

                if key in ("TAB", "DOWN"):
                    error = ""
                    focus = _move_focus(fields, focus, 1)
                elif key in ("SHIFT_TAB", "UP"):
                    error = ""
                    focus = _move_focus(fields, focus, -1)
                elif key == "ENTER":
                    if f.field_type in ("text", "button"):
                        result = try_submit()
                        if result is not None:
                            return result
                elif key == " " and f.field_type == "checkbox":
                    f.value = not f.value
                    error = ""
                elif (key in ("LEFT", "RIGHT") or key == " ") \
                        and f.field_type == "radio":
                    _cycle_radio(f, -1 if key == "LEFT" else 1)
                    error = ""
                elif key == "BACKSPACE" and f.field_type == "text" \
                        and isinstance(f.value, str):
                    f.value = f.value[:-1]
                    error = ""
                    if f.on_change:
                        f.on_change(fields)
                elif f.field_type == "text" and len(key) == 1 \
                        and key.isprintable():
                    current = f.value if isinstance(f.value, str) else ""
                    f.value = current + key
                    error = ""
                    if f.on_change:
                        f.on_change(fields)

                render()
        finally:
            if not fullscreen and drawn_lines:
                _erase_inline(terminal, drawn_lines)


def show_page(title: str, lines: list[str], theme: ThemeColors, terminal: Terminal, *,
              hint: str = "press any key to continue") -> str:
    """Render a fullscreen page of text and wait for a single keypress.

    Uses the same terminal semantics as run_form: borrowed when the terminal
    is already raw, otherwise an owned alt-screen raw cycle (never closed).

    Returns the key string that was pressed (empty string on interrupt).
    """
    def render() -> None:
        rows, cols = terminal.get_size()
        total_height = 2 + len(lines)
        start_row = max(1, (rows - total_height) // 2)
        buf: list[str] = [CLEAR_SCREEN]
        title_col = max(1, (cols - len(title)) // 2)
        buf.append(move_to(start_row, title_col)
                   + BOLD + theme.forms_title_fg + title + RESET)
        left_col = max(1, (cols - _FIELD_AREA_WIDTH) // 2)
        row = start_row + 2
        for line in lines:
            buf.append(move_to(row, left_col)
                       + theme.forms_field_fg + line + RESET)
            row += 1
        buf.append(move_to(rows, 2) + theme.forms_hint_fg + hint + RESET)
        terminal.write("".join(buf))

    with _form_session(terminal, True, render):
        render()
        try:
            return terminal.read_key()
        except KeyboardInterrupt:
            return ""
