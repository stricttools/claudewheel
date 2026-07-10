"""Interactive form wizard for creating and configuring new profiles."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import auth
from .appdata import OptionsFile, StateFile
from .constants import (
    CLAUDE_SYMLINK,
    OPTIONS_FILE, PROFILES_DIR, SCRIPTS_DIR, STATE_FILE,
    SHARED_DIR, SHARED_SETTINGS_FILE, SKILLS_DIR, TOKENS_FILE,
)
from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .discovery import detect_browsers
from .fsutil import write_json_atomic
from .patch_profiles import merge_hooks
from .profile_store import ProfileStore
from .pty_runner import run_under_pty
from .shared_store import SharedStore
from .state import AUTH_BROWSER_KEY, load_state_value, save_state_value
from .tokens import TokenStore, add_token, store_tier
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


def _set_onboarding_flag(config_dir: str) -> None:
    """Merge ``hasCompletedOnboarding: true`` into ``{config_dir}/.claude.json``.

    Claude Code gates on this flag in interactive mode -- it is normally
    set by CC's own OAuth success handler, but claudewheel bypasses that
    when injecting tokens via CLAUDE_CODE_OAUTH_TOKEN. Read-merge-write
    preserves any other metadata CC has already written (machineID,
    cachedGrowthBookFeatures, etc.). Corrupt or missing files are handled
    gracefully. If the config directory itself doesn't exist, this is a
    no-op (nothing to write to).
    """
    expanded = Path(config_dir).expanduser()
    if not expanded.is_dir():
        return
    path = expanded / ".claude.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data["hasCompletedOnboarding"] = True
    write_json_atomic(path, data)


def create_profile(result: WizardResult) -> list[str]:
    """Execute the profile creation based on wizard results.

    Assembles the final settings dict (clone/defaults/checkbox overrides/hooks)
    then delegates all durable mechanics to :meth:`ProfileStore.create` --
    atomic settings.json write, onboarding flag, shared-store symlinks, and
    options.json (pinned) registration. No config_dir metadata is persisted.

    Returns the summary lines describing what was created; presentation is
    the caller's job (the TUI shows a fullscreen page, the CLI prints them).
    """
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

    # Wire hooks -- when the profile already has a hooks section (e.g. cloned),
    # additively merge ALL canonical events/matchers (reusing the matcher-based,
    # de-duplicated merge that patch-profiles uses) so every wiring lands, not
    # just UserPromptSubmit. With no hooks section, copy the canonical hooks.
    canonical_hooks = shared.get("hooks", {})
    if result.wire_hooks:
        existing_hooks = settings.get("hooks")
        if existing_hooks:
            merge_hooks(existing_hooks, canonical_hooks)
            settings["hooks"] = existing_hooks
        else:
            settings["hooks"] = canonical_hooks

    # Land the finished settings durably. ProfileStore.create performs the
    # atomic settings.json write, sets the onboarding flag, symlinks the six
    # shared-store subdirs (+ skills), and registers the profile in
    # options.json (pinned). The old non-atomic write_text lived here.
    store = ProfileStore(
        PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE),
        shared=SharedStore(SHARED_DIR, SKILLS_DIR),
        options=OptionsFile(OPTIONS_FILE),
        state=StateFile(STATE_FILE),
    )
    store.create(result.name, settings, symlink_shared=result.symlink_shared)
    config_dir = store.path_for(result.name)

    return [
        f"Created profile '{result.name}':",
        f"  Config dir:     {config_dir}",
        f"  Settings from:  {result.clone_from or 'defaults'}",
        f"  Hooks wired:    {result.wire_hooks}",
        f"  Shared symlinks:{result.symlink_shared}",
        f"  Recap disabled: {result.disable_recap}",
        f"  Cleanup 10y:    {result.cleanup_10y}",
        f"  Auto-memory:    {not result.disable_memory}",
        f"  Attribution:    {not result.disable_attribution}",
    ]


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

    - ``"authenticated"`` -- auth was set up successfully (tokens: validated
      against the API before saving)
    - ``"unverified"`` -- a token was saved WITHOUT validation (the probe
      was unreachable or inconclusive and the user explicitly chose to save)
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
            ("paste", "Paste token directly"),
            ("skip", skip_label),
        ],
        theme, terminal,
    )

    if choice == "skip":
        return "skip"
    if choice not in ("session", "token", "paste"):
        return "cancel"

    if choice == "paste":
        outcome = _auth_paste_token(config_dir, profile_name, theme, terminal)
    else:
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
            initial_key=remembered,
        )
        if browser is None:
            return "cancel"

        if choice == "session":
            ok = _auth_session_login(config_dir, profile_name, browser, terminal)
            outcome = "authenticated" if ok else "failed"
        else:
            outcome = _auth_long_lived_token(config_dir, profile_name, browser,
                                             theme, terminal)

    if outcome in ("authenticated", "unverified"):
        # Remember the working browser choice for the next auth flow. An
        # unverified save still means the browser step itself worked.
        # The paste path has no browser step, so skip persistence.
        if choice != "paste":
            save_state_value(AUTH_BROWSER_KEY, browser)
        # Mark onboarding complete so CC skips the login screen
        _set_onboarding_flag(config_dir)
    return outcome


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


def _capture_tier_from_credentials(credentials: Path, profile_name: str) -> None:
    """Read rateLimitTier/subscriptionType from .credentials.json and store in tokens.json.

    Best-effort: silently skips if the credentials file cannot be parsed or
    the expected fields are absent (older Claude Code versions omit them).
    """
    try:
        data = json.loads(credentials.read_text())
    except (json.JSONDecodeError, OSError):
        return
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return
    tier = oauth.get("rateLimitTier")
    subscription = oauth.get("subscriptionType")
    if not tier and not subscription:
        return
    try:
        store_tier(profile_name, tier=tier, subscription=subscription)
    except OSError:
        pass  # best-effort: don't fail auth flow over tier storage


def _auth_session_login(config_dir: str, profile_name: str,
                        browser: str, terminal) -> bool:
    """Run ``claude auth login`` with CLAUDE_CONFIG_DIR and BROWSER set.

    The whole body runs inside ``terminal.cooked()`` so the claude subprocess
    and all prints see a real cooked terminal; raw mode (and the alt screen,
    if any) is restored on exit.
    """
    with terminal.cooked():
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
            # Extract rate-limit tier metadata if present
            _capture_tier_from_credentials(credentials, profile_name)
            print("Authentication successful.")
            return True

        print("Authentication did not complete (.credentials.json not found).")
        return False


_PASTE_PROMPT = "Paste the token manually, or press Enter to abort: "


def _read_pasted_token(prompt: str) -> str | None:
    """Read a manually pasted token from input().

    Removes ALL whitespace, including linebreaks and spaces embedded by
    line-wrapped terminal copies. Returns None on EOF/Ctrl-C; an empty
    string when nothing (or only whitespace) was entered.
    """
    try:
        raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return "".join(raw.split())


def _capture_setup_token(config_dir: str, browser: str) -> str | None:
    """Run ``claude setup-token`` under a PTY and scrape the token.

    Must run inside a cooked window: run_under_pty proxies the real terminal
    (it sets its own raw mode internally and restores it). The user interacts
    with setup-token normally while ALL output is captured; the token is then
    extracted from the capture -- no paste needed. Returns the token, or None
    on failure.

    If extraction fails, that is a hard, explicit situation: the user is told
    the scrape failed and offered a manual paste as a clearly labeled recovery
    step -- not a silent fallback. Empty input aborts.
    """
    binary = _find_claude_binary()
    if binary is None:
        print("Error: Claude binary not found. Install it or add it to PATH.")
        return None

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    _apply_browser_env(env, browser)

    try:
        exit_code, captured = run_under_pty([binary, "setup-token"], env)
    except (OSError, RuntimeError) as e:
        print(f"Error running claude setup-token: {e}")
        return None

    if exit_code != 0:
        print("setup-token exited with an error.")
        return None

    token = auth.extract_token(captured)
    if token is not None:
        return token

    print()
    print("Error: could not extract the token from setup-token's output.")
    token = _read_pasted_token(_PASTE_PROMPT)
    if not token:
        print("No token provided.")
        return None
    return token


def _save_token(profile_name: str, token: str) -> bool:
    """Write the token via add_token; print and return False on OSError."""
    try:
        add_token(profile_name, token)
    except OSError as e:
        print(f"Error saving token: {e}")
        return False
    return True


def _auth_paste_token(config_dir: str, profile_name: str,
                      theme, terminal) -> str:
    """Prompt the user to paste a token directly, validate it, save it.

    Skips the browser selection and PTY capture entirely -- the user pastes
    a token they already have. Validation and outcome handling mirror
    ``_auth_long_lived_token``. Returns one of:

    - ``"authenticated"`` -- the probe returned VALID and the token was saved
    - ``"unverified"`` -- the probe was UNREACHABLE/INDETERMINATE and the
      user explicitly chose to save the unvalidated token
    - ``"failed"`` -- anything else (cancelled, rejected, save error)
    """
    with terminal.cooked():
        token = _read_pasted_token("Paste your API token: ")
        if not token:
            return "cancel"

        status = auth.validate_token(token)
        if status == auth.INVALID:
            print("Error: the token was rejected by the API (401).")
            token = _read_pasted_token("Paste the corrected token, or press Enter to abort: ")
            if not token:
                print("No token provided.")
                return "failed"
            status = auth.validate_token(token)
            if status == auth.INVALID:
                print("Error: the token was rejected by the API (401) again.")
                return "failed"

        if status == auth.VALID:
            if not _save_token(profile_name, token):
                return "failed"
            print("Token validated and saved successfully.")
            return "authenticated"

    # UNREACHABLE or INDETERMINATE: the token cannot be verified right now.
    # The cooked window is closed, so the choice form renders borrowed in
    # the caller's session (raw terminal) like the other auth forms.
    reason = ("API unreachable" if status == auth.UNREACHABLE
              else "validation inconclusive")
    choice = run_selection(
        f"Token could not be validated ({reason})",
        [
            ("save", "Save unvalidated"),
            ("abort", "Abort"),
        ],
        theme, terminal,
    )
    if choice != "save":
        return "failed"

    with terminal.cooked():
        if not _save_token(profile_name, token):
            return "failed"
        print("Token saved WITHOUT validation.")
    return "unverified"


def _auth_long_lived_token(config_dir: str, profile_name: str, browser: str,
                           theme, terminal) -> str:
    """Run ``claude setup-token``, scrape the token, validate it, save it.

    Every token (scraped or manually recovered) is probed against the API
    BEFORE being saved. Returns one of:

    - ``"authenticated"`` -- the probe returned VALID and the token was saved
    - ``"unverified"`` -- the probe was UNREACHABLE/INDETERMINATE and the
      user explicitly chose to save the unvalidated token
    - ``"failed"`` -- anything else. A token the API rejects (401) is NEVER
      saved: one manual re-paste is offered (the scrape may have picked a
      stale frame), then the flow fails hard.
    """
    with terminal.cooked():
        token = _capture_setup_token(config_dir, browser)
        if token is None:
            return "failed"

        status = auth.validate_token(token)
        if status == auth.INVALID:
            print("Error: the token was rejected by the API (401).")
            print("The captured token may be stale or truncated.")
            token = _read_pasted_token(_PASTE_PROMPT)
            if not token:
                print("No token provided.")
                return "failed"
            status = auth.validate_token(token)
            if status == auth.INVALID:
                print("Error: the token was rejected by the API (401) again.")
                return "failed"

        if status == auth.VALID:
            if not _save_token(profile_name, token):
                return "failed"
            print("Token validated and saved successfully.")
            return "authenticated"

    # UNREACHABLE or INDETERMINATE: the token cannot be verified right now.
    # The cooked window is closed, so the choice form renders borrowed in
    # the caller's session (raw terminal) like the other auth forms.
    reason = ("API unreachable" if status == auth.UNREACHABLE
              else "validation inconclusive")
    choice = run_selection(
        f"Token could not be validated ({reason})",
        [
            ("save", "Save unvalidated"),
            ("abort", "Abort"),
        ],
        theme, terminal,
    )
    if choice != "save":
        return "failed"

    with terminal.cooked():
        if not _save_token(profile_name, token):
            return "failed"
        print("Token saved WITHOUT validation.")
    return "unverified"
