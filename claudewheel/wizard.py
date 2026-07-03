"""Interactive form wizard for creating and configuring new profiles."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    CLAUDE_SYMLINK,
    PROFILES_DIR, PROFILE_SHARED_DIRS, SCRIPTS_DIR,
    SHARED_DIR, SHARED_SETTINGS_FILE, SKILLS_DIR,
)
from .config import ConfigManager
from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .discovery import detect_browsers
from .state import AUTH_BROWSER_KEY, load_state_value, save_state_value
from .tokens import add_token
from .ui import FormField, get_field, run_form, run_selection

# The 6 advanced checkboxes: (field key, display label). Keys match the
# WizardResult attribute names.
_CHECKBOX_DEFS = [
    ("wire_hooks", "Wire common hooks"),
    ("symlink_shared", "Symlink to shared store"),
    ("disable_recap", "Disable recap"),
    ("cleanup_10y", "10-year cleanup period"),
    ("disable_memory", "Disable auto-memory"),
    ("disable_attribution", "Disable Co-Authored-By"),
]

_DEFAULTS_TEMPLATE = "Defaults template"


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


def _build_fields(existing_profiles: list[str]) -> list[FormField]:
    """Build the ordered list of wizard form fields."""
    radio_opts = [_DEFAULTS_TEMPLATE] + existing_profiles

    def advanced_expanded(fields: list[FormField]) -> bool:
        return get_field(fields, "advanced").value == "Show advanced"

    def sync_config_dir(fields: list[FormField]) -> None:
        name = get_field(fields, "name").value or ""
        get_field(fields, "config_dir").value = \
            f"~/.claudewheel/profiles/{name}"

    fields = [
        FormField("name", "text", label="Name", value="",
                  on_change=sync_config_dir),
        FormField("config_dir", "readonly", label="Config dir",
                  value="~/.claudewheel/profiles/"),
        FormField("settings_source", "radio", label="Settings source",
                  value=radio_opts[0], options=radio_opts),
        FormField("advanced", "radio", label="Advanced",
                  value="Hide advanced",
                  options=["Hide advanced", "Show advanced"]),
    ]
    for key, label in _CHECKBOX_DEFS:
        fields.append(FormField(key, "checkbox", label=label, value=True,
                                visible=advanced_expanded))
    fields.append(FormField("create", "button", label="Create"))
    return fields


def _build_result(values: dict[str, object]) -> WizardResult:
    """Construct a WizardResult from submitted form values."""
    name = str(values["name"]).strip()
    source = values["settings_source"]
    clone = None if source == _DEFAULTS_TEMPLATE else source
    return WizardResult(
        name=name,
        config_dir=f"~/.claudewheel/profiles/{name}",
        clone_from=clone,
        wire_hooks=bool(values["wire_hooks"]),
        symlink_shared=bool(values["symlink_shared"]),
        disable_recap=bool(values["disable_recap"]),
        cleanup_10y=bool(values["cleanup_10y"]),
        disable_memory=bool(values["disable_memory"]),
        disable_attribution=bool(values["disable_attribution"]),
    )


def _cancelled_result() -> WizardResult:
    """Return a cancelled WizardResult with zero-value fields."""
    return WizardResult("", "", None, False, False, False, False, False,
                        False, cancelled=True)


def run_profile_wizard(existing_profiles: list[str], theme,
                       terminal) -> WizardResult:
    """Run the profile creation form and return the user's choices.

    The form renders on *terminal* with *theme* colors via the ui widget
    layer: fullscreen, borrowed when the terminal is already raw.
    """
    fields = _build_fields(existing_profiles)

    def validate(fields: list[FormField]) -> str | None:
        name = str(get_field(fields, "name").value or "").strip()
        return _validate_name(name, existing_profiles)

    values = run_form("New Profile", fields, theme, terminal,
                      validate=validate)
    if values is None:
        return _cancelled_result()
    return _build_result(values)


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


def run_auth_flow(config_dir: str, profile_name: str, theme, terminal,
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
    never prevents profile creation. The forms render on *terminal* with
    *theme* colors; a terminal that is already raw is borrowed (the forms
    render as pages in the existing screen).
    """
    choice = run_selection(
        f"Authenticate profile '{profile_name}'",
        [
            ("session", "Session login (recommended)"),
            ("token", "Long-lived token"),
            ("skip", skip_label),
        ],
        theme, terminal,
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
        theme, terminal,
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
