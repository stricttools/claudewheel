"""Scan the per-user Claude Code scratchpad tree under /tmp for stale data.

Claude Code sessions write scratchpad data under
``/tmp/claude-<uid>/<encoded-project>/<session-uuid>/...``. Each top-level
subdirectory of ``/tmp/claude-<uid>/`` corresponds to one project. This module
enumerates those per-project subdirectories, computing for each its real tmpfs
block usage and the newest mtime anywhere in its tree, and classifies a
directory as "stale" when nothing in it has been touched within
:data:`SCRATCHPAD_STALE_DAYS`.

Size accounting mirrors :func:`claudewheel.health._real_disk_usage`: it walks
with ``followlinks=False``, ``lstat``s each entry, counts ``st_blocks * 512``
for regular files only, and never follows symlinks (symlink targets living
outside /tmp are never charged against /tmp space).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

# A per-project scratchpad directory is stale when nothing anywhere in its tree
# has been modified within this many days.
SCRATCHPAD_STALE_DAYS = 14

# When the user declines a cleanup prompt, suppress further prompts for this many
# days (the snooze window).
SCRATCHPAD_SNOOZE_DAYS = 7

_SECONDS_PER_DAY = 86400.0


def tmp_claude_dir() -> Path:
    """Return the per-user Claude scratchpad root under /tmp."""
    return Path(f"/tmp/claude-{os.getuid()}")


@dataclass(frozen=True)
class ScratchpadDir:
    """One top-level per-project scratchpad directory and its measured facts.

    - ``size_bytes``: real tmpfs block usage (``st_blocks * 512``) of the regular
      files in its tree; symlinks and non-regular files contribute nothing.
    - ``newest_mtime``: the newest ``lstat`` mtime of any entry in its tree
      (directories and files, including the directory itself). Symlinks are
      lstat'd (their own mtime), never followed.
    """

    path: Path
    name: str
    size_bytes: int
    newest_mtime: float

    def age_days(self, now: float) -> float:
        """Days since the newest activity in this tree, relative to *now* (epoch)."""
        return max(0.0, (now - self.newest_mtime) / _SECONDS_PER_DAY)

    def is_stale(self, now: float, stale_days: int = SCRATCHPAD_STALE_DAYS) -> bool:
        """True when the newest activity is older than *stale_days* before *now*."""
        return self.age_days(now) > stale_days


def _scan_tree(root: Path) -> tuple[int, float]:
    """Return ``(real_block_bytes, newest_mtime)`` for the tree under *root*.

    Mirrors :func:`claudewheel.health._real_disk_usage` for the size half:
    ``os.walk(followlinks=False)`` does not descend into symlinked directories,
    and ``lstat`` + ``S_ISREG`` counts only regular files (``st_blocks * 512``).
    The mtime half considers every directory and file encountered -- including
    *root* itself -- and uses each entry's own ``lstat`` mtime, so a symlink
    contributes its own mtime and is never followed to its target.
    """
    total = 0
    try:
        newest = os.lstat(root).st_mtime
    except OSError:
        newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if st.st_mtime > newest:
                newest = st.st_mtime
            if stat.S_ISREG(st.st_mode):
                total += st.st_blocks * 512
    return total, newest


def scan_scratchpad_dirs(root: Path) -> list[ScratchpadDir]:
    """Enumerate the top-level per-project subdirectories of *root*.

    Returns one :class:`ScratchpadDir` per immediate subdirectory (sorted by
    name), skipping non-directories and top-level symlinks (which are never
    followed). A missing or non-directory *root* yields an empty list.
    """
    if not root.is_dir():
        return []
    results: list[ScratchpadDir] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        size, newest = _scan_tree(entry)
        results.append(
            ScratchpadDir(
                path=entry, name=entry.name, size_bytes=size, newest_mtime=newest
            )
        )
    return results


def stale_scratchpad_dirs(
    root: Path, now: float, stale_days: int = SCRATCHPAD_STALE_DAYS
) -> list[ScratchpadDir]:
    """Return the scratchpad subdirectories of *root* that are stale at *now*."""
    return [d for d in scan_scratchpad_dirs(root) if d.is_stale(now, stale_days)]
