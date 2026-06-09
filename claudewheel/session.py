"""Session lookup: locate session JSONL files and extract metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .constants import SHARED_DIR

MAX_CWD_SCAN_LINES = 10


@dataclass
class SessionInfo:
    """Metadata for a single session resolved from the shared store."""

    session_id: str
    jsonl_path: Path
    encoded_cwd: str
    cwd: str | None  # extracted from JSONL, None if unreadable


def get_session_cwd(
    jsonl_path: Path, max_lines: int = MAX_CWD_SCAN_LINES
) -> str | None:
    """Read up to *max_lines* from a JSONL file and return the first ``cwd`` value.

    Returns ``None`` when the file is missing, empty, or contains no ``cwd``
    field within the scanned range.  Corrupt JSON lines are silently skipped.
    """
    try:
        with jsonl_path.open() as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if "cwd" in obj:
                    return obj["cwd"]
    except (FileNotFoundError, OSError):
        return None
    return None


def find_session(
    session_id: str, shared_projects_dir: Path | None = None
) -> SessionInfo | None:
    """Locate a session by UUID in the shared projects store.

    Globs ``<shared_projects_dir>/*/<session_id>.jsonl`` and returns a
    :class:`SessionInfo` on the first match (UUIDs are globally unique).
    Returns ``None`` when no matching file exists.
    """
    if shared_projects_dir is None:
        shared_projects_dir = SHARED_DIR / "projects"

    matches = list(shared_projects_dir.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None

    jsonl_path = matches[0]
    encoded_cwd = jsonl_path.parent.name
    cwd = get_session_cwd(jsonl_path)

    return SessionInfo(
        session_id=session_id,
        jsonl_path=jsonl_path,
        encoded_cwd=encoded_cwd,
        cwd=cwd,
    )
