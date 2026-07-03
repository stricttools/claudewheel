"""Interactive form wizard for creating and configuring new profiles."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    CLAUDE_SYMLINK,
    CLEAR_SCREEN, CLEAR_LINE, RESET, BOLD, DIM,
    PROFILES_DIR, PROFILE_SHARED_DIRS, SCRIPTS_DIR,
    SHARED_DIR, SHARED_SETTINGS_FILE, SKILLS_DIR,
    move_to, fg_rgb,
)
from .config import ConfigManager
from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .discovery import detect_browsers
from .state import AUTH_BROWSER_KEY, load_state_value, save_state_value
from .tokens import add_token
from .terminal import Terminal
from .ui import ACCENT, DIM_CLR, run_selection

# Checkbox labels -- used to identify the 6 advanced fields
_CHECKBOX_LABELS = [
    "Wire common hooks",
    "Symlink to shared store",
    "Disable recap",
    "10-year cleanup period",
    "Disable auto-memory",
    "Disable Co-Authored-By",
]


@dataclass
class WizardField:
    """Defines a single input field in the profile creation wizard."""

    label: str
    field_type: str  # "text", "radio", "checkbox", "readonly", "button"
    value: str | bool | None = None
    options: list[str] | None = None  # for radio


@dataclass
class WizardResult:
    """Collected values from a completed profile wizard run."""

    name: str
    config_dir: str
    clone_from: str | None  # profile name or None for defaults
    wire_hooks: bool
    symlink_shared: bool
    disable_recap: bool
    cleanup_10y: bool
    disable_memory: bool
    disable_attribution: bool
    cancelled: bool = False


def _get_field(fields: list[WizardField], label: str) -> WizardField:
    """Look up a wizard field by its label."""
    for f in fields:
        if f.label == label:
            return f
    raise KeyError(f"No field with label {label!r}")


def _validate_name(name: str, existing_profiles: list[str]) -> str | None:
    """Return error message, or None if valid."""
    name = name.strip()
    if not name:
        return "Name cannot be empty"
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', name):
        return "Use lowercase letters, digits, hyphens only"
    if name == "default":
        return f"'{name}' is a reserved name"
    config_dir = (PROFILES_DIR / name).expanduser()
    if config_dir.exists():
        return f"~/.claudewheel/profiles/{name} already exists"
    if name in existing_profiles:
        return f"Profile '{name}' already registered"
    return None


def _build_fields(existing_profiles: list[str]) -> list[WizardField]:
    """Build the ordered list of wizard fields."""
    radio_opts = ["Defaults template"] + existing_profiles
    return [
        WizardField("Name", "text", value=""),
        WizardField("Config dir", "readonly", value="~/.claudewheel/profiles/"),
        WizardField("Settings source", "radio", value=radio_opts[0], options=radio_opts),
        WizardField("Advanced", "radio", value="Hide advanced",
                     options=["Hide advanced", "Show advanced"]),
        WizardField("Wire common hooks", "checkbox", value=True),
        WizardField("Symlink to shared store", "checkbox", value=True),
        WizardField("Disable recap", "checkbox", value=True),
        WizardField("10-year cleanup period", "checkbox", value=True),
        WizardField("Disable auto-memory", "checkbox", value=True),
        WizardField("Disable Co-Authored-By", "checkbox", value=True),
        WizardField("Create", "button"),
    ]


def _is_advanced_expanded(fields: list[WizardField]) -> bool:
    """Return True when the Advanced toggle is set to show checkboxes."""
    return _get_field(fields, "Advanced").value == "Show advanced"


def _visible_fields(fields: list[WizardField]) -> list[WizardField]:
    """Return fields that should be rendered based on current Advanced toggle."""
    expanded = _is_advanced_expanded(fields)
    return [f for f in fields
            if expanded or f.label not in _CHECKBOX_LABELS]


def _focusable_indices(fields: list[WizardField]) -> list[int]:
    """Return indices into `fields` that are visible and interactive."""
    visible = _visible_fields(fields)
    visible_labels = {f.label for f in visible}
    return [i for i, f in enumerate(fields)
            if f.label in visible_labels and f.field_type != "readonly"]


def _build_result(fields: list[WizardField]) -> WizardResult:
    """Construct a WizardResult from current field values."""
    name = _get_field(fields, "Name").value.strip()
    source = _get_field(fields, "Settings source").value
    clone = None if source == "Defaults template" else source
    return WizardResult(
        name=name,
        config_dir=f"~/.claudewheel/profiles/{name}",
        clone_from=clone,
        wire_hooks=bool(_get_field(fields, "Wire common hooks").value),
        symlink_shared=bool(_get_field(fields, "Symlink to shared store").value),
        disable_recap=bool(_get_field(fields, "Disable recap").value),
        cleanup_10y=bool(_get_field(fields, "10-year cleanup period").value),
        disable_memory=bool(_get_field(fields, "Disable auto-memory").value),
        disable_attribution=bool(_get_field(fields, "Disable Co-Authored-By").value),
    )


def _hints_for_field(f: WizardField) -> str:
    """Return context-sensitive keyboard hint text for the given field."""
    if f.field_type == "text":
        return "Tab: next  Enter: create  Esc: cancel"
    if f.field_type == "radio":
        return "Left/Right: cycle  Tab: next  Esc: cancel"
    if f.field_type == "checkbox":
        return "Space: toggle  Tab: next  Esc: cancel"
    if f.field_type == "button":
        return "Enter: select  Tab: next  Esc: cancel"
    return "Esc: cancel"


def _render(term: Terminal, fields: list[WizardField], focus: int,
            error: str = "") -> None:
    """Render the full wizard form centered in the terminal."""
    rows, cols = term.get_size()
    visible = _visible_fields(fields)

    # Total lines: 1 title + 1 blank + visible fields + 1 blank (hints) + 1 error + 1 hints
    form_lines = len(visible)
    total_height = 1 + 1 + form_lines
    start_row = max(1, (rows - total_height) // 2)
    buf: list[str] = [CLEAR_SCREEN]

    # Title
    title = "New Profile"
    title_col = max(1, (cols - len(title)) // 2)
    buf.append(move_to(start_row, title_col) + BOLD + fg_rgb(*ACCENT) + title + RESET)

    row = start_row + 2  # skip blank line after title
    visible_set = {f.label for f in visible}
    for i, f in enumerate(fields):
        if f.label not in visible_set:
            continue

        focused = (i == focus)
        color = fg_rgb(*ACCENT) if focused else fg_rgb(*DIM_CLR)
        style = BOLD + color if focused else color

        if f.field_type == "button":
            line = f"{style}[ {f.label} ]{RESET}"
            col = max(1, (cols - len(f.label) - 4) // 2)
            buf.append(move_to(row, col) + CLEAR_LINE + line)
            row += 1
            continue

        # Label + value
        if f.field_type == "text":
            cursor = "_" if focused else ""
            val = f"[{f.value}{cursor}]"
            line = f"{style}{f.label}: {RESET}{val}"
        elif f.field_type == "readonly":
            line = f"{style}{f.label}: {RESET}{DIM}{f.value}{RESET}"
        elif f.field_type == "radio":
            parts: list[str] = []
            for opt in (f.options or []):
                if opt == f.value:
                    marker = "(*)"
                    parts.append(f"{BOLD}{marker} {opt}{RESET}")
                else:
                    marker = "( )"
                    parts.append(f"{marker} {opt}")
            opts_str = "  ".join(parts)
            line = f"{style}{f.label}: {RESET}{opts_str}"
        elif f.field_type == "checkbox":
            marker = "[x]" if f.value else "[ ]"
            line = f"{style}{marker} {f.label}{RESET}"
        else:
            line = f"{style}{f.label}{RESET}"

        col = max(1, (cols - 60) // 2)  # left-align fields within a 60-col area
        buf.append(move_to(row, col) + CLEAR_LINE + line)
        row += 1

    # Error message on the row above hints (bottom - 1)
    error_row = rows - 1
    if error:
        error_col = max(1, (cols - len(error)) // 2)
        buf.append(move_to(error_row, error_col) + CLEAR_LINE
                   + BOLD + fg_rgb(255, 100, 100) + error + RESET)
    else:
        buf.append(move_to(error_row, 1) + CLEAR_LINE)

    # Keyboard hints at the very bottom row
    hints_row = rows
    focused_field = fields[focus]
    hints = _hints_for_field(focused_field)
    hints_col = max(1, (cols - len(hints)) // 2)
    buf.append(move_to(hints_row, hints_col) + CLEAR_LINE
               + DIM + fg_rgb(*DIM_CLR) + hints + RESET)

    term.write("".join(buf))


def _cancelled_result() -> WizardResult:
    """Return a cancelled WizardResult with zero-value fields."""
    return WizardResult("", "", None, False, False, False, False, False,
                        False, cancelled=True)


def run_profile_wizard(existing_profiles: list[str]) -> WizardResult:
    """Run the form TUI and return the user's choices."""
    term = Terminal()
    fields = _build_fields(existing_profiles)
    focusable = _focusable_indices(fields)
    focus_pos = 0  # index into focusable list
    focus = focusable[focus_pos]
    error = ""  # persistent error string

    term.enter_raw()

    def on_resize(signum, frame):
        term.rows, term.cols = term.get_size()
        _render(term, fields, focus, error)

    prev_handler = signal.signal(signal.SIGWINCH, on_resize)

    try:
        _render(term, fields, focus, error)
        while True:
            try:
                key = term.read_key()
            except KeyboardInterrupt:
                return _cancelled_result()

            f = fields[focus]

            if key == "ESC" or key == "CTRL_C":
                return _cancelled_result()

            if key in ("TAB", "DOWN"):
                error = ""  # clear error on focus change
                focus_pos = (focus_pos + 1) % len(focusable)
                focus = focusable[focus_pos]
            elif key in ("SHIFT_TAB", "UP"):
                error = ""  # clear error on focus change
                focus_pos = (focus_pos - 1) % len(focusable)
                focus = focusable[focus_pos]
            elif key == "ENTER":
                if f.field_type == "text":
                    # Enter from Name field submits
                    err = _validate_name(f.value.strip(), existing_profiles)
                    if err:
                        error = err
                    else:
                        return _build_result(fields)
                elif f.field_type == "button":
                    if f.label == "Create":
                        name = _get_field(fields, "Name").value.strip()
                        err = _validate_name(name, existing_profiles)
                        if err:
                            error = err
                        else:
                            return _build_result(fields)
            elif key == " ":
                if f.field_type == "checkbox":
                    f.value = not f.value
                    error = ""  # clear error on toggle
                elif f.field_type == "radio":
                    # Space cycles forward through radio options
                    opts = f.options or []
                    if opts:
                        idx = opts.index(f.value) if f.value in opts else -1
                        f.value = opts[(idx + 1) % len(opts)]
                        error = ""  # clear error on cycle
                    if f.label == "Advanced":
                        focusable = _focusable_indices(fields)
                        # Keep focus on Advanced
                        focus_pos = focusable.index(focus)
            elif key in ("LEFT", "RIGHT"):
                if f.field_type == "radio":
                    opts = f.options or []
                    if opts:
                        idx = opts.index(f.value) if f.value in opts else 0
                        step = 1 if key == "RIGHT" else -1
                        f.value = opts[(idx + step) % len(opts)]
                        error = ""  # clear error on cycle
                    if f.label == "Advanced":
                        focusable = _focusable_indices(fields)
                        # Keep focus on Advanced
                        focus_pos = focusable.index(focus)
            elif key == "BACKSPACE":
                if f.field_type == "text" and isinstance(f.value, str):
                    f.value = f.value[:-1]
                    error = ""  # clear error on edit
                    # Update computed config dir
                    _get_field(fields, "Config dir").value = f"~/.claudewheel/profiles/{f.value}"
            else:
                # Printable character on text field
                if f.field_type == "text" and len(key) == 1 and key.isprintable():
                    f.value = (f.value or "") + key
                    error = ""  # clear error on typing
                    _get_field(fields, "Config dir").value = f"~/.claudewheel/profiles/{f.value}"

            _render(term, fields, focus, error)
    finally:
        signal.signal(signal.SIGWINCH, prev_handler or signal.SIG_DFL)
        term.exit_raw()
        term.close()


def _load_shared_settings() -> dict:
    """Load shared-settings.json, falling back to canonical defaults if missing."""
    if SHARED_SETTINGS_FILE.exists():
        try:
            return json.loads(SHARED_SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return build_canonical_shared_settings(SCRIPTS_DIR)


def create_profile(result: WizardResult, cfg: ConfigManager) -> None:
    """Execute the profile creation based on wizard results."""
    config_dir = Path(result.config_dir).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    # Load shared settings once -- used for profileDefaults and hooks/disallowedTools
    shared = _load_shared_settings()

    # Build settings.json content
    settings: dict = {}
    if result.clone_from:
        # Copy from existing profile
        source_dir = PROFILES_DIR / result.clone_from
        source_settings = source_dir / "settings.json"
        if source_settings.exists():
            try:
                settings = json.loads(source_settings.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    else:
        # Use profileDefaults from shared-settings.json, otherwise minimal
        profile_defaults = shared.get("profileDefaults", {})
        if profile_defaults:
            settings = dict(profile_defaults)

    # Apply checkbox overrides on top
    if result.disable_recap:
        settings["awaySummaryEnabled"] = False
    if result.cleanup_10y:
        settings["cleanupPeriodDays"] = 3650
    if result.disable_memory:
        settings["autoMemoryEnabled"] = False
    if result.disable_attribution:
        settings["attribution"] = {"commit": "", "pr": ""}

    # Disable auto mode
    settings.setdefault("permissions", {})["disableAutoMode"] = "disable"
    # Record which tools claudewheel manages (enforcement is via --disallowedTools CLI flag in launch.py)
    settings.setdefault("claudewheel", {})["disallowedTools"] = shared.get("disallowedTools", DISALLOWED_TOOLS[:])

    # Wire hooks -- merge into existing hooks if cloned
    canonical_hooks = shared.get("hooks", {})
    if result.wire_hooks:
        existing_hooks = settings.get("hooks", {}).get("UserPromptSubmit", [])
        if existing_hooks:
            # Collect all commands already present across all entries
            all_cmds: set[str] = set()
            for entry in existing_hooks:
                for h in entry.get("hooks", []):
                    all_cmds.add(h.get("command", ""))
            # Append missing wanted hooks to the first entry's hook list
            wanted = canonical_hooks.get("UserPromptSubmit", [{}])[0].get("hooks", [])
            first_hooks = existing_hooks[0].setdefault("hooks", [])
            for h in wanted:
                if h["command"] not in all_cmds:
                    first_hooks.append(h)
            settings.setdefault("hooks", {})["UserPromptSubmit"] = existing_hooks
        else:
            settings["hooks"] = canonical_hooks

    # Write settings.json
    settings_path = config_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    # Symlink shared dirs
    if result.symlink_shared:
        for dirname in PROFILE_SHARED_DIRS:
            link = config_dir / dirname
            target = SHARED_DIR / dirname
            if link.exists() or link.is_symlink():
                continue
            target.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
        # Skills -> shared skills directory
        skills_link = config_dir / "skills"
        if SKILLS_DIR.is_dir() and not skills_link.exists() and not skills_link.is_symlink():
            skills_link.symlink_to(SKILLS_DIR)

    # Register in options.json: add value and metadata
    cfg.add_option("profile", result.name)
    cfg.set_option_metadata("profile", result.name, {"config_dir": result.config_dir})

    # Summary
    print(f"Created profile '{result.name}':")
    print(f"  Config dir:     {config_dir}")
    print(f"  Settings from:  {result.clone_from or 'defaults'}")
    print(f"  Hooks wired:    {result.wire_hooks}")
    print(f"  Shared symlinks:{result.symlink_shared}")
    print(f"  Recap disabled: {result.disable_recap}")
    print(f"  Cleanup 10y:    {result.cleanup_10y}")
    print(f"  Auto-memory:    {not result.disable_memory}")
    print(f"  Attribution:    {not result.disable_attribution}")


def _find_claude_binary() -> str | None:
    """Locate the Claude Code binary. Returns the path or None."""
    if CLAUDE_SYMLINK.exists() or CLAUDE_SYMLINK.is_symlink():
        resolved = CLAUDE_SYMLINK.resolve()
        if resolved.is_file():
            return str(resolved)
    found = shutil.which("claude")
    return found


def run_auth_flow(config_dir: str, profile_name: str,
                  skip_label: str = "Skip for now") -> str:
    """Prompt the user to set up authentication for a newly created profile.

    Presents a selection form with three choices: session login
    (browser-based), long-lived token, or skip. After picking a method,
    a second form asks which browser to open the auth URL in (or to
    suppress browser opening and copy the URL manually). The browser chosen
    in the last successful auth is remembered in state.json and pre-focused
    on the next run. Returns one of:

    - ``"authenticated"`` -- auth was set up successfully
    - ``"skip"`` -- the user explicitly chose to skip
    - ``"cancel"`` -- the user cancelled a form (Esc/Ctrl-C)
    - ``"failed"`` -- auth was attempted but did not complete

    This function is safe to call after create_profile() -- auth failure
    never prevents profile creation.
    """
    choice = run_selection(
        f"Authenticate profile '{profile_name}'",
        [
            ("session", "Session login (recommended)"),
            ("token", "Long-lived token"),
            ("skip", skip_label),
        ],
        use_alt_screen=False,
    )

    if choice == "skip":
        return "skip"
    if choice not in ("session", "token"):
        return "cancel"

    # Pre-focus the browser chosen in the last successful auth. If that
    # browser is gone from the options (uninstalled), run_selection falls
    # back to focusing the first option -- the form always appears.
    remembered = load_state_value(AUTH_BROWSER_KEY)
    if not isinstance(remembered, str):
        remembered = None

    browser = run_selection(
        "Choose browser",
        detect_browsers() + [("copy", "Copy URL instead")],
        use_alt_screen=False,
        initial_key=remembered,
    )
    if browser is None:
        return "cancel"

    if choice == "session":
        ok = _auth_session_login(config_dir, browser)
    else:
        ok = _auth_long_lived_token(config_dir, profile_name, browser)

    if ok:
        # Remember the working browser choice for the next auth flow.
        save_state_value(AUTH_BROWSER_KEY, browser)
    return "authenticated" if ok else "failed"


def _apply_browser_env(env: dict[str, str], browser: str) -> None:
    """Set BROWSER in ``env`` from the browser-form selection.

    ``browser`` is either a browser binary path or ``"copy"``. Claude Code
    spawns ``$BROWSER <url>``; ``BROWSER=false`` makes that fail, so claude
    falls back to printing the URL for manual copying.
    """
    if browser == "copy":
        env["BROWSER"] = "false"
        print("Browser opening suppressed -- copy the URL shown below.")
    else:
        env["BROWSER"] = browser


def _auth_session_login(config_dir: str, browser: str) -> bool:
    """Run ``claude auth login`` with CLAUDE_CONFIG_DIR and BROWSER set."""
    binary = _find_claude_binary()
    if binary is None:
        print("Error: Claude binary not found. Install it or add it to PATH.")
        return False

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    _apply_browser_env(env, browser)

    try:
        result = subprocess.run([binary, "auth", "login"], env=env)
    except OSError as e:
        print(f"Error running claude auth login: {e}")
        return False

    if result.returncode != 0:
        print("Auth login exited with an error.")
        return False

    credentials = Path(config_dir).expanduser() / ".credentials.json"
    if credentials.exists():
        print("Authentication successful.")
        return True

    print("Authentication did not complete (.credentials.json not found).")
    return False


def _auth_long_lived_token(config_dir: str, profile_name: str,
                           browser: str) -> bool:
    """Run ``claude setup-token``, then capture the token from user input."""
    binary = _find_claude_binary()
    if binary is None:
        print("Error: Claude binary not found. Install it or add it to PATH.")
        return False

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    _apply_browser_env(env, browser)

    try:
        result = subprocess.run([binary, "setup-token"], env=env)
    except OSError as e:
        print(f"Error running claude setup-token: {e}")
        return False

    if result.returncode != 0:
        print("setup-token exited with an error.")
        return False

    print()
    print("Copy the token shown above (it should start with sk-ant-).")
    print("Whitespace and linebreaks are removed automatically.")
    try:
        token = input("Paste the token that was displayed above: ")
    except (EOFError, KeyboardInterrupt):
        print()
        print("Token entry cancelled.")
        return False

    # Remove ALL whitespace, including linebreaks and spaces embedded by
    # line-wrapped terminal copies.
    token = "".join(token.split())

    if not token:
        print("No token provided.")
        return False

    if not token.startswith("sk-ant-"):
        print("Warning: token does not start with 'sk-ant-' -- saving anyway.")

    try:
        add_token(profile_name, token)
    except OSError as e:
        print(f"Error saving token: {e}")
        return False

    print("Token saved successfully.")
    return True
