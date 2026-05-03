"""Redirect session data after a project directory rename."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PREFIX = "[redir]"


def _log(msg: str) -> None:
    print(f"{PREFIX} {msg}")


@dataclass
class RedirResult:
    dirs_renamed: int = 0
    files_rewritten: int = 0
    lines_replaced: int = 0
    project_keys_updated: int = 0
    profiles_scanned: int = 0


def _encode_path(p: str) -> str:
    """Encode an absolute path the way Claude Code does: replace / with -."""
    return p.replace("/", "-")


def _discover_profile_dirs() -> list[Path]:
    """Find all ~/.claude-*/ directories plus ~/.claude-shared/ if it exists."""
    home = Path.home()
    dirs: list[Path] = []
    for entry in sorted(home.glob(".claude-*")):
        if entry.is_dir() and not entry.is_symlink():
            dirs.append(entry)
    # Ensure shared is included even if glob already caught it
    shared = home / ".claude-shared"
    if shared.is_dir() and shared not in dirs:
        dirs.append(shared)
    return dirs


def _rewrite_jsonl_file(
    path: Path, old_path: str, new_path: str, dry_run: bool,
) -> int:
    """Replace old_path with new_path in every line of a JSONL file.

    Returns the number of lines where a replacement was made.
    """
    try:
        lines = path.read_text().splitlines(keepends=True)
    except OSError as e:
        _log(f"  cannot read {path}: {e}")
        return 0

    replaced = 0
    new_lines: list[str] = []
    for line in lines:
        if old_path in line:
            new_lines.append(line.replace(old_path, new_path))
            replaced += 1
        else:
            new_lines.append(line)

    if replaced > 0:
        if dry_run:
            _log(f"  would rewrite {path} ({replaced} lines)")
        else:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(new_lines))
            tmp.rename(path)
            _log(f"  rewrote {path} ({replaced} lines)")

    return replaced


def _update_claude_json(
    path: Path, old_path: str, new_path: str, dry_run: bool,
) -> bool:
    """Rename a top-level project key in .claude.json. Returns True if updated."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _log(f"  cannot parse {path}: {e}")
        return False

    if old_path not in data:
        return False

    if dry_run:
        _log(f"  would rename key {old_path!r} -> {new_path!r} in {path}")
        return True

    data[new_path] = data.pop(old_path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(path)
    _log(f"  renamed key {old_path!r} -> {new_path!r} in {path}")
    return True


def run_redir(
    old_path: str, new_path: str, dry_run: bool = False,
) -> RedirResult:
    """Redirect Claude Code session data from old_path to new_path.

    Both paths refer to the same project directory -- old_path is the former
    location (must no longer exist) and new_path is the current one (must exist).
    """
    result = RedirResult()

    # 1. Resolve paths
    old_resolved = str(Path(old_path).expanduser().resolve())
    new_resolved = str(Path(new_path).expanduser().resolve())

    if not Path(new_resolved).is_dir():
        raise FileNotFoundError(f"new path does not exist as a directory: {new_resolved}")
    if Path(old_resolved).exists():
        raise FileExistsError(
            f"old path still exists: {old_resolved} -- rename the directory first"
        )

    _log(f"redirecting {old_resolved} -> {new_resolved}")
    if dry_run:
        _log("DRY RUN -- no changes will be made")

    # 2. Compute encoded directory names
    old_encoded = _encode_path(old_resolved)
    new_encoded = _encode_path(new_resolved)
    _log(f"encoded: {old_encoded} -> {new_encoded}")

    # 3. Discover profile dirs
    profile_dirs = _discover_profile_dirs()
    result.profiles_scanned = len(profile_dirs)
    _log(f"found {len(profile_dirs)} profile/shared dirs")

    # 4. Process projects/ in each profile dir
    for pdir in profile_dirs:
        projects = pdir / "projects"
        if not projects.is_dir():
            continue

        old_project = projects / old_encoded
        new_project = projects / new_encoded

        # 4a. Rename the project directory
        if old_project.is_dir():
            if new_project.exists():
                _log(f"  ERROR: target already exists: {new_project}, skipping rename")
            elif dry_run:
                _log(f"  would rename {old_project} -> {new_project}")
                result.dirs_renamed += 1
            else:
                old_project.rename(new_project)
                _log(f"  renamed {old_project} -> {new_project}")
                result.dirs_renamed += 1

        # 4b. Rewrite JSONL files in the (now renamed) project dir
        if new_project.is_dir():
            for jsonl_path in new_project.rglob("*.jsonl"):
                # Skip history.jsonl -- append-only, not critical for resume
                if jsonl_path.name == "history.jsonl":
                    continue
                lines_fixed = _rewrite_jsonl_file(
                    jsonl_path, old_resolved, new_resolved, dry_run,
                )
                if lines_fixed > 0:
                    result.files_rewritten += 1
                    result.lines_replaced += lines_fixed

    # 5. Update .claude.json in each profile dir (not shared)
    shared = Path.home() / ".claude-shared"
    for pdir in profile_dirs:
        if pdir == shared:
            continue
        claude_json = pdir / ".claude.json"
        if claude_json.is_file():
            if _update_claude_json(claude_json, old_resolved, new_resolved, dry_run):
                result.project_keys_updated += 1

    # 6. Summary
    _log("summary")
    _log(f"  dirs renamed:          {result.dirs_renamed}")
    _log(f"  files rewritten:       {result.files_rewritten}")
    _log(f"  lines replaced:        {result.lines_replaced}")
    _log(f"  project keys updated:  {result.project_keys_updated}")
    _log(f"  profiles scanned:      {result.profiles_scanned}")
    if dry_run:
        _log("  (dry run -- nothing written)")

    return result
