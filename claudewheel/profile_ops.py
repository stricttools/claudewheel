"""Delete profiles and clean up their dirs, tokens, and options."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field

from .constants import OPTIONS_FILE, PROFILES_DIR, TOKENS_FILE
from .discovery import classify_shared_dirs
from .fsutil import write_json_atomic, write_json_atomic_secret
from .profile_info import config_dir_for
from .state import load_state_value, save_state_value
from .tokens import parse_entry


@dataclass
class FixAuthResult:
    """Outcome of fix_auth_shadow(): success or a reason for no-op/failure.

    ok: True when the shadow was removed, False otherwise.
    reason: None on success; "no-token" / "no-shadow" / "unreadable-creds" on failure.
    tier_saved: rateLimitTier value preserved into tokens.json, or None.
    subscription_saved: subscriptionType value preserved into tokens.json, or None.
    """

    ok: bool
    reason: str | None = None
    tier_saved: str | None = None
    subscription_saved: str | None = None


def fix_auth_shadow(name: str) -> FixAuthResult:
    """Remove session credentials (claudeAiOauth) that shadow a long-lived token.

    Reads the profile's .credentials.json, strips the claudeAiOauth key, and
    preserves any tier/subscription metadata into tokens.json. Zero printing,
    zero sys.exit -- returns a FixAuthResult describing what happened.
    """
    config_dir = config_dir_for(name)

    # 1. Check tokens.json has a valid entry
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        tokens = {}
    if parse_entry(tokens.get(name)) is None:
        return FixAuthResult(ok=False, reason="no-token")

    # 2. Read .credentials.json
    creds_path = config_dir / ".credentials.json"
    if not creds_path.exists():
        return FixAuthResult(ok=False, reason="no-shadow")
    try:
        creds = json.loads(creds_path.read_text())
    except (json.JSONDecodeError, OSError):
        return FixAuthResult(ok=False, reason="unreadable-creds")

    if "claudeAiOauth" not in creds:
        return FixAuthResult(ok=False, reason="no-shadow")

    # 3. Extract tier fields before stripping
    oauth_block = creds["claudeAiOauth"]
    tier = oauth_block.get("rateLimitTier") if isinstance(oauth_block, dict) else None
    sub_type = oauth_block.get("subscriptionType") if isinstance(oauth_block, dict) else None

    if tier or sub_type:
        # Save tier data into tokens.json entry
        entry = tokens.get(name)
        if isinstance(entry, str):
            entry = {"token": entry}
        elif not isinstance(entry, dict):
            entry = {}
        if tier:
            entry["rateLimitTier"] = tier
        if sub_type:
            entry["subscriptionType"] = sub_type
        tokens[name] = entry
        write_json_atomic_secret(TOKENS_FILE, tokens)

    # 4. Strip claudeAiOauth and write back
    creds.pop("claudeAiOauth", None)
    write_json_atomic_secret(creds_path, creds)

    return FixAuthResult(
        ok=True,
        tier_saved=tier,
        subscription_saved=sub_type,
    )


@dataclass
class DeleteResult:
    """Outcome of delete_profile_core(): success data or a refusal reason.

    refusal_reason is None on success, else one of:
    - "not-found": not registered in options.json and no dir on disk
    - "default-profile": the built-in ~/.claude, never deletable
    - "running": active sessions detected (and running check not skipped)
    - "data-destruction": real data at shared-dir names (see at_risk_dirs)
    """

    ok: bool
    refusal_reason: str | None = None
    at_risk_dirs: list[str] = field(default_factory=list)
    known_profiles: list[str] = field(default_factory=list)
    removed_symlinks: int = 0
    removed_real: int = 0
    removed_from_options: bool = False
    removed_from_tokens: bool = False
    last_config_purged: bool = False


def _is_profile_running(name: str) -> bool:
    """Check if a profile has active sessions by scanning its sessions/ dir for PID files."""
    profile_dir = PROFILES_DIR / name
    sessions_dir = profile_dir / "sessions"
    if not sessions_dir.is_dir():
        return False
    for entry in sessions_dir.iterdir():
        if entry.suffix == ".pid" and entry.is_file():
            try:
                pid = int(entry.read_text().strip())
                # Check if process is alive (signal 0 = existence check)
                os.kill(pid, 0)
                return True
            except (ValueError, OSError):
                # Stale PID file or process gone -- not running
                continue
    return False


def _remove_profile_dir(name: str) -> tuple[int, int]:
    """Remove ~/.claudewheel/profiles/<name>/, handling symlinks safely.

    Returns (removed_symlinks, removed_real) counts.
    """
    profile_dir = PROFILES_DIR / name
    if not profile_dir.is_dir():
        return 0, 0

    removed_symlinks = 0
    removed_real = 0

    # Walk top-level entries: remove symlinks without following, remove real
    # files/dirs normally.  We process children first, then rmdir the parent.
    for child in list(profile_dir.iterdir()):
        if child.is_symlink():
            child.unlink()
            removed_symlinks += 1
        elif child.is_dir():
            shutil.rmtree(child)
            removed_real += 1
        else:
            child.unlink()
            removed_real += 1

    # Remove the now-empty profile dir itself
    profile_dir.rmdir()
    return removed_symlinks, removed_real


def _remove_from_options(name: str) -> bool:
    """Remove a profile from options.json values list and metadata.

    Returns True if the profile was found and removed.
    """
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False

    profile_sec = options.get("profile")
    if not profile_sec:
        return False

    values = profile_sec.get("values", [])
    found = name in values
    if found:
        values.remove(name)
        profile_sec["values"] = values

    pinned = profile_sec.get("pinned", [])
    if name in pinned:
        pinned.remove(name)
        profile_sec["pinned"] = pinned
        found = True

    metadata = profile_sec.get("metadata", {})
    if name in metadata:
        del metadata[name]
        found = True

    if found:
        write_json_atomic(OPTIONS_FILE, options)

    return found


def _remove_from_tokens(name: str) -> bool:
    """Remove a profile entry from tokens.json. Returns True if found."""
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False

    if name not in tokens:
        return False

    del tokens[name]
    write_json_atomic_secret(TOKENS_FILE, tokens)
    return True


def _purge_last_config_profile(name: str) -> bool:
    """Remove last_config["profile"] from state.json on disk if it names *name*.

    Returns True when a purge happened. Read-modify-write via the state
    helpers so all other state.json keys are preserved.
    """
    last_config = load_state_value("last_config")
    if not isinstance(last_config, dict) or last_config.get("profile") != name:
        return False
    del last_config["profile"]
    save_state_value("last_config", last_config)
    return True


def delete_profile_core(name: str, *, skip_running_check: bool = False,
                        allow_data_destruction: bool = False) -> DeleteResult:
    """Delete a profile and all associated data. Never prints.

    Refuses (in order): the built-in "default" profile; profiles neither
    registered in options.json nor present on disk under PROFILES_DIR;
    profiles with active sessions (unless skip_running_check); profiles
    holding REAL data at shared-dir names (unless allow_data_destruction).
    """
    # 1. "default" is Claude Code's built-in ~/.claude, not a claudewheel
    # profile. Deleting it would strip tokens/options entries while leaving
    # ~/.claude intact -- refuse outright.
    if name == "default":
        return DeleteResult(ok=False, refusal_reason="default-profile")

    # 2. Registration: accept profiles in options.json (values or pinned) OR
    # discovered-but-unregistered profiles whose dir exists under
    # PROFILES_DIR (the TUI shows those too).
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        options = {}
    profile_sec = options.get("profile", {})
    profile_values = profile_sec.get("values", [])
    profile_pinned = profile_sec.get("pinned", [])
    registered = name in profile_values or name in profile_pinned
    profile_dir = PROFILES_DIR / name
    if not registered and not profile_dir.is_dir():
        return DeleteResult(
            ok=False, refusal_reason="not-found",
            known_profiles=sorted(set(profile_values + profile_pinned)),
        )

    # 3. Running check
    if not skip_running_check and _is_profile_running(name):
        return DeleteResult(ok=False, refusal_reason="running")

    # 4. Data-destruction guard: BEFORE removing anything, refuse if any
    # shared-dir name holds real data (a real dir or file, not a symlink).
    # Profiles with no shared entries at all ("missing") are fine.
    if profile_dir.is_dir():
        states = classify_shared_dirs(profile_dir)
        at_risk = sorted(d for d, s in states.items() if s == "real-dir")
        if at_risk and not allow_data_destruction:
            return DeleteResult(ok=False, refusal_reason="data-destruction",
                                at_risk_dirs=at_risk)

    # 5. Remove profile dir, options entry, tokens entry, stale last_config
    sym, real = _remove_profile_dir(name)
    removed_options = _remove_from_options(name)
    removed_tokens = _remove_from_tokens(name)
    purged = _purge_last_config_profile(name)

    return DeleteResult(
        ok=True,
        removed_symlinks=sym,
        removed_real=real,
        removed_from_options=removed_options,
        removed_from_tokens=removed_tokens,
        last_config_purged=purged,
    )


def do_delete_profile(name: str, force: bool = False,
                      force_data: bool = False) -> int:
    """CLI wrapper: run delete_profile_core and print the outcome.

    Returns a process exit code (0 = success).
    """
    result = delete_profile_core(
        name, skip_running_check=force, allow_data_destruction=force_data,
    )

    if not result.ok:
        if result.refusal_reason == "default-profile":
            print(
                "Profile 'default' is Claude Code's built-in ~/.claude, "
                "not a claudewheel profile. Refusing to delete it.",
                file=sys.stderr,
            )
        elif result.refusal_reason == "not-found":
            print(f"Profile '{name}' is not registered in options.json.",
                  file=sys.stderr)
            print(f"Known profiles: {', '.join(result.known_profiles) or '<none>'}",
                  file=sys.stderr)
        elif result.refusal_reason == "running":
            print(
                f"Profile '{name}' appears to have active sessions. "
                "Use --force-delete to delete anyway.",
                file=sys.stderr,
            )
        elif result.refusal_reason == "data-destruction":
            print(
                f"Profile '{name}' holds REAL data (not symlinks) at: "
                f"{', '.join(result.at_risk_dirs)}.",
                file=sys.stderr,
            )
            print(
                "Deleting it would destroy that data. "
                "Use --force-delete-data to delete anyway.",
                file=sys.stderr,
            )
        return 1

    print(f"Deleting profile '{name}'...")
    print(f"  Removed dir: {result.removed_symlinks} symlinks unlinked, "
          f"{result.removed_real} real entries removed")
    if result.removed_from_options:
        print("  Removed from options.json")
    else:
        print("  Not found in options.json (already clean)")
    if result.removed_from_tokens:
        print("  Removed from tokens.json")
    else:
        print("  Not found in tokens.json (already clean)")
    if result.last_config_purged:
        print("  Cleared last_config profile reference in state.json")
    print(f"Profile '{name}' deleted.")
    return 0


# ---------------------------------------------------------------------------
# Profile rename
# ---------------------------------------------------------------------------

RENAME_PENDING_FILE = ".rename_pending"


def _update_options_rename(old: str, new: str) -> None:
    """Swap old->new in options.json values list, pinned list, and metadata."""
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return

    profile_sec = options.get("profile")
    if not profile_sec:
        return

    changed = False

    values = profile_sec.get("values", [])
    if old in values:
        idx = values.index(old)
        values[idx] = new
        changed = True

    pinned = profile_sec.get("pinned", [])
    if old in pinned:
        idx = pinned.index(old)
        pinned[idx] = new
        changed = True

    metadata = profile_sec.get("metadata", {})
    if old in metadata:
        entry = metadata.pop(old)
        entry["config_dir"] = f"~/.claudewheel/profiles/{new}"
        metadata[new] = entry
        changed = True

    if changed:
        write_json_atomic(OPTIONS_FILE, options)


def _update_tokens_rename(old: str, new: str) -> bool:
    """Move old key to new in tokens.json. Returns True if token existed."""
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False

    if old not in tokens:
        return False

    tokens[new] = tokens.pop(old)
    write_json_atomic_secret(TOKENS_FILE, tokens)
    return True


def _update_state_rename(old: str, new: str) -> None:
    """If last_config.profile == old, change to new."""
    last_config = load_state_value("last_config")
    if not isinstance(last_config, dict) or last_config.get("profile") != old:
        return
    last_config["profile"] = new
    save_state_value("last_config", last_config)


def rename_profile(old: str, new: str) -> None:
    """Rename a profile directory and update all JSON stores.

    Raises ValueError on validation failures, OSError on filesystem errors.
    The function is crash-safe: a .rename_pending breadcrumb file is written
    before the rename and removed after all stores are updated. If a crash
    occurs mid-rename, recover_incomplete_renames() can finish the job.
    """
    old_dir = PROFILES_DIR / old
    new_dir = PROFILES_DIR / new

    if not old_dir.is_dir():
        raise ValueError(f"Profile directory does not exist: {old_dir}")
    if new_dir.exists():
        raise ValueError(f"Target directory already exists: {new_dir}")

    # 1. Write breadcrumb
    pending_path = old_dir / RENAME_PENDING_FILE
    pending_path.write_text(json.dumps({"from": old, "to": new}))

    # 2. Atomic directory rename (same filesystem)
    os.rename(old_dir, new_dir)

    # 3-5. Update all JSON stores
    _update_tokens_rename(old, new)
    _update_options_rename(old, new)
    _update_state_rename(old, new)

    # 6. Remove breadcrumb (it moved with the dir in step 2)
    breadcrumb = new_dir / RENAME_PENDING_FILE
    if breadcrumb.exists():
        breadcrumb.unlink()


def recover_incomplete_renames() -> list[str]:
    """Scan PROFILES_DIR for .rename_pending breadcrumbs and finish the rename.

    Returns a list of recovered profile names (the new names).
    Called at startup to auto-repair after a crash mid-rename.
    """
    recovered: list[str] = []
    if not PROFILES_DIR.is_dir():
        return recovered

    for profile_dir in PROFILES_DIR.iterdir():
        if not profile_dir.is_dir():
            continue
        pending = profile_dir / RENAME_PENDING_FILE
        if not pending.exists():
            continue
        try:
            data = json.loads(pending.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        old = data.get("from")
        new = data.get("to")
        if not old or not new:
            continue
        # The dir was already renamed (it's at new's path now)
        if profile_dir.name != new:
            continue

        # Re-run store updates (idempotent: if they already reference new, no-op)
        _update_tokens_rename(old, new)
        _update_options_rename(old, new)
        _update_state_rename(old, new)

        # Remove breadcrumb
        pending.unlink()
        recovered.append(new)

    return recovered
