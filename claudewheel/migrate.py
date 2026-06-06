"""Migrate session-keyed artifacts between Claude Code profile dirs."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
XATTR_NAME = "user.origin-profile"
# Dirs whose direct children are keyed by UUID
SIMPLE_DIRS = ("session-env", "file-history", "tasks")

PREFIX = "[migrate]"


@dataclass
class MigrateResult:
    """Counters tracking the outcome of a session migration operation."""

    stamped: int = 0
    already_stamped: int = 0
    moved: int = 0
    skipped_move: int = 0
    collisions: int = 0
    uuids_found: int = 0


def _log(msg: str) -> None:
    print(f"{PREFIX} {msg}")


def _is_uuid(name: str) -> bool:
    return UUID_RE.match(name) is not None


def _resolve_symlink_target(p: Path) -> Path | None:
    """Return the resolved target if p is a symlink, else None."""
    try:
        if p.is_symlink():
            return p.resolve()
    except OSError:
        pass
    return None


def _discover_uuids(src: Path) -> set[str]:
    """Union of session UUIDs found across all five artifact dirs."""
    uuids: set[str] = set()

    # projects/<cwd>/<uuid>.jsonl and projects/<cwd>/<uuid>/
    projects = src / "projects"
    if projects.is_dir():
        for cwd_dir in projects.iterdir():
            if not cwd_dir.is_dir():
                continue
            for entry in cwd_dir.iterdir():
                name = entry.name
                if name.endswith(".jsonl"):
                    stem = name[:-6]  # strip .jsonl
                    if _is_uuid(stem):
                        uuids.add(stem)
                elif entry.is_dir() and _is_uuid(name):
                    uuids.add(name)

    # session-env/, file-history/, tasks/: direct child dirs named as UUIDs
    for d in SIMPLE_DIRS:
        dirpath = src / d
        if not dirpath.is_dir():
            continue
        for entry in dirpath.iterdir():
            if _is_uuid(entry.name):
                uuids.add(entry.name)

    # todos/<uuid>-agent-*.json
    todos = src / "todos"
    if todos.is_dir():
        for entry in todos.iterdir():
            name = entry.name
            if "-agent-" in name and name.endswith(".json"):
                prefix = name.split("-agent-", 1)[0]
                if _is_uuid(prefix):
                    uuids.add(prefix)

    return uuids


def _stamp_xattr(
    path: Path, profile: str, index_path: Path, ts: str,
    result: MigrateResult, dry_run: bool,
) -> None:
    """Stamp origin-profile xattr on path. Skip if already stamped."""
    if not path.exists():
        return
    spath = str(path)
    try:
        os.getxattr(spath, XATTR_NAME)
        result.already_stamped += 1
        return
    except OSError:
        pass  # not stamped yet

    result.stamped += 1
    if dry_run:
        _log(f"STAMP {spath}")
        return

    os.setxattr(spath, XATTR_NAME, profile.encode())
    entry = json.dumps({"path": spath, "profile": profile, "ts": ts, "phase": "migrate"})
    lock_path = str(index_path) + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with open(index_path, "a") as f:
            f.write(entry + "\n")


def _move_artifact(
    src: Path, dst: Path, result: MigrateResult, dry_run: bool,
) -> None:
    """Move src to dst. Refuse to overwrite (collision)."""
    if not src.exists():
        return
    if dst.exists():
        _log(f"COLLISION: {dst} already exists, leaving {src} in place")
        result.collisions += 1
        return

    result.moved += 1
    if dry_run:
        _log(f"MOVE  {src}")
        _log(f"  ->  {dst}")
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _shared_store(src: Path, dst: Path) -> bool:
    """True if src/projects and dst/projects symlink to the same target."""
    src_target = _resolve_symlink_target(src / "projects")
    dst_target = _resolve_symlink_target(dst / "projects")
    return src_target is not None and src_target == dst_target


def migrate_sessions(
    src_profile: str,
    dst_profile: str,
    uuid_filter: str | None = None,
    dry_run: bool = False,
) -> MigrateResult:
    """Migrate session artifacts from src_profile to dst_profile."""
    src = Path.home() / f".claude-{src_profile}"
    dst = Path.home() / f".claude-{dst_profile}"
    from .constants import COMMON_DIR
    index_path = COMMON_DIR / "profile-origins.jsonl"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = MigrateResult()

    if not src.is_dir():
        raise FileNotFoundError(f"source profile dir missing: {src}")
    if not dst.is_dir():
        raise FileNotFoundError(f"dest profile dir missing: {dst}")

    # Discover UUIDs
    uuids = _discover_uuids(src)
    if uuid_filter:
        uuids = {u for u in uuids if uuid_filter in u}
    result.uuids_found = len(uuids)

    skip_move = _shared_store(src, dst)

    _log(f"{src_profile} -> {dst_profile}")
    _log(f"discovered {result.uuids_found} session UUIDs in source")
    if skip_move:
        _log("shared store detected — skipping moves (files already co-located)")
    if dry_run:
        _log("DRY RUN — no changes will be made")

    # Ensure index parent exists (not in dry-run to avoid side-effects)
    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)

    for uuid in sorted(uuids):
        # --- projects/<cwd>/<uuid>.jsonl and <uuid>/ ---
        projects = src / "projects"
        if projects.is_dir():
            for cwd_dir in projects.iterdir():
                if not cwd_dir.is_dir():
                    continue
                jsonl = cwd_dir / f"{uuid}.jsonl"
                sub = cwd_dir / uuid
                cwd_name = cwd_dir.name

                if jsonl.exists():
                    _stamp_xattr(jsonl, src_profile, index_path, ts, result, dry_run)
                    if not skip_move:
                        _move_artifact(jsonl, dst / "projects" / cwd_name / f"{uuid}.jsonl", result, dry_run)

                if sub.is_dir():
                    _stamp_xattr(sub, src_profile, index_path, ts, result, dry_run)
                    if not skip_move:
                        _move_artifact(sub, dst / "projects" / cwd_name / uuid, result, dry_run)

        # --- session-env, file-history, tasks ---
        for d in SIMPLE_DIRS:
            artifact = src / d / uuid
            if artifact.exists():
                _stamp_xattr(artifact, src_profile, index_path, ts, result, dry_run)
                if not skip_move:
                    _move_artifact(artifact, dst / d / uuid, result, dry_run)

        # --- todos/<uuid>-agent-*.json ---
        todos_dir = src / "todos"
        if todos_dir.is_dir():
            for todo in todos_dir.iterdir():
                if todo.name.startswith(f"{uuid}-agent-") and todo.name.endswith(".json"):
                    _stamp_xattr(todo, src_profile, index_path, ts, result, dry_run)
                    if not skip_move:
                        _move_artifact(todo, dst / "todos" / todo.name, result, dry_run)

    _log("summary")
    _log(f"  stamped:         {result.stamped}")
    _log(f"  already stamped: {result.already_stamped}")
    _log(f"  moved:           {result.moved}")
    _log(f"  collisions:      {result.collisions}")
    if dry_run:
        _log("  (dry run — nothing written)")

    return result
