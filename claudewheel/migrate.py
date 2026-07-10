"""Move session artifacts between profiles."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .constants import PROFILES_DIR, TOKENS_FILE
from .profile_store import ProfileStore
from .tokens import TokenStore

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Dirs whose direct children are keyed by UUID
SIMPLE_DIRS = ("session-env", "file-history", "tasks")

PREFIX = "[migrate]"


@dataclass
class MigrateResult:
    """Counters tracking the outcome of a session migration operation."""

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
    """Migrate session artifacts from src_profile to dst_profile.

    Profile names resolve through :meth:`ProfileStore.path_for`, so ``"default"``
    (Claude Code's built-in ``~/.claude``) is a valid source or destination in
    either direction.
    """
    store = ProfileStore(
        PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE)
    )
    src = store.path_for(src_profile)
    dst = store.path_for(dst_profile)
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

                if jsonl.exists() and not skip_move:
                    _move_artifact(jsonl, dst / "projects" / cwd_name / f"{uuid}.jsonl", result, dry_run)

                if sub.is_dir() and not skip_move:
                    _move_artifact(sub, dst / "projects" / cwd_name / uuid, result, dry_run)

        # --- session-env, file-history, tasks ---
        for d in SIMPLE_DIRS:
            artifact = src / d / uuid
            if artifact.exists() and not skip_move:
                _move_artifact(artifact, dst / d / uuid, result, dry_run)

        # --- todos/<uuid>-agent-*.json ---
        todos_dir = src / "todos"
        if todos_dir.is_dir():
            for todo in todos_dir.iterdir():
                if todo.name.startswith(f"{uuid}-agent-") and todo.name.endswith(".json"):
                    if not skip_move:
                        _move_artifact(todo, dst / "todos" / todo.name, result, dry_run)

    _log("summary")
    _log(f"  moved:      {result.moved}")
    _log(f"  collisions: {result.collisions}")
    if dry_run:
        _log("  (dry run — nothing written)")

    return result
