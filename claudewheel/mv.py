"""Move session data after a project directory rename."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .fsutil import write_json_atomic, write_text_atomic
from .shared_store import SharedStore

if TYPE_CHECKING:
    from .workspace import Workspace

PREFIX = "[mv]"


_quiet = False


def _log(msg: str) -> None:
    if not _quiet:
        print(f"{PREFIX} {msg}")


@dataclass
class MvResult:
    """Counters tracking the outcome of a project-directory move operation."""

    dirs_renamed: int = 0
    files_rewritten: int = 0
    lines_replaced: int = 0
    project_keys_updated: int = 0
    github_repo_paths_updated: int = 0
    paths_migrated: int = 0
    profiles_scanned: int = 0


def _discover_profile_dirs(ws: "Workspace") -> list[Path]:
    """Find all profile directories plus ~/.claudewheel/shared/ if it exists.

    Enumerates profiles via the workspace's ProfileStore, then includes the
    shared store directory as a peer target (it holds the actual session data).
    A corrupt tokens.json raises ``TokenStoreError`` -- the uniform hard-error
    contract.
    """
    dirs: list[Path] = [p.path for p in ws.profiles.enumerate()]
    shared_dir = ws.shared_dir
    if shared_dir.is_dir() and shared_dir not in dirs:
        dirs.append(shared_dir)
    return sorted(dirs)


def _rewrite_jsonl_file(
    path: Path,
    old_path: str,
    new_path: str,
    dry_run: bool,
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
            write_text_atomic(path, "".join(new_lines))
            _log(f"  rewrote {path} ({replaced} lines)")

    return replaced


# ---------------------------------------------------------------------------
# Prefix-aware descendant discovery
# ---------------------------------------------------------------------------


def _plan_migrations(
    old_resolved: str,
    new_resolved: str,
    descendants: set[str],
) -> list[tuple[str, str]]:
    """Build the ordered ``(old, new)`` migration plan.

    Includes ``old_resolved`` itself.  Every destination is ``new_resolved``
    plus the source's relative suffix.  Longest old paths come first so a
    shorter prefix is never processed before its own descendants
    (prefix-shadowing prevention, same pattern as import_'s rewriters).
    """
    paths = {old_resolved} | set(descendants)
    ordered = sorted(paths, key=lambda p: (-len(p), p))
    return [(p, new_resolved + p[len(old_resolved) :]) for p in ordered]


def _decode_rel(root: Path, enc: str) -> list[str]:
    """Find every existing relative dir path under root whose encoding is enc.

    The path encoding is lossy ('/', '.', and literal '-' all become '-'), so
    one encoded string can correspond to several real paths.  All matches are
    returned so the caller can detect ambiguity.
    """
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []

    matches: list[str] = []
    for entry in entries:
        encoded_name = SharedStore.encode_path(entry.name)
        if enc == encoded_name:
            matches.append(entry.name)
        elif enc.startswith(encoded_name + "-"):
            for sub in _decode_rel(entry, enc[len(encoded_name) + 1 :]):
                matches.append(f"{entry.name}/{sub}")
    return matches


def _collect_project_keys(profile_dirs: list[Path], shared_dir: Path) -> set[str]:
    """All real-path keys under projects{} across every profile's .claude.json."""
    keys: set[str] = set()
    for pdir in profile_dirs:
        if pdir == shared_dir:
            continue
        claude_json = pdir / ".claude.json"
        if not claude_json.is_file():
            continue
        try:
            data = json.loads(claude_json.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _log(f"  cannot parse {claude_json}: {e}")
            continue
        projects = data.get("projects")
        if isinstance(projects, dict):
            keys.update(k for k in projects if isinstance(k, str))
    return keys


def _discover_descendants(
    profile_dirs: list[Path],
    old_resolved: str,
    source_root: Path,
    known_keys: set[str],
) -> set[str]:
    """Every real project path equal to or under old_resolved that has data.

    Union of (a) .claude.json projects{} keys under OLD and (b) encoded
    ``projects/`` dir names that decode to a path under OLD.  Encoded names
    are never prefix-matched directly -- the encoding is ambiguous -- so each
    candidate is resolved back to a real path via the known keys plus
    filesystem checks under the moved tree.  A candidate that resolves to a
    sibling path (merely sharing the encoded prefix) is skipped; one that
    cannot be resolved to exactly one real path is a hard error.
    """
    descendants = {
        k for k in known_keys if k == old_resolved or k.startswith(old_resolved + "/")
    }

    old_encoded = SharedStore.encode_path(old_resolved)
    candidates: set[str] = set()
    for pdir in profile_dirs:
        projects = pdir / "projects"
        if not projects.is_dir():
            continue
        for entry in projects.iterdir():
            name = entry.name
            if (
                entry.is_dir()
                and name != old_encoded
                and name.startswith(old_encoded + "-")
            ):
                candidates.add(name)

    errors: list[str] = []
    for cand in sorted(candidates):
        resolved = {k for k in known_keys if SharedStore.encode_path(k) == cand}
        suffix_enc = cand[len(old_encoded) + 1 :]
        for rel in _decode_rel(source_root, suffix_enc):
            resolved.add(f"{old_resolved}/{rel}")

        if not resolved:
            errors.append(
                f"  projects/{cand}: no known project key and no directory "
                f"under the moved tree decodes to it"
            )
        elif len(resolved) > 1:
            listing = ", ".join(sorted(resolved))
            errors.append(f"  projects/{cand}: ambiguous, decodes to: {listing}")
        else:
            path = resolved.pop()
            if path == old_resolved or path.startswith(old_resolved + "/"):
                descendants.add(path)
            # else: a sibling that merely shares the encoded prefix
            # (e.g. OLD.ish or OLD-ish) -- not part of this move.

    if errors:
        raise ValueError(
            "cannot safely migrate: encoded project dirs under the source "
            "prefix could not be unambiguously decoded:\n" + "\n".join(errors)
        )
    return descendants


def _verify_destinations(
    migrations: list[tuple[str, str]],
    old_resolved: str,
    source_root: Path,
    new_resolved: str,
) -> None:
    """Hard-error unless every descendant's destination will exist on disk.

    ``source_root`` is the moved tree as it currently exists (OLD before the
    rename, NEW in post-hoc mode), so ``source_root/<suffix>`` existing now is
    equivalent to ``NEW/<suffix>`` existing at migration time.  On failure,
    every unresolvable descendant is listed and nothing is migrated.
    """
    missing = [
        f"  {mo} -> {mn}"
        for mo, mn in migrations
        if mo != old_resolved
        and not (source_root / mo[len(old_resolved) + 1 :]).is_dir()
    ]
    if missing:
        raise FileNotFoundError(
            "descendant project paths have no matching directory under "
            f"{new_resolved} -- nothing was migrated:\n" + "\n".join(missing)
        )


# ---------------------------------------------------------------------------
# Per-target mutation helpers
# ---------------------------------------------------------------------------


def _rename_project_dir(old_project: Path, new_project: Path, dry_run: bool) -> bool:
    """Rename old_project to new_project, merging when the target exists.

    Returns True when a rename or merge happened (or would happen in dry run).
    """
    if not old_project.is_dir():
        return False

    if new_project.exists():
        # Target already exists -- merge contents from old into new
        if dry_run:
            _log(f"  would merge {old_project} -> {new_project}")
            for item in sorted(old_project.iterdir()):
                dest = new_project / item.name
                if dest.exists():
                    _log(f"    would skip (already exists): {item.name}")
                else:
                    _log(f"    would move: {item.name}")
        else:
            _log(f"  merging {old_project} -> {new_project}")
            for item in sorted(old_project.iterdir()):
                dest = new_project / item.name
                if dest.exists():
                    _log(f"    skipping (already exists): {item.name}")
                else:
                    shutil.move(str(item), str(dest))
                    _log(f"    moved: {item.name}")
            # Remove the now-empty old directory
            try:
                old_project.rmdir()
            except OSError:
                _log(
                    f"  WARNING: could not remove {old_project} (not empty after merge)"
                )
        return True

    if dry_run:
        _log(f"  would rename {old_project} -> {new_project}")
        return True

    old_project.rename(new_project)
    _log(f"  renamed {old_project} -> {new_project}")
    return True


def _rewrite_prefixed_path(path: str, migrations: list[tuple[str, str]]) -> str:
    """Rewrite a real path equal to or under a migrated source path.

    ``migrations`` is longest-source-first, so the most specific mapping wins.
    """
    for mo, mn in migrations:
        if path == mo or path.startswith(mo + "/"):
            return mn + path[len(mo) :]
    return path


def _update_claude_json(
    path: Path,
    migrations: list[tuple[str, str]],
    dry_run: bool,
) -> tuple[int, int]:
    """Rename project keys and rewrite githubRepoPaths in one .claude.json.

    Project keys live under ``data["projects"]``; every key matching a
    migration source is renamed to its destination.  ``githubRepoPaths``
    values (repo -> list of local paths) equal to or under a migration source
    are rewritten too.  Returns ``(project_keys_updated, github_paths_updated)``.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _log(f"  cannot parse {path}: {e}")
        return 0, 0

    keys_updated = 0
    projects = data.get("projects")
    if isinstance(projects, dict):
        for mo, mn in migrations:
            if mo not in projects:
                continue
            if dry_run:
                _log(f"  would rename key {mo!r} -> {mn!r} in {path}")
            else:
                projects[mn] = projects.pop(mo)
                _log(f"  renamed key {mo!r} -> {mn!r} in {path}")
            keys_updated += 1

    github_updated = 0
    repo_paths = data.get("githubRepoPaths")
    if isinstance(repo_paths, dict):
        for repo, value in list(repo_paths.items()):
            # Values are lists of local paths; tolerate a bare string too.
            items = [value] if isinstance(value, str) else value
            if not isinstance(items, list):
                continue
            new_items = []
            changed = 0
            for item in items:
                new_item = (
                    _rewrite_prefixed_path(item, migrations)
                    if isinstance(item, str)
                    else item
                )
                if new_item != item:
                    changed += 1
                    verb = "would rewrite" if dry_run else "rewrote"
                    _log(
                        f"  {verb} githubRepoPaths[{repo!r}]: "
                        f"{item!r} -> {new_item!r} in {path}"
                    )
                new_items.append(new_item)
            if changed:
                github_updated += changed
                if not dry_run:
                    repo_paths[repo] = (
                        new_items[0] if isinstance(value, str) else new_items
                    )

    if (keys_updated or github_updated) and not dry_run:
        write_json_atomic(path, data)
    return keys_updated, github_updated


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_mv(
    ws: "Workspace",
    old_path: str,
    new_path: str,
    dry_run: bool = False,
    quiet: bool = False,
    post_hoc: bool = False,
) -> MvResult:
    """Rename a project directory and migrate Claude Code session data.

    In default mode, renames old_path to new_path on the filesystem and then
    migrates all session data.  With post_hoc=True, skips the filesystem rename
    (the directory was already renamed externally) and only migrates sessions.

    The migration is prefix-aware: every project keyed at old_path or nested
    under it (Claude Code projects inside the moved tree) is migrated to
    new_path plus the same relative suffix.  That covers the encoded
    ``projects/`` dirs, the ``projects{}`` keys and ``githubRepoPaths``
    entries in every profile's .claude.json, and the JSONL cwd references of
    every migrated project.  Each descendant's destination must exist on disk
    under new_path; otherwise the operation aborts before touching anything.
    """
    global _quiet
    _quiet = quiet
    result = MvResult()

    # 1. Resolve paths
    old_resolved = str(Path(old_path).expanduser().resolve())
    new_resolved = str(Path(new_path).expanduser().resolve())

    if old_resolved == new_resolved:
        raise ValueError(f"source and target are the same: {old_resolved}")

    if post_hoc:
        # Session-only migration: directory already renamed externally
        if not Path(new_resolved).is_dir():
            raise FileNotFoundError(
                f"target does not exist as a directory: {new_resolved}"
            )
        if Path(old_resolved).exists():
            raise FileExistsError(
                f"source still exists: {old_resolved} -- use 'mv' without --post-hoc to rename it"
            )
    else:
        # Rename mode: validate now, rename after descendant discovery so the
        # whole operation is check-then-act (nothing moves if discovery fails)
        if not Path(old_resolved).is_dir():
            raise FileNotFoundError(
                f"source does not exist as a directory: {old_resolved}"
            )
        if Path(new_resolved).exists():
            raise FileExistsError(f"target already exists: {new_resolved}")

    _log(f"moving {old_resolved} -> {new_resolved}")
    if dry_run:
        _log("DRY RUN -- no changes will be made")

    # 2. Compute encoded directory names
    old_encoded = SharedStore.encode_path(old_resolved)
    new_encoded = SharedStore.encode_path(new_resolved)
    _log(f"encoded: {old_encoded} -> {new_encoded}")

    # 3. Discover profile dirs
    profile_dirs = _discover_profile_dirs(ws)
    result.profiles_scanned = len(profile_dirs)
    _log(f"found {len(profile_dirs)} profile/shared dirs")
    shared_dir = ws.shared_dir

    # 4. Discover nested descendant projects and plan the migration.
    # The moved tree as it currently exists on disk: OLD before the rename
    # (default mode), NEW after it (post-hoc mode).  Both hold the same
    # contents, so decoding and destination checks against it are equivalent.
    source_root = (
        Path(old_resolved) if Path(old_resolved).is_dir() else Path(new_resolved)
    )
    known_keys = _collect_project_keys(profile_dirs, shared_dir)
    descendants = _discover_descendants(
        profile_dirs, old_resolved, source_root, known_keys
    )
    migrations = _plan_migrations(old_resolved, new_resolved, descendants)
    result.paths_migrated = len(migrations)
    for mo, mn in migrations:
        if mo != old_resolved:
            _log(f"  nested project: {mo} -> {mn}")

    # Atomic check-then-act: every descendant destination must exist on disk,
    # otherwise nothing is migrated at all.
    _verify_destinations(migrations, old_resolved, source_root, new_resolved)

    # 5. Rename the directory (default mode only)
    if not post_hoc:
        if dry_run:
            _log(f"would rename directory {old_resolved} -> {new_resolved}")
        else:
            try:
                Path(old_resolved).rename(new_resolved)
            except OSError as e:
                raise OSError(
                    f"failed to rename directory {old_resolved} -> {new_resolved}: {e}"
                ) from e

    # 6. Process projects/ in each profile dir, longest source path first
    for pdir in profile_dirs:
        projects = pdir / "projects"
        if not projects.is_dir():
            continue

        # 6a. Rename or merge each migrated project directory
        scan_dirs: list[Path] = []
        for mo, mn in migrations:
            old_project = projects / SharedStore.encode_path(mo)
            new_project = projects / SharedStore.encode_path(mn)
            if _rename_project_dir(old_project, new_project, dry_run):
                result.dirs_renamed += 1
            # After a real rename/merge, files live in new_project.  In
            # dry-run merge, files stay in both dirs -- scan both to get
            # accurate counts.  In dry-run simple rename, new_project doesn't
            # exist, so only old_project is scanned.
            if new_project.is_dir() and new_project not in scan_dirs:
                scan_dirs.append(new_project)
            if dry_run and old_project.is_dir() and old_project not in scan_dirs:
                scan_dirs.append(old_project)

        # 6b. Rewrite JSONL files in every migrated project dir.  Descendant
        # destinations are NEW + suffix, so replacing the parent prefix also
        # fixes every descendant path in one pass.
        for scan_dir in scan_dirs:
            for jsonl_path in scan_dir.rglob("*.jsonl"):
                # Skip history.jsonl -- append-only, not critical for resume
                if jsonl_path.name == "history.jsonl":
                    continue
                lines_fixed = _rewrite_jsonl_file(
                    jsonl_path,
                    old_resolved,
                    new_resolved,
                    dry_run,
                )
                if lines_fixed > 0:
                    result.files_rewritten += 1
                    result.lines_replaced += lines_fixed

    # 7. Update .claude.json in each profile dir (not shared): rename every
    # migrated projects{} key and rewrite githubRepoPaths entries
    for pdir in profile_dirs:
        if pdir == shared_dir:
            continue
        claude_json = pdir / ".claude.json"
        if claude_json.is_file():
            keys_updated, github_updated = _update_claude_json(
                claude_json, migrations, dry_run
            )
            result.project_keys_updated += keys_updated
            result.github_repo_paths_updated += github_updated

    # 8. Summary
    _log("summary")
    _log(f"  project paths migrated: {result.paths_migrated}")
    _log(f"  dirs renamed:           {result.dirs_renamed}")
    _log(f"  files rewritten:        {result.files_rewritten}")
    _log(f"  lines replaced:         {result.lines_replaced}")
    _log(f"  project keys updated:   {result.project_keys_updated}")
    _log(f"  githubRepoPaths fixed:  {result.github_repo_paths_updated}")
    _log(f"  profiles scanned:       {result.profiles_scanned}")
    if dry_run:
        _log("  (dry run -- nothing written)")

    return result
