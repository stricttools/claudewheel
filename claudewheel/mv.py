"""Move session data after a project directory rename."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import effects
from .effects import write_json_atomic, write_text_atomic
from .profile_store import CLAUDE_GLOBAL_CONFIG_NAME
from .session import recorded_store_cwds
from .session_stores import discover_profile_dirs, distinct_store_dirs
from .shared_store import PROJECT_DIR_NAME_LIMIT, SharedStore

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


def _read_transcript(path: Path) -> str:
    """Read one session JSONL file, raising an error that names it."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise OSError(f"cannot read {path}: {e}") from e


def _transcripts_to_rewrite(scan_dir: Path) -> list[Path]:
    """Every JSONL file the rewrite visits in one migrated store dir.

    Nested files (subagent transcripts) included; ``history.jsonl`` is
    skipped, being append-only and not needed to resume.
    """
    return [p for p in sorted(scan_dir.rglob("*.jsonl")) if p.name != "history.jsonl"]


def _check_transcripts_readable(
    stores: list[Path], migrations: list[tuple[str, str]]
) -> None:
    """Refuse the move, before anything changes, if a transcript it would rewrite is unreadable.

    Reads every file the rewrite step will read, in the same way, so an
    unreadable one is found while nothing has been renamed yet.  Every
    unreadable file is listed.
    """
    failures: list[str] = []
    for projects in stores:
        for mo, mn in migrations:
            for name in (SharedStore.encode_path(mo), SharedStore.encode_path(mn)):
                scan_dir = projects / name
                if not scan_dir.is_dir():
                    continue
                for path in _transcripts_to_rewrite(scan_dir):
                    try:
                        _read_transcript(path)
                    except OSError as e:
                        failures.append(f"  {e}")
    if failures:
        raise OSError(
            "cannot migrate, nothing was changed: session files the move "
            "must rewrite cannot be read:\n" + "\n".join(failures)
        )


def _check_merges_complete(
    stores: list[Path], migrations: list[tuple[str, str]]
) -> None:
    """Refuse the move, before anything changes, if a merge would strand entries.

    When a migrated path's store dir and its destination's both exist, the
    first is merged into the second and removed; an entry name present in
    both cannot move, so the old store dir could not be removed.  Every such
    pair is listed with the names both hold.
    """
    conflicts: list[str] = []
    for projects in stores:
        for mo, mn in migrations:
            old_project = projects / SharedStore.encode_path(mo)
            new_project = projects / SharedStore.encode_path(mn)
            if not (old_project.is_dir() and new_project.exists()):
                continue
            shared = sorted(
                item.name
                for item in old_project.iterdir()
                if (new_project / item.name).exists()
            )
            if shared:
                conflicts.append(
                    f"  {old_project} -> {new_project}: both hold " + ", ".join(shared)
                )
    if conflicts:
        raise FileExistsError(
            "cannot migrate, nothing was changed: merging these store dirs "
            "would leave the old one non-empty:\n" + "\n".join(conflicts)
        )


# Transcript fields whose string value (or list of string values) is one
# filesystem path.  Such a value is rewritten only when it IS the old path or
# lies under it, so ``/x/foo.bak`` in a path field survives a move of
# ``/x/foo``.  Every other string is free text (see ``_free_text_pattern``).
_PATH_FIELDS = frozenset(
    {
        "cwd",
        "file_path",
        "filePath",
        "filename",
        "filesChanged",
        "notebook_path",
        "path",
        "persistedOutputPath",
        "planFilePath",
        "realParentDir",
        "relocatedCwd",
        "workingDirectory",
    }
)

# Objects whose keys are filesystem paths (a file-history snapshot maps each
# tracked file to its backup).
_PATH_KEYED_FIELDS = frozenset({"trackedFileBackups"})


def _free_text_pattern(old_path: str) -> re.Pattern[str]:
    """Occurrences of *old_path* in free text that end at a path boundary.

    An occurrence followed by a letter, digit, ``-``, or ``_`` names a
    different path (``/x/foobar``, ``/x/foo-bar``) and is not matched;
    anything else after it (``/``, a quote, whitespace, punctuation, or the
    end of the string) ends the path, so it is.
    """
    return re.compile(re.escape(old_path) + r"(?![\w-])")


def _rewrite_path_value(value: str, old_path: str, new_path: str) -> str:
    if value == old_path or value.startswith(old_path + "/"):
        return new_path + value[len(old_path) :]
    return value


def _rewrite_record(
    value: Any, old_path: str, new_path: str, free_text: re.Pattern[str], key: str
) -> Any:
    """*value* with every path equal to or under *old_path* moved to *new_path*.

    *key* is the name of the field holding *value* ("" for none); it selects
    the path-field rule over the free-text rule.
    """
    if isinstance(value, str):
        if key in _PATH_FIELDS:
            return _rewrite_path_value(value, old_path, new_path)
        return free_text.sub(lambda _: new_path, value)
    if isinstance(value, list):
        return [
            _rewrite_record(item, old_path, new_path, free_text, key) for item in value
        ]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if key in _PATH_KEYED_FIELDS:
                new_k = _rewrite_path_value(k, old_path, new_path)
            else:
                new_k = free_text.sub(lambda _: new_path, k)
            out[new_k] = _rewrite_record(v, old_path, new_path, free_text, k)
        return out
    return value


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _dump_record(record: Any) -> str:
    """Serialize one rewritten transcript record the way Claude Code writes it.

    Compact separators and raw non-ASCII, as ``JSON.stringify`` does; a lone
    surrogate (which ``JSON.stringify`` escapes) is escaped too, so the line
    stays valid UTF-8.
    """
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _rewrite_jsonl_file(
    path: Path,
    old_path: str,
    new_path: str,
    dry_run: bool,
) -> int:
    """Move every path equal to or under old_path to new_path in a JSONL file.

    Each newline-terminated line is parsed as JSON.  Path fields
    (``_PATH_FIELDS``, and the keys of ``_PATH_KEYED_FIELDS``) are rewritten
    when they are old_path or under it; every other string, keys included,
    is rewritten where old_path ends at a path boundary
    (``_free_text_pattern``).  A line that changes is re-serialized; every
    other line is kept byte for byte.  A line that is not JSON (a live
    session's partial last line) is kept as it is, as the store's cwd reader
    skips it.

    Returns the number of lines where a replacement was made.  An unreadable
    file is a hard error naming it (``_check_transcripts_readable`` refuses
    the same condition before the move changes anything).
    """
    # Split on "\n" only: str.splitlines() also splits on U+2028 and kin,
    # which JSON strings may hold raw.
    lines = _read_transcript(path).split("\n")
    free_text = _free_text_pattern(old_path)

    replaced = 0
    new_lines: list[str] = []
    for line in lines:
        if old_path not in line:
            new_lines.append(line)
            continue
        try:
            record = json.loads(line)
        except ValueError:
            new_lines.append(line)
            continue
        rewritten = _rewrite_record(record, old_path, new_path, free_text, "")
        if rewritten == record:
            new_lines.append(line)
        else:
            new_lines.append(_dump_record(rewritten))
            replaced += 1

    if replaced > 0:
        if dry_run:
            _log(f"  would rewrite {path} ({replaced} lines)")
        else:
            write_text_atomic(path, "\n".join(new_lines))
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


def _decode_rel(root: Path, root_real: str, name: str) -> list[str]:
    """Every existing dir under *root* whose store-dir name is *name*.

    *root* is the moved tree as it exists on disk and *root_real* the real
    path its contents are recorded under; a relative dir ``rel`` matches when
    ``encode_path(root_real + "/" + rel) == name``.  The encoding is lossy
    (see ``SharedStore.encode_path_untruncated``), so one name can match
    several dirs; all matches are returned so the caller can detect
    ambiguity.  A name Claude Code truncated and hash-suffixed decodes the
    same way, because each match is confirmed by encoding its full path.
    """
    limit = PROJECT_DIR_NAME_LIMIT
    head = name[:limit]
    matches: list[str] = []

    def walk(directory: Path, rel: str) -> None:
        try:
            entries = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for entry in entries:
            entry_rel = f"{rel}/{entry.name}" if rel else entry.name
            full = f"{root_real}/{entry_rel}"
            if SharedStore.encode_path(full) == name:
                matches.append(entry_rel)
            # Descend only where a deeper path could still produce the name.
            deeper = (SharedStore.encode_path_untruncated(full) + "-")[:limit]
            if head.startswith(deeper):
                walk(entry, entry_rel)

    walk(root, "")
    return matches


def _names_under(old_resolved: str) -> Callable[[str], bool]:
    """A test for store-dir names that could belong to a path under OLD.

    A path under OLD encodes to ``untruncated(OLD) + "-" + ...``; its store
    name keeps the first ``PROJECT_DIR_NAME_LIMIT`` characters of that, so
    the test compares that many characters.  It admits siblings that merely
    share the encoded prefix (``OLD.ish``); resolution sorts them out.
    """
    prefix = SharedStore.encode_path_untruncated(old_resolved) + "-"
    if len(prefix) <= PROJECT_DIR_NAME_LIMIT:
        return lambda name: name.startswith(prefix)
    cut = prefix[:PROJECT_DIR_NAME_LIMIT]
    return lambda name: len(name) > PROJECT_DIR_NAME_LIMIT and name.startswith(cut)


def _read_claude_json(path: Path) -> dict[str, Any]:
    """Read one profile's .claude.json, hard-erroring when it cannot be read.

    Swallowing an unreadable registry is not an option here: during discovery
    it turns decodable descendants into spurious "undecodable orphan" errors
    that name the wrong cause, and during the update pass it lets the
    migration report success while leaving that profile's ``projects{}`` at
    the old path.  Both readers refuse instead, per the module's uniform
    hard-error contract.
    """
    try:
        text = path.read_text()
    except OSError as e:
        raise OSError(f"cannot read {path}: {e}") from e
    try:
        return cast("dict[str, Any]", json.loads(text))
    except json.JSONDecodeError as e:
        raise ValueError(f"cannot parse {path}: {e}") from e


def _collect_project_keys(profile_dirs: list[Path], shared_dir: Path) -> set[str]:
    """All real-path keys under projects{} across every profile's .claude.json.

    An unreadable or malformed ``.claude.json`` is a hard error (see
    ``_read_claude_json``), not a profile silently contributing zero keys.
    """
    keys: set[str] = set()
    for pdir in profile_dirs:
        if pdir == shared_dir:
            continue
        claude_json = pdir / CLAUDE_GLOBAL_CONFIG_NAME
        if not claude_json.is_file():
            continue
        data = _read_claude_json(claude_json)
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

    Union of (a) .claude.json projects{} keys under OLD and (b) store dirs
    whose name could encode a path under OLD.  Encoded names are never
    prefix-matched directly -- the encoding is lossy -- so every such
    candidate is resolved back to real paths from all three proofs at once:
    registry keys that encode to its name, directories under the moved tree
    that encode to it (``_decode_rel``), and the cwds its own sessions
    recorded that encode to it (``recorded_store_cwds``).  Exactly one
    distinct path must result.  A candidate resolving to a sibling (merely
    sharing the encoded prefix, e.g. ``OLD-ish``) is not part of the move;
    one resolving to several paths, or to none, is a hard error listing
    every such candidate, raised before anything is changed.
    """
    descendants = {
        k for k in known_keys if k == old_resolved or k.startswith(old_resolved + "/")
    }

    old_encoded = SharedStore.encode_path(old_resolved)
    under_old = _names_under(old_resolved)
    candidates: dict[str, list[Path]] = {}
    for projects in distinct_store_dirs(profile_dirs):
        for entry in projects.iterdir():
            name = entry.name
            if entry.is_dir() and name != old_encoded and under_old(name):
                candidates.setdefault(name, []).append(entry)

    errors: list[str] = []
    for cand in sorted(candidates):
        store_dirs = candidates[cand]
        resolved = {k for k in known_keys if SharedStore.encode_path(k) == cand}
        for rel in _decode_rel(source_root, old_resolved, cand):
            resolved.add(f"{old_resolved}/{rel}")
        for store_dir in store_dirs:
            resolved |= recorded_store_cwds(store_dir)

        where = ", ".join(str(d) for d in store_dirs)
        if not resolved:
            errors.append(
                f"  {where}: no project key encodes to it, no directory under "
                f"the moved tree encodes to it, and no recorded cwd in its "
                f"sessions encodes to it"
            )
        elif len(resolved) > 1:
            listing = ", ".join(sorted(resolved))
            errors.append(f"  {where}: ambiguous, its name encodes: {listing}")
        else:
            path = resolved.pop()
            if path == old_resolved or path.startswith(old_resolved + "/"):
                descendants.add(path)
            # else: a sibling that merely shares the encoded prefix
            # (e.g. OLD.ish or OLD-ish) -- not part of this move.

    if errors:
        raise ValueError(
            "cannot safely migrate, nothing was changed: store dirs whose "
            "name could belong to a path under the source do not resolve to "
            "exactly one real path:\n" + "\n".join(errors)
        )
    return descendants


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
        # Target already exists -- merge contents from old into new. The moves
        # are issued in both modes: under --dry-run the effects chokepoint
        # records them into the would-do log rather than performing them, so
        # this narration and the framework's preview describe the same run.
        _log(
            f"  {'would merge' if dry_run else 'merging'} {old_project} -> {new_project}"
        )
        # Name collisions were refused before any change
        # (``_check_merges_complete``), so every entry moves.
        for item in sorted(old_project.iterdir()):
            if effects.issue(dry_run):
                effects.move(item, new_project / item.name)
            _log(f"    {'would move' if dry_run else 'moved'}: {item.name}")
        # Remove the now-empty old directory
        try:
            if effects.issue(dry_run):
                effects.rmdir(old_project)
        except OSError as e:
            raise OSError(
                f"cannot remove {old_project} after merging it into {new_project}: {e}"
            ) from e
        return True

    if effects.issue(dry_run):
        effects.rename(old_project, new_project)
    _log(f"  {'would rename' if dry_run else 'renamed'} {old_project} -> {new_project}")
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

    An unreadable or malformed file is a hard error (see ``_read_claude_json``):
    returning ``(0, 0)`` would let the migration report success while this
    profile's registry still points at the old path.
    """
    data = _read_claude_json(path)

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

    The migration is prefix-aware: every project proven to be old_path or
    nested under it (see ``_discover_descendants``) is migrated to new_path
    plus the same relative suffix.  That covers the encoded ``projects/``
    dirs, the ``projects{}`` keys and ``githubRepoPaths`` entries in every
    profile's .claude.json, and the JSONL cwd references of every migrated
    project.  A proven descendant whose directory no longer exists (session
    data outliving a deleted project) is relabeled all the same, so nothing
    in the store stays recorded under old_path.  Every check runs before the
    first change: a refusal leaves everything as it was.
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
            if Path(new_resolved).is_dir():
                # The signature of an interrupted run: the rename happened,
                # the session migration did not.  --post-hoc completes it.
                raise FileNotFoundError(
                    f"source does not exist as a directory: {old_resolved} "
                    f"(the target {new_resolved} does exist -- the rename "
                    f"already happened, so finish the session migration with: "
                    f"claudewheel mv --post-hoc {old_resolved} {new_resolved})"
                )
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
    profile_dirs = discover_profile_dirs(ws)
    result.profiles_scanned = len(profile_dirs)
    _log(f"found {len(profile_dirs)} profile/shared dirs")
    shared_dir = ws.shared_dir

    # 4. Discover nested descendant projects and plan the migration.
    # The moved tree as it currently exists on disk: OLD before the rename
    # (default mode), NEW after it (post-hoc mode).  Both hold the same
    # contents, so decoding directories against it is equivalent.
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

    stores = distinct_store_dirs(profile_dirs)
    _check_transcripts_readable(stores, migrations)
    _check_merges_complete(stores, migrations)

    # 5. Rename the directory (default mode only)
    if not post_hoc:
        if dry_run:
            _log(f"would rename directory {old_resolved} -> {new_resolved}")
        try:
            if effects.issue(dry_run):
                effects.rename(Path(old_resolved), new_resolved)
        except OSError as e:
            raise OSError(
                f"failed to rename directory {old_resolved} -> {new_resolved}: {e}"
            ) from e

    # 6. Process each distinct projects/ store, longest source path first
    for projects in stores:
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
            for jsonl_path in _transcripts_to_rewrite(scan_dir):
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
        claude_json = pdir / CLAUDE_GLOBAL_CONFIG_NAME
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
