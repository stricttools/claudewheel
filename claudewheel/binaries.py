"""Locate installed Claude Code binaries and the active `claude` symlink."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .segment import version_sort_key


@dataclass(frozen=True)
class BinaryLocator:
    """Locate the Claude Code binary for a version, the fallback symlink, and installed versions.

    ``versions_dir`` and ``claude_symlink`` live outside ``~/.claudewheel`` --
    they locate the Claude Code binary itself, not claudewheel's own data.
    """

    versions_dir: Path
    claude_symlink: Path

    @classmethod
    def default(cls) -> "BinaryLocator":
        """Build a locator at today's default paths, computed at call time."""
        home = Path.home()
        return cls(
            versions_dir=home / ".local/share/claude/versions",
            claude_symlink=home / ".local/bin/claude",
        )

    def binary_for(self, version: str) -> Path:
        """Return the on-disk path for a specific installed version's binary."""
        return self.versions_dir / version

    @property
    def fallback(self) -> Path:
        """Return the `claude` symlink used when no specific version is selected."""
        return self.claude_symlink

    def installed_versions(self) -> list[str]:
        """List installed version names, newest first.

        Mirrors the cli ``versions`` handler: only regular files under
        ``versions_dir`` count, sorted by :func:`version_sort_key` descending.
        Returns an empty list when ``versions_dir`` is not a directory.
        """
        if not self.versions_dir.is_dir():
            return []
        return sorted(
            [e.name for e in self.versions_dir.iterdir() if e.is_file()],
            key=version_sort_key,
            reverse=True,
        )

    def symlink_target(self) -> Path | None:
        """Return the resolved target of the `claude` symlink, or None.

        Returns None when the symlink does not exist or cannot be resolved.
        The current version name is the ``.name`` of the returned path.
        """
        try:
            if self.claude_symlink.is_symlink() or self.claude_symlink.exists():
                return self.claude_symlink.resolve()
        except OSError:
            pass
        return None
