"""Delete profiles and clean up their dirs, tokens, options, and xattrs."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
from pathlib import Path

from .constants import OPTIONS_FILE, ORIGINS_FILE, PROFILES_DIR, PROFILE_SHARED_DIRS, SHARED_DIR, TOKENS_FILE

XATTR_NAME = b"user.origin-profile"


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


def _strip_xattrs(name: str) -> int:
    """Remove origin-profile xattr from shared files stamped with this profile.

    Scans all 5 shared dirs for files/dirs whose user.origin-profile xattr
    matches `name` and removes the xattr. Returns the count modified.
    """
    if not SHARED_DIR.is_dir():
        return 0

    stripped = 0
    for dirname in PROFILE_SHARED_DIRS:
        dirpath = SHARED_DIR / dirname
        if not dirpath.is_dir():
            continue
        for f in dirpath.rglob("*"):
            try:
                val = os.getxattr(str(f), XATTR_NAME)
                if val.decode(errors="replace") == name:
                    os.removexattr(str(f), XATTR_NAME)
                    stripped += 1
            except OSError:
                pass
    return stripped


def _clean_origins_file(name: str) -> int:
    """Remove lines from profile-origins.jsonl that match the deleted profile.

    Returns the number of lines removed.
    """
    if not ORIGINS_FILE.is_file():
        return 0

    lock_path = str(ORIGINS_FILE) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lines = ORIGINS_FILE.read_text().splitlines()
        except OSError:
            return 0

        kept: list[str] = []
        removed = 0
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            try:
                entry = json.loads(line_stripped)
                if entry.get("profile") == name:
                    removed += 1
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            kept.append(line)

        if removed > 0:
            tmp = ORIGINS_FILE.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n" if kept else "")
            tmp.rename(ORIGINS_FILE)

    return removed


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

    # Session data preserved: xattrs and origins log entries are kept
    # so a private project maintains correct historical attribution.

    # 5. Remove from tokens.json
    if _remove_from_tokens(name):
        print("  Removed from tokens.json")
    else:
        print("  Not found in tokens.json (already clean)")

    print(f"Profile '{name}' deleted.")
    return 0
