"""Gather and format a detailed inspection report for a single profile."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .appdata import OptionsFile
from .constants import OPTIONS_FILE, PROFILES_DIR, TOKENS_FILE
from .discovery import classify_shared_dirs
from .profile_store import ProfileStore
from .tokens import TokenExpiry, TokenStore, TokenStoreError, parse_entry


@dataclass
class ProfileReport:
    """Everything gather_profile_info() learns about one profile."""

    name: str
    config_dir: Path
    exists: bool                       # config_dir is a directory on disk
    registered: bool                   # in options.json profile values
    pinned: bool                       # in options.json profile pinned list
    has_credentials: bool              # .credentials.json present
    has_token: bool                    # entry in tokens.json
    token_expiry: TokenExpiry | None   # only when has_token
    has_auth_shadow: bool = False      # claudeAiOauth in creds AND valid token
    rate_limit_tier: str | None = None       # from tokens.json entry
    subscription_type: str | None = None     # from tokens.json entry
    shared_dirs: dict[str, str] = field(default_factory=dict)
    danger: bool = False               # any shared entry is a real dir/file
    settings_found: bool = False
    permission_counts: dict[str, int] = field(default_factory=dict)
    away_summary_enabled: bool | None = None
    cleanup_period_days: int | None = None
    auto_memory_enabled: bool | None = None
    active_sessions: int = 0
    disk_usage_bytes: int = 0


def _profile_store() -> ProfileStore:
    """Build a path-injected ProfileStore from this module's path constants.

    Interim call-time construction until a later phase threads a Workspace
    through; patching the module constants in tests still redirects it.
    """
    return ProfileStore(
        PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE)
    )


def _read_options_registration(name: str) -> tuple[bool, bool]:
    """Return (registered, pinned) for *name* from options.json."""
    options = OptionsFile(OPTIONS_FILE).load({})
    profile_sec = options.get("profile", {})
    return (
        name in profile_sec.get("values", []),
        name in profile_sec.get("pinned", []),
    )


def _read_token_state(name: str) -> tuple[bool, TokenExpiry | None,
                                          str | None, str | None]:
    """Return (has_token, expiry, tier, subscription) for *name* from tokens.json.

    A corrupt tokens.json raises :class:`TokenStoreError` (the hard-error
    contract): profile inspection is a CLI command, so token corruption is a
    workspace-integrity problem the operator must fix, never a silent skip.
    """
    store = TokenStore(TOKENS_FILE)
    tokens = store.load()
    if name not in tokens:
        return False, None, None, None
    entry = tokens[name]
    tier = entry.get("rateLimitTier") if isinstance(entry, dict) else None
    subscription = entry.get("subscriptionType") if isinstance(entry, dict) else None
    return True, store.expiry_for(name), tier, subscription


def _count_active_sessions(config_dir: Path) -> int:
    """Count live sessions by scanning <config_dir>/sessions/*.pid.

    The .pid files are written by Claude Code itself (an external
    dependency, not claudewheel). A session counts as active when its
    recorded PID is a live process (os.kill(pid, 0) succeeds).
    """
    sessions_dir = config_dir / "sessions"
    if not sessions_dir.is_dir():
        return 0
    count = 0
    for entry in sessions_dir.iterdir():
        if entry.suffix != ".pid" or not entry.is_file():
            continue
        try:
            pid = int(entry.read_text().strip())
            os.kill(pid, 0)
            count += 1
        except (ValueError, OSError):
            continue  # stale PID file or process gone
    return count


def _disk_usage(config_dir: Path) -> int:
    """Sum file sizes under *config_dir*, never following symlinks.

    Uses os.walk(followlinks=False) so symlinked dirs (the shared-store
    links inside profile dirs) are not descended into, and skips symlinked
    files so only data truly owned by the profile dir is counted.
    """
    total = 0
    for root, _dirs, files in os.walk(config_dir, followlinks=False):
        for fname in files:
            fpath = os.path.join(root, fname)
            if os.path.islink(fpath):
                continue
            try:
                total += os.lstat(fpath).st_size
            except OSError:
                continue
    return total


def _read_settings(config_dir: Path) -> tuple[bool, dict[str, int],
                                              bool | None, int | None,
                                              bool | None]:
    """Summarize settings.json; tolerate a missing/corrupt file and keys."""
    try:
        settings = json.loads((config_dir / "settings.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False, {}, None, None, None
    perms = settings.get("permissions", {})
    counts = {
        cat: len(perms.get(cat, []) or [])
        for cat in ("allow", "deny", "ask")
    }
    return (
        True,
        counts,
        settings.get("awaySummaryEnabled"),
        settings.get("cleanupPeriodDays"),
        settings.get("autoMemoryEnabled"),
    )


def detect_auth_shadow(name: str) -> bool:
    """Return True if profile has session credentials shadowing a long-lived token.

    Conditions (all must be true):
    - A valid token entry exists in tokens.json for *name*
    - .credentials.json exists in the profile's config dir
    - .credentials.json contains a "claudeAiOauth" key

    Lightweight check usable from both gather_profile_info and health checks.
    """
    # Token read tolerates a corrupt tokens.json (returns False). This helper is
    # shared with health checks, which carry the tokens-corruption carve-out;
    # gather_profile_info calls _read_token_state first, so the CLI inspection
    # path still hard-errors on corrupt tokens before ever reaching here.
    try:
        tokens = TokenStore(TOKENS_FILE).load()
    except TokenStoreError:
        return False
    if parse_entry(tokens.get(name)) is None:
        return False

    # Check .credentials.json for claudeAiOauth
    config_dir = _profile_store().path_for(name)
    creds_path = config_dir / ".credentials.json"
    if not creds_path.exists():
        return False
    try:
        creds = json.loads(creds_path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    return "claudeAiOauth" in creds


def gather_profile_info(name: str) -> ProfileReport:
    """Assemble a full ProfileReport for *name*.

    Never raises for unknown profiles: callers check report.exists /
    report.registered / report.has_token to decide how to present one.
    """
    config_dir = _profile_store().path_for(name)
    exists = config_dir.is_dir()
    registered, pinned = _read_options_registration(name)
    has_token, token_expiry, rate_limit_tier, subscription_type = _read_token_state(name)
    has_credentials = (config_dir / ".credentials.json").exists()

    shared_dirs: dict[str, str] = {}
    active_sessions = 0
    disk_usage_bytes = 0
    settings_found, permission_counts = False, {}
    away, cleanup, auto_memory = None, None, None
    if exists:
        shared_dirs = classify_shared_dirs(config_dir)
        active_sessions = _count_active_sessions(config_dir)
        disk_usage_bytes = _disk_usage(config_dir)
        (settings_found, permission_counts,
         away, cleanup, auto_memory) = _read_settings(config_dir)

    has_auth_shadow = detect_auth_shadow(name)

    return ProfileReport(
        name=name,
        config_dir=config_dir,
        exists=exists,
        registered=registered,
        pinned=pinned,
        has_credentials=has_credentials,
        has_token=has_token,
        token_expiry=token_expiry,
        has_auth_shadow=has_auth_shadow,
        rate_limit_tier=rate_limit_tier,
        subscription_type=subscription_type,
        shared_dirs=shared_dirs,
        danger=any(s == "real-dir" for s in shared_dirs.values()),
        settings_found=settings_found,
        permission_counts=permission_counts,
        away_summary_enabled=away,
        cleanup_period_days=cleanup,
        auto_memory_enabled=auto_memory,
        active_sessions=active_sessions,
        disk_usage_bytes=disk_usage_bytes,
    )


def _format_size(size: int) -> str:
    """Human-readable byte size (B / KB / MB / GB)."""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_report(report: ProfileReport) -> list[str]:
    """Render a ProfileReport as plain display lines (TUI page and CLI)."""
    lines = [
        f"Profile: {report.name}",
        f"Config dir: {report.config_dir}" + ("" if report.exists else " (missing)"),
    ]

    if report.registered and report.pinned:
        lines.append("Registered: yes (pinned)")
    elif report.registered:
        lines.append("Registered: yes")
    elif report.pinned:
        lines.append("Registered: pinned only")
    else:
        lines.append("Registered: no")

    lines.append(f"Credentials file: {'present' if report.has_credentials else 'missing'}")

    if report.has_token and report.token_expiry is not None:
        exp = report.token_expiry
        created = exp.created.isoformat() if exp.created else "unknown"
        expires = exp.expires.isoformat() if exp.expires else "unknown"
        lines.append(f"Token: present (created {created}, expires {expires}, "
                     f"{exp.remaining_days:.0f} days left)")
    else:
        lines.append("Token: none")

    if report.has_auth_shadow:
        lines.append("Auth shadow: yes (session credentials override token)")

    if report.rate_limit_tier:
        sub = f" ({report.subscription_type})" if report.subscription_type else ""
        lines.append(f"Tier: {report.rate_limit_tier}{sub}")
    else:
        lines.append("Tier: unknown")

    if report.shared_dirs:
        intact = sum(1 for s in report.shared_dirs.values() if s == "intact")
        lines.append(f"Shared dirs: {intact}/{len(report.shared_dirs)} intact")
        for dname, state in sorted(report.shared_dirs.items()):
            if state != "intact":
                lines.append(f"  {dname}: {state}")
        if report.danger:
            lines.append("  DANGER: real data at a shared-dir name (not a symlink)")

    if report.settings_found:
        c = report.permission_counts
        lines.append(f"Permissions: {c.get('allow', 0)} allow, "
                     f"{c.get('deny', 0)} deny, {c.get('ask', 0)} ask")
        lines.append(f"awaySummaryEnabled: {report.away_summary_enabled}")
        lines.append(f"cleanupPeriodDays: {report.cleanup_period_days}")
        lines.append(f"autoMemoryEnabled: {report.auto_memory_enabled}")
    else:
        lines.append("Settings: no settings.json")

    lines.append(f"Active sessions: {report.active_sessions}")
    lines.append(f"Disk usage: {_format_size(report.disk_usage_bytes)}")
    return lines
