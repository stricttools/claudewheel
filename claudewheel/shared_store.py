"""Thin path owner for the ~/.claudewheel/shared store layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SharedStore:
    """Path owner for the shared store: projects, inodes, and per-profile subdirs.

    A thin, side-effect-free path resolver. It never reads or writes any file;
    it only computes paths under *shared_dir* (and holds *skills_dir*).
    """

    shared_dir: Path
    skills_dir: Path

    # Directories inside each profile that are symlinked to the shared store.
    # Copied verbatim from constants.PROFILE_SHARED_DIRS -- this module must
    # NOT import from constants (it is the codec/layout's new canonical home).
    SHARED_SUBDIRS = ("projects", "session-env", "file-history", "tasks", "todos", "paste-cache")

    @property
    def projects_dir(self) -> Path:
        """Directory holding per-project session data (shared/projects)."""
        return self.shared_dir / "projects"

    @property
    def inodes_file(self) -> Path:
        """Path to the inode map file (shared/inodes.json)."""
        return self.shared_dir / "inodes.json"

    def subdir(self, name: str) -> Path:
        """Return the path to a named subdirectory of the shared store."""
        return self.shared_dir / name

    @staticmethod
    def encode_path(p: str) -> str:
        """Encode an absolute path the way Claude Code does: replace / and . with -."""
        return p.replace("/", "-").replace(".", "-")
