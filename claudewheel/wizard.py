"""Interactive form wizard for creating and configuring new profiles."""

from __future__ import annotations

import json
import signal
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    CLEAR_SCREEN, CLEAR_LINE, RESET, BOLD, DIM,
    PROFILES_DIR, PROFILE_SHARED_DIRS, SCRIPTS_DIR,
    SHARED_DIR, SHARED_SETTINGS_FILE, SKILLS_DIR,
    move_to, fg_rgb,
)
from .config import ConfigManager
from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .terminal import Terminal

# Color constants
_ACCENT = (107, 138, 255)  # #6B8AFF
_DIM_CLR = (136, 136, 136)  # #888888


@dataclass
class WizardField:
    """Defines a single input field in the profile creation wizard."""

    label: str
    field_type: str  # "text", "radio", "checkbox", "readonly", "button"
    value: str | bool | None = None
    options: list[str] | None = None  # for radio
    enabled: bool = True


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


def _build_fields(existing_profiles: list[str]) -> list[WizardField]:
    """Build the ordered list of wizard fields."""
    radio_opts = ["Defaults template"] + existing_profiles
    return [
        WizardField("Name", "text", value=""),
        WizardField("Config dir", "readonly", value="~/.claudewheel/profiles/"),
        WizardField("Settings source", "radio", value=radio_opts[0], options=radio_opts),
        WizardField("Wire common hooks", "checkbox", value=True),
        WizardField("Symlink to shared store", "checkbox", value=True),
        WizardField("Disable recap", "checkbox", value=True),
        WizardField("10-year cleanup period", "checkbox", value=True),
        WizardField("Disable auto-memory", "checkbox", value=True),
        WizardField("Disable Co-Authored-By", "checkbox", value=True),
        WizardField("Create", "button"),
        WizardField("Cancel", "button"),
    ]


def _render(term: Terminal, fields: list[WizardField], focus: int,
            flash: str = "") -> None:
    """Render the full wizard form centered in the terminal."""
    rows, cols = term.get_size()
    # Total lines: 1 title + 1 blank + len(fields) + 1 blank line before buttons
    # Buttons are last 2 fields, rendered on the same line
    form_lines = len(fields) - 1  # -1 because two buttons share a line
    total_height = 1 + 1 + form_lines
    start_row = max(1, (rows - total_height) // 2)
    buf: list[str] = [CLEAR_SCREEN]

    # Title
    title = "New Profile"
    title_col = max(1, (cols - len(title)) // 2)
    buf.append(move_to(start_row, title_col) + BOLD + fg_rgb(*_ACCENT) + title + RESET)

    row = start_row + 2  # skip blank line after title
    for i, f in enumerate(fields):
        # Buttons (Create + Cancel) share one line
        if f.field_type == "button" and f.label == "Cancel":
            continue  # rendered alongside Create

        focused = (i == focus)
        color = fg_rgb(*_ACCENT) if focused else fg_rgb(*_DIM_CLR)
        style = BOLD + color if focused else color

        if f.field_type == "button" and f.label == "Create":
            # Render both buttons on one line
            cancel_idx = i + 1
            cancel_focused = (focus == cancel_idx)
            create_style = BOLD + fg_rgb(*_ACCENT) if focused else fg_rgb(*_DIM_CLR)
            cancel_style = (BOLD + fg_rgb(*_ACCENT) if cancel_focused
                            else fg_rgb(*_DIM_CLR))
            line = (f"{create_style}[ Create ]{RESET}"
                    f"    "
                    f"{cancel_style}[ Cancel ]{RESET}")
            col = max(1, (cols - 23) // 2)  # 23 = len("[ Create ]    [ Cancel ]")
            buf.append(move_to(row, col) + CLEAR_LINE + line)
            row += 1
            continue

        # Label + value
        if f.field_type == "text":
            cursor = "_" if focused else ""
            val = f"{f.value}{cursor}"
            line = f"{style}{f.label}: {RESET}{val}"
        elif f.field_type == "readonly":
            line = f"{style}{f.label}: {RESET}{DIM}{f.value}{RESET}"
        elif f.field_type == "radio":
            parts: list[str] = []
            for opt in (f.options or []):
                marker = "(*)" if opt == f.value else "( )"
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

    # Flash message below form
    if flash:
        flash_col = max(1, (cols - len(flash)) // 2)
        buf.append(move_to(row + 1, flash_col) + CLEAR_LINE
                   + BOLD + fg_rgb(255, 100, 100) + flash + RESET)

    term.write("".join(buf))


def run_profile_wizard(existing_profiles: list[str]) -> WizardResult:
    """Run the form TUI and return the user's choices."""
    term = Terminal()
    fields = _build_fields(existing_profiles)
    focus = 0  # start on Name field

    term.enter_raw()

    def on_resize(signum, frame):
        term.rows, term.cols = term.get_size()
        _render(term, fields, focus)

    prev_handler = signal.signal(signal.SIGWINCH, on_resize)

    try:
        _render(term, fields, focus)
        while True:
            try:
                key = term.read_key()
            except KeyboardInterrupt:
                return WizardResult("", "", None, False, False, False, False, False,
                                    False, cancelled=True)

            f = fields[focus]

            if key == "ESC" or key == "CTRL_C":
                return WizardResult("", "", None, False, False, False, False, False,
                                    False, cancelled=True)

            if key in ("UP", "DOWN", "TAB"):
                step = -1 if key == "UP" else 1
                focus = (focus + step) % len(fields)
            elif key == "ENTER":
                if f.field_type == "button":
                    if f.label == "Cancel":
                        return WizardResult("", "", None, False, False, False, False,
                                            False, cancelled=True)
                    if f.label == "Create":
                        name = _get_field(fields, "Name").value.strip()
                        if not name:
                            _render(term, fields, focus, flash="Name cannot be empty")
                            continue
                        import re
                        if not re.match(r'^[a-z0-9][a-z0-9-]*$', name):
                            _render(term, fields, focus, flash="Use lowercase letters, digits, hyphens only")
                            continue
                        if name == "default":
                            _render(term, fields, focus, flash=f"'{name}' is a reserved name")
                            continue
                        config_dir = (PROFILES_DIR / name).expanduser()
                        if config_dir.exists():
                            _render(term, fields, focus, flash=f"~/.claudewheel/profiles/{name} already exists")
                            continue
                        if name in existing_profiles:
                            _render(term, fields, focus, flash=f"Profile '{name}' already registered")
                            continue
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
            elif key == " ":
                if f.field_type == "checkbox":
                    f.value = not f.value
                elif f.field_type == "radio":
                    # Space cycles forward through radio options
                    opts = f.options or []
                    if opts:
                        idx = opts.index(f.value) if f.value in opts else -1
                        f.value = opts[(idx + 1) % len(opts)]
            elif key in ("LEFT", "RIGHT"):
                if f.field_type == "radio":
                    opts = f.options or []
                    if opts:
                        idx = opts.index(f.value) if f.value in opts else 0
                        step = 1 if key == "RIGHT" else -1
                        f.value = opts[(idx + step) % len(opts)]
            elif key == "BACKSPACE":
                if f.field_type == "text" and isinstance(f.value, str):
                    f.value = f.value[:-1]
                    # Update computed config dir
                    _get_field(fields, "Config dir").value = f"~/.claudewheel/profiles/{f.value}"
            else:
                # Printable character on text field
                if f.field_type == "text" and len(key) == 1 and key.isprintable():
                    f.value = (f.value or "") + key
                    _get_field(fields, "Config dir").value = f"~/.claudewheel/profiles/{f.value}"

            _render(term, fields, focus)
    finally:
        signal.signal(signal.SIGWINCH, prev_handler or signal.SIG_DFL)
        term.exit_raw()


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
        shared = _load_shared_settings()
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

    # Load canonical shared settings
    shared = _load_shared_settings()

    # Disable auto mode
    settings.setdefault("permissions", {})["disableAutoMode"] = "disable"
    # Record which tools claudewheel manages (enforcement is via --disallowedTools CLI flag in launch.py)
    settings.setdefault("claudewheel", {})["disallowedTools"] = shared.get("disallowedTools", DISALLOWED_TOOLS[:])

    # Wire hooks — merge into existing hooks if cloned
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
    print()
    print("  To set up long-lived auth, run:")
    print(f"    CLAUDE_CONFIG_DIR={result.config_dir} claude setup-token")
