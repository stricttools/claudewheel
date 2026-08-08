"""Session lookup: locate session JSONL files and extract metadata."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

MAX_CWD_SCAN_LINES = 10

# Substring marker used to cheaply pre-filter JSONL lines before JSON-parsing
# them when scanning for a user-assigned session title. Claude Code writes the
# title as a record ``{"type":"custom-title","sessionId":...,"customTitle":...}``.
# Auto-generated ``ai-title`` and ``agent-name`` records are deliberately NOT
# matched by this marker (it is the exact ``custom-title`` type string).
CUSTOM_TITLE_MARKER = "custom-title"


@dataclass
class SessionInfo:
    """Metadata for a single session resolved from the shared store."""

    session_id: str
    jsonl_path: Path
    encoded_cwd: str
    cwd: str | None  # extracted from JSONL, None if unreadable


@dataclass
class TitleMatch:
    """A session file whose ``custom-title`` record matches a requested title."""

    session_id: str
    project_dir: Path
    mtime: float


@dataclass
class OrphanedProject:
    """A project directory in the shared store whose original cwd no longer exists."""

    encoded_cwd: str
    cwd: str
    session_count: int
    total_size_bytes: int
    projects_dir: Path  # full path to the project dir in the shared store


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
                    cwd = obj["cwd"]
                    return cwd if isinstance(cwd, str) else None
    except (FileNotFoundError, OSError):
        return None
    return None


def find_session(session_id: str, shared_projects_dir: Path) -> SessionInfo | None:
    """Locate a session by UUID in the shared projects store.

    Globs ``<shared_projects_dir>/*/<session_id>.jsonl`` and returns a
    :class:`SessionInfo` on the first match (UUIDs are globally unique).
    Returns ``None`` when no matching file exists.
    """
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


def _find_title_in_file(jsonl_path: Path, title: str) -> str | None:
    """Return the session UUID if *jsonl_path* holds a matching custom-title.

    Scans the file line by line. A cheap substring check on the raw line skips
    the overwhelming majority of lines (and files) without JSON-parsing them,
    which matters because real project dirs hold thousands of multi-megabyte
    JSONL files. Only lines containing :data:`CUSTOM_TITLE_MARKER` are parsed.

    A line matches only when it is a genuine ``custom-title`` record whose
    ``customTitle`` equals *title* exactly. Auto-generated ``ai-title`` and
    ``agent-name`` records are ignored (their ``type`` is not ``custom-title``).
    The returned UUID is the record's ``sessionId`` when present, else the
    file stem (Claude Code keeps these identical).
    """
    try:
        with jsonl_path.open() as fh:
            for line in fh:
                if CUSTOM_TITLE_MARKER not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "custom-title":
                    continue
                if obj.get("customTitle") == title:
                    sid = obj.get("sessionId")
                    return sid if isinstance(sid, str) else jsonl_path.stem
    except (FileNotFoundError, OSError):
        return None
    return None


def find_sessions_by_title(title: str, project_dirs: list[Path]) -> list[TitleMatch]:
    """Find sessions whose user-assigned title equals *title* exactly.

    Scans ONLY the top-level ``*.jsonl`` files of each directory in
    *project_dirs* (non-recursive, deliberately: subagent session files live two
    levels deeper under ``<parent>/subagents/`` and must never match a
    top-level resume). Returns one :class:`TitleMatch` per matching file, in
    directory order.
    """
    results: list[TitleMatch] = []
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        for jsonl_path in sorted(project_dir.glob("*.jsonl")):
            sid = _find_title_in_file(jsonl_path, title)
            if sid is not None:
                results.append(
                    TitleMatch(
                        session_id=sid,
                        project_dir=project_dir,
                        mtime=jsonl_path.stat().st_mtime,
                    )
                )
    return results


def find_orphaned_project_dirs(
    shared_projects_dir: Path,
) -> list[OrphanedProject]:
    """Find all project dirs whose original cwd no longer exists on disk.

    Scans every subdirectory of *shared_projects_dir*.  For each, reads the
    newest ``.jsonl`` file (by mtime) to extract the ``cwd``.  If the cwd is
    not ``None`` and no longer exists on disk, the project is included as an
    :class:`OrphanedProject`.
    """
    if not shared_projects_dir.is_dir():
        return []

    results: list[OrphanedProject] = []
    for project_dir in shared_projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not jsonl_files:
            continue

        cwd = get_session_cwd(jsonl_files[0])
        if cwd is None:
            continue

        if os.path.isdir(cwd):
            continue

        total_size = sum(f.stat().st_size for f in jsonl_files)
        results.append(
            OrphanedProject(
                encoded_cwd=project_dir.name,
                cwd=cwd,
                session_count=len(jsonl_files),
                total_size_bytes=total_size,
                projects_dir=project_dir,
            )
        )

    return results
