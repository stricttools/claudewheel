"""Report shared-store statistics and clean up legacy data."""
from __future__ import annotations
import shutil
from .constants import SHARED_DIR, SKILLS_DIR
from .shared_store import SharedStore


def _log(msg: str) -> None:
    print(f"[stats] {msg}")


def _report_shared_stats() -> None:
    """Print file count and total size of the shared store by subdirectory."""
    shared = SharedStore(SHARED_DIR, SKILLS_DIR).shared_dir
    if not shared.is_dir():
        _log("shared store not found")
        return
    _log(f"shared store: {shared}")
    rows, tf, tb = [], 0, 0
    for entry in sorted(shared.iterdir(), key=lambda e: e.name):
        if entry.is_dir() and not entry.is_symlink():
            files = [f for f in entry.rglob("*") if f.is_file() and not f.is_symlink()]
            c, s = len(files), sum(f.stat().st_size for f in files)
        elif entry.is_file():
            c, s = 1, entry.stat().st_size
        else:
            continue
        rows.append((entry.name, c, s))
        tf += c
        tb += s
    for name, c, s in rows:
        _log(f"  {name:<20s} {c:>6d} files  {s / 1024:>10.1f} KB")
    _log(f"  {'TOTAL':<20s} {tf:>6d} files  {tb / 1024:>10.1f} KB")


def run_stats(dry_run: bool = False) -> None:
    """Report shared-store stats and clean up legacy data."""
    if dry_run:
        _log("DRY RUN -- no changes will be made")
    sentinels_dir = SharedStore(SHARED_DIR, SKILLS_DIR).subdir("sentinels")
    if sentinels_dir.is_dir():
        if dry_run:
            _log(f"would remove legacy sentinels dir: {sentinels_dir}")
        else:
            shutil.rmtree(sentinels_dir)
            _log(f"removed legacy sentinels dir: {sentinels_dir}")
    _report_shared_stats()
    _log("done")
