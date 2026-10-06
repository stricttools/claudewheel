"""Thin path owner for the ~/.claudewheel/shared store layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The machine-wide per-session lifecycle store (one <session-uuid>.jsonl per
# Claude Code session). Deliberately NOT in SHARED_SUBDIRS: that tuple lists the
# directories symlinked INTO each profile, and this one is never symlinked --
# every profile's sessions are recorded in the same place, because the record
# outlives the profile that launched the session.
LIFECYCLE_DIRNAME = "lifecycle"

# The machine-wide probe store (claudewheel.probe): probes, the OOM kills the
# probe runner read, and the reports it queued for each session. Not in
# SHARED_SUBDIRS either, for the same reason as the lifecycle store.
PROBES_DIRNAME = "probes"

# Where `claudewheel move-session` keeps the journal of a session move that has
# not finished (claudewheel.session_move): one <session-uuid>.json per move,
# removed when the move completes. Not in SHARED_SUBDIRS either.
SESSION_MOVES_DIRNAME = "session-moves"

# The longest store-dir name Claude Code writes in full; a longer sanitized
# path is cut to this length and suffixed with a hash of the raw path.
PROJECT_DIR_NAME_LIMIT = 200

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _path_hash_base36(p: str) -> str:
    """Claude Code's store-dir name hash of *p*, in base 36.

    A 31-multiplier string hash over the UTF-16 code units of *p*, kept to a
    signed 32-bit integer after every step (JavaScript's ``| 0``); the
    absolute value is written in base 36, as ``Math.abs(h).toString(36)``.
    """
    units = p.encode("utf-16-le", errors="surrogatepass")
    h = 0
    for i in range(0, len(units), 2):
        h = (h * 31 + int.from_bytes(units[i : i + 2], "little")) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    n = abs(h)
    if n == 0:
        return "0"
    digits: list[str] = []
    while n:
        n, r = divmod(n, 36)
        digits.append(_BASE36_DIGITS[r])
    return "".join(reversed(digits))


@dataclass(frozen=True)
class SharedStore:
    """Path owner for the shared store: projects, inodes, and per-profile subdirs.

    A thin, side-effect-free path resolver. It never reads or writes any file;
    it only computes paths under *shared_dir* (and holds *skills_dir*).
    """

    shared_dir: Path
    skills_dir: Path

    # Directories inside each profile that are symlinked to the shared store.
    # This module is the canonical home of the shared-store layout -- it must
    # NOT import from constants (constants is now ANSI/terminal-only).
    SHARED_SUBDIRS = (
        "projects",
        "session-env",
        "file-history",
        "tasks",
        "todos",
        "paste-cache",
    )

    @property
    def projects_dir(self) -> Path:
        """Directory holding per-project session data (shared/projects)."""
        return self.shared_dir / "projects"

    @property
    def lifecycle_dir(self) -> Path:
        """Directory holding the per-session lifecycle files (shared/lifecycle)."""
        return self.shared_dir / LIFECYCLE_DIRNAME

    @property
    def probes_dir(self) -> Path:
        """Directory holding the probe store (shared/probes)."""
        return self.shared_dir / PROBES_DIRNAME

    @property
    def session_moves_dir(self) -> Path:
        """Directory holding unfinished session-move journals (shared/session-moves)."""
        return self.shared_dir / SESSION_MOVES_DIRNAME

    @property
    def inodes_file(self) -> Path:
        """Path to the inode map file (shared/inodes.json)."""
        return self.shared_dir / "inodes.json"

    def subdir(self, name: str) -> Path:
        """Return the path to a named subdirectory of the shared store."""
        return self.shared_dir / name

    @staticmethod
    def encode_path_untruncated(p: str) -> str:
        """Claude Code's store-dir sanitizer before its length limit applies.

        Every UTF-16 code unit outside ``[a-zA-Z0-9]`` becomes ``-`` (Claude
        Code runs a JavaScript regex without the ``u`` flag, so a character
        outside the Basic Multilingual Plane is two code units and becomes
        two dashes).  The encoding is lossy: ``/``, ``.``, ``_``, a space and
        a literal ``-`` all become ``-``, so one encoded name can correspond
        to several real paths.  Encoding distributes over path joins:
        ``encode(a + "/" + b) == encode(a) + "-" + encode(b)``.
        """
        out: list[str] = []
        for ch in p:
            if ch.isascii() and ch.isalnum():
                out.append(ch)
            elif ord(ch) > 0xFFFF:
                out.append("--")
            else:
                out.append("-")
        return "".join(out)

    @staticmethod
    def encode_path(p: str) -> str:
        """The name of the store dir Claude Code keeps a path's sessions in.

        :meth:`encode_path_untruncated`, cut to
        :data:`PROJECT_DIR_NAME_LIMIT` characters and suffixed with ``-`` plus
        :func:`_path_hash_base36` of the raw path when it is longer than that.
        """
        name = SharedStore.encode_path_untruncated(p)
        if len(name) <= PROJECT_DIR_NAME_LIMIT:
            return name
        return f"{name[:PROJECT_DIR_NAME_LIMIT]}-{_path_hash_base36(p)}"
