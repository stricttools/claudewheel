"""Clean stale sentinels, compact origin logs, and report shared-store stats."""
from __future__ import annotations
import fcntl
import json
import re
import time
from pathlib import Path
from .constants import COMMON_DIR, OPTIONS_FILE, SHARED_DIR

ORIGINS_FILE = COMMON_DIR / "profile-origins.jsonl"
STALE_THRESHOLD = 30 * 24 * 3600  # 30 days

def _log(msg: str) -> None:
    print(f"[gc] {msg}")


def _known_profiles() -> set[str]:
    known = {"personal", "work", "6_J5"}
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
        known.update(opts.get("profile", {}).get("values", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return known


def _clean_sentinels(dry_run: bool) -> int:
    """Remove .stamped-<uuid> sentinel files older than 30 days."""
    if not SHARED_DIR.is_dir():
        return 0
    now, removed = time.time(), 0
    for entry in SHARED_DIR.iterdir():
        if not entry.name.startswith(".stamped-"):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age >= STALE_THRESHOLD:
            removed += 1
            if not dry_run:
                entry.unlink(missing_ok=True)
            _log(f"{'would remove' if dry_run else 'removed'} {entry.name} ({age / 86400:.0f}d)")
    return removed


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

def _compact_origins(dry_run: bool) -> tuple[int, int]:
    """Remove unknown-profile entries and deduplicate by UUID."""
    if not ORIGINS_FILE.is_file():
        return 0, 0
    lock_path = str(ORIGINS_FILE) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lines = ORIGINS_FILE.read_text().splitlines()
        except OSError:
            return 0, 0
        known = _known_profiles()
        kept: list[str] = []
        seen_uuids: set[str] = set()
        removed = 0
        # Process in reverse so we keep the LATEST entry per UUID
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                kept.append(line)
                continue
            if entry.get("profile") not in known:
                removed += 1
                continue
            m = _UUID_RE.search(entry.get("path", ""))
            if m:
                uuid = m.group()
                if uuid in seen_uuids:
                    removed += 1
                    continue
                seen_uuids.add(uuid)
            kept.append(line)
        kept.reverse()
        if removed > 0 and not dry_run:
            tmp = ORIGINS_FILE.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n" if kept else "")
            tmp.rename(ORIGINS_FILE)
    return len(kept), removed


def _report_shared_stats() -> None:
    """Print file count and total size of SHARED_DIR by subdirectory."""
    if not SHARED_DIR.is_dir():
        _log("shared store not found"); return
    _log(f"shared store: {SHARED_DIR}")
    rows, tf, tb = [], 0, 0
    for entry in sorted(SHARED_DIR.iterdir(), key=lambda e: e.name):
        if entry.is_dir() and not entry.is_symlink():
            files = [f for f in entry.rglob("*") if f.is_file() and not f.is_symlink()]
            c, s = len(files), sum(f.stat().st_size for f in files)
        elif entry.is_file():
            c, s = 1, entry.stat().st_size
        else:
            continue
        rows.append((entry.name, c, s)); tf += c; tb += s
    for name, c, s in rows:
        _log(f"  {name:<20s} {c:>6d} files  {s / 1024:>10.1f} KB")
    _log(f"  {'TOTAL':<20s} {tf:>6d} files  {tb / 1024:>10.1f} KB")


def run_gc(dry_run: bool = False) -> None:
    """Run garbage collection across shared store and profile infrastructure."""
    if dry_run:
        _log("DRY RUN -- no changes will be made")
    _log(f"sentinels removed: {_clean_sentinels(dry_run)}")
    kept, removed = _compact_origins(dry_run)
    _log(f"profile-origins.jsonl: kept {kept}, removed {removed}")
    _report_shared_stats()
    _log("done")
