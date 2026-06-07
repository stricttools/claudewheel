"""Delete profiles and clean up their dirs, tokens, and options."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .constants import OPTIONS_FILE, PROFILES_DIR, TOKENS_FILE


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

    metadata = profile_sec.get("metadata", {})
    if name in metadata:
        del metadata[name]
        found = True

    if found:
        # Atomic write via tmp-file rename (matches ConfigManager._save_json)
        tmp = OPTIONS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(options, f, indent=2)
            f.write("\n")
        tmp.rename(OPTIONS_FILE)

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
    tmp = TOKENS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
        f.write("\n")
    tmp.rename(TOKENS_FILE)
    return True


def do_delete_profile(name: str, force: bool = False) -> int:
    """Delete a Claude Code profile and all associated data.

    Returns a process exit code (0 = success).
    """
    # 1. Validate: must be in options.json
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        print(f"Cannot read {OPTIONS_FILE}", file=sys.stderr)
        return 1

    profile_values = options.get("profile", {}).get("values", [])
    if name not in profile_values:
        print(f"Profile '{name}' is not registered in options.json.", file=sys.stderr)
        print(f"Known profiles: {', '.join(profile_values) or '<none>'}", file=sys.stderr)
        return 1

    # 2. Check if running
    if _is_profile_running(name) and not force:
        print(
            f"Profile '{name}' appears to have active sessions. "
            "Use --force to delete anyway.",
            file=sys.stderr,
        )
        return 1

    print(f"Deleting profile '{name}'...")

    # 3. Remove profile dir (config, credentials — not shared session data)
    sym, real = _remove_profile_dir(name)
    print(f"  Removed dir: {sym} symlinks unlinked, {real} real entries removed")

    # 4. Remove from options.json
    if _remove_from_options(name):
        print("  Removed from options.json")
    else:
        print("  Not found in options.json (already clean)")

    # 5. Remove from tokens.json
    if _remove_from_tokens(name):
        print("  Removed from tokens.json")
    else:
        print("  Not found in tokens.json (already clean)")

    print(f"Profile '{name}' deleted.")
    return 0
