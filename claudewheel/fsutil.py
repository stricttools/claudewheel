"""Atomic file writes that preserve or enforce target permissions."""

from __future__ import annotations

import json
import os
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """Atomic tmp+rename text write that preserves the target's file mode.

    The rename replaces the target inode, so without a chmod any
    pre-existing restrictive mode on the target would be silently reset
    to the umask default on every update. Fresh targets (no existing
    file to stat) keep the umask default.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    try:
        tmp.chmod(path.stat().st_mode & 0o777)
    except FileNotFoundError:
        pass  # fresh file: umask default is fine
    tmp.rename(path)


def write_json_atomic(path: Path, data) -> None:
    """Atomic JSON write (indent=2, trailing newline), preserving file mode."""
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def write_json_atomic_secret(path: Path, data) -> None:
    """Atomic JSON write for secret-holding files: target is always 0600.

    The tmp file is created 0600 from the start (never umask-readable,
    even transiently) and chmod'd to exactly 0600 before the rename in
    case the umask stripped owner bits at creation.
    """
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.rename(path)
