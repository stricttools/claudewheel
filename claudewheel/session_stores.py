"""Find every Claude Code session store on this machine.

A session store is a ``projects`` directory: one per profile directory, and
the shared one under ``~/.claudewheel/shared/``.  Managed profiles' ``projects``
entries are symlinks to the shared store, while the ``default`` profile
(``~/.claude``) may keep its own, so the same real directory is reached under
several names and a pass over session data visits each real directory once.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Workspace


def discover_profile_dirs(ws: "Workspace") -> list[Path]:
    """Find all profile directories plus ~/.claudewheel/shared/ if it exists.

    Enumerates profiles via the workspace's ProfileStore, then includes the
    shared store directory as a peer target (it holds the actual session data).
    A corrupt token entry raises ``TokenStoreError`` -- the uniform hard-error
    contract.
    """
    dirs: list[Path] = [p.path for p in ws.profiles.enumerate()]
    shared_dir = ws.shared_dir
    if shared_dir.is_dir() and shared_dir not in dirs:
        dirs.append(shared_dir)
    return sorted(dirs)


def distinct_store_dirs(profile_dirs: list[Path]) -> list[Path]:
    """The ``projects`` store dirs of *profile_dirs*, each real directory once.

    Managed profiles' ``projects`` entries are symlinks to the one shared
    store, so several profile dirs reach the same directory.  Every pass over
    session data must visit a real directory once: a second visit rewrites
    already-rewritten paths again (``foo -> foobar`` compounding into
    ``foobarbar``) and multiplies every counter.  Returned as resolved paths.
    """
    seen: set[Path] = set()
    dirs: list[Path] = []
    for pdir in profile_dirs:
        projects = pdir / "projects"
        if not projects.is_dir():
            continue
        real = projects.resolve()
        if real not in seen:
            seen.add(real)
            dirs.append(real)
    return dirs
