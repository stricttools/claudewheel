"""Import Claude Code session data from an external directory into the shared store."""

from __future__ import annotations

import re
import shutil
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path

from .constants import SHARED_DIR, encode_path
from .fsutil import write_text_atomic
from .session import get_session_cwd

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Directories whose children are keyed by session UUID.
SIMPLE_DIRS = ("todos", "session-env", "file-history", "tasks")

PREFIX = "[import]"


@dataclass
class ImportResult:
    """Counters tracking the outcome of an import operation."""

    sessions_imported: int = 0
    sessions_reided: int = 0
    artifacts_copied: int = 0
    lines_rewritten: int = 0
    paste_files_copied: int = 0
    collisions: list[str] = field(default_factory=list)


def _log(msg: str) -> None:
    print(f"{PREFIX} {msg}")


def _is_uuid(name: str) -> bool:
    return UUID_RE.match(name) is not None


def _normalize_cwd(cwd: str) -> str:
    """Normalize a cwd for comparison.

    Case-folds the drive letter (``C:\\`` -> ``c:\\``), strips trailing
    ``/`` and ``\\``.
    """
    s = cwd.rstrip("/\\")
    # Drive-letter paths: c:\..., C:\..., c:/..., C:/...
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = s[0].lower() + s[1:]
    return s


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------


def _build_rewriters(
    mappings: list[tuple[str, str]],
) -> list[tuple[re.Pattern[str], str]]:
    """Compile regex patterns for JSON-level path rewriting.

    Returns a list of ``(pattern, to_path)`` pairs, sorted longest
    ``from_path`` first so that longer prefixes match before shorter ones.
    """
    # Sort longest from_path first to prevent prefix shadowing.
    sorted_mappings = sorted(mappings, key=lambda m: len(m[0]), reverse=True)

    rewriters: list[tuple[re.Pattern[str], str]] = []
    for from_path, to_path in sorted_mappings:
        # --- Pattern A: JSON-escaped backslashes (Windows paths in JSON) ---
        # In JSON, ``c:\Users\m`` is stored as ``c:\\Users\\m``.
        # In the regex string, each literal ``\`` needs to be escaped once
        # more, so a single JSON ``\\`` becomes ``\\\\`` in the regex.
        parts_bs = re.split(r"[\\/]", from_path)
        escaped_bs = r"\\\\".join(re.escape(p) for p in parts_bs if p)
        # Drive letter: make the first char case-insensitive.
        if len(parts_bs) > 0 and len(parts_bs[0]) == 2 and parts_bs[0][1] == ":":
            letter = parts_bs[0][0]
            escaped_bs = f"[{letter.lower()}{letter.upper()}]" + escaped_bs[1:]
        # Allow optional deeper segments: \\segment continuations.
        # Negative lookahead rejects JSON unicode escapes (\\uXXXX).
        # Segments must not contain [, ], or space (ANSI escapes, prose).
        pattern_bs = escaped_bs + r'((?:\\\\(?!u[0-9a-fA-F]{4})[^\\"\[\] ]+)*)'
        rewriters.append((re.compile(pattern_bs), to_path))

        # --- Pattern B: Forward-slash variant ---
        parts_fs = re.split(r"[\\/]", from_path)
        escaped_fs = "/".join(re.escape(p) for p in parts_fs if p)
        if from_path.startswith("/"):
            escaped_fs = "/" + escaped_fs
        if len(parts_fs) > 0 and len(parts_fs[0]) == 2 and parts_fs[0][1] == ":":
            letter = parts_fs[0][0]
            escaped_fs = f"[{letter.lower()}{letter.upper()}]" + escaped_fs[1:]
        pattern_fs = escaped_fs + r'((?:/[^/\\"\[\] ]+)*)'
        rewriters.append((re.compile(pattern_fs), to_path))

    return rewriters


def _apply_rewrites(
    line: str,
    rewriters: list[tuple[re.Pattern[str], str]],
) -> tuple[str, bool]:
    """Apply all path rewriters to a single line of text.

    Returns ``(new_line, changed)``.
    """
    changed = False
    for pattern, to_path in rewriters:
        def _replace(m: re.Match[str], _to: str = to_path) -> str:
            suffix = m.group(1)
            if suffix:
                # Convert ``\\seg1\\seg2`` or ``/seg1/seg2`` to ``/seg1/seg2``.
                normalized = suffix.replace("\\\\", "/").replace("\\", "/")
                return _to + normalized
            return _to
        new_line = pattern.sub(_replace, line)
        if new_line != line:
            changed = True
            line = new_line
    return line, changed


def _rewrite_jsonl(
    src_path: Path,
    dst_path: Path,
    rewriters: list[tuple[re.Pattern[str], str]],
    old_uuid: str | None,
    new_uuid: str | None,
    dry_run: bool,
) -> int:
    """Read a JSONL file, apply path rewrites (and optional UUID reid), write atomically.

    Returns the number of lines where at least one replacement was made.
    """
    try:
        lines = src_path.read_text().splitlines(keepends=True)
    except OSError as e:
        _log(f"  cannot read {src_path}: {e}")
        return 0

    rewritten_count = 0
    new_lines: list[str] = []
    for line in lines:
        result_line, changed = _apply_rewrites(line, rewriters)
        if old_uuid and new_uuid and old_uuid != new_uuid:
            new_result = result_line.replace(
                f'"sessionId":"{old_uuid}"', f'"sessionId":"{new_uuid}"',
            )
            if new_result == result_line:
                # Try with spaces around colon (some formatters).
                new_result = result_line.replace(
                    f'"sessionId": "{old_uuid}"', f'"sessionId": "{new_uuid}"',
                )
            if new_result != result_line:
                changed = True
                result_line = new_result
        if changed:
            rewritten_count += 1
        new_lines.append(result_line)

    if not dry_run:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(dst_path, "".join(new_lines))

    return rewritten_count


# ---------------------------------------------------------------------------
# Session bundle
# ---------------------------------------------------------------------------


@dataclass
class _SessionBundle:
    """A session's JSONL file plus its optional companion directory."""

    uuid: str
    jsonl_path: Path
    companion_dir: Path | None  # <uuid>/ directory if it exists
    cwd: str  # extracted cwd from the JSONL
    source_encoded_dir: str  # the encoded project directory name in the source


def _scan_source(source: Path) -> list[_SessionBundle]:
    """Walk ``<source>/projects/*/`` and collect session bundles."""
    projects_dir = source / "projects"
    bundles: list[_SessionBundle] = []

    for encoded_dir in sorted(projects_dir.iterdir()):
        if not encoded_dir.is_dir():
            continue
        for entry in sorted(encoded_dir.iterdir()):
            name = entry.name
            if not name.endswith(".jsonl"):
                continue
            stem = name[:-6]
            if not _is_uuid(stem):
                continue
            if entry.stat().st_size == 0:
                continue
            cwd = get_session_cwd(entry)
            if cwd is None:
                raise ValueError(
                    f"cannot extract cwd from {entry} -- "
                    f"file has no cwd field in the first lines"
                )
            companion = encoded_dir / stem
            bundles.append(_SessionBundle(
                uuid=stem,
                jsonl_path=entry,
                companion_dir=companion if companion.is_dir() else None,
                cwd=cwd,
                source_encoded_dir=encoded_dir.name,
            ))

    return bundles


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_import(
    source: str,
    mappings: list[tuple[str, str]],
    reid: bool = False,
    dry_run: bool = False,
) -> ImportResult:
    """Import session data from an external directory into the shared store.

    Parameters
    ----------
    source:
        Path to the external directory (must contain a ``projects/`` subdir).
    mappings:
        List of ``(from_path, to_path)`` pairs.  ``to_path`` values are
        already resolved to absolute paths on the current machine.
    reid:
        If ``True``, assign new UUIDs to sessions that collide with existing
        ones in the shared store.
    dry_run:
        If ``True``, report what would happen without writing anything.
    """
    result = ImportResult()
    source_path = Path(source)

    # 1. Validate source.
    if not (source_path / "projects").is_dir():
        raise FileNotFoundError(
            f"source directory does not contain a projects/ subdirectory: {source_path}"
        )

    if dry_run:
        _log("DRY RUN -- no changes will be made")

    # 2. Scan source.
    _log(f"scanning {source_path}")
    bundles = _scan_source(source_path)
    _log(f"found {len(bundles)} session bundles")

    if not bundles:
        _log("nothing to import")
        return result

    # 3. Build cwd-to-sessions map.
    cwd_to_bundles: dict[str, list[_SessionBundle]] = {}
    for b in bundles:
        key = _normalize_cwd(b.cwd)
        cwd_to_bundles.setdefault(key, []).append(b)

    # 4. Validate mappings -- every discovered cwd must have a mapping.
    norm_mappings = {_normalize_cwd(f): t for f, t in mappings}
    unmapped = sorted(set(cwd_to_bundles.keys()) - set(norm_mappings.keys()))
    if unmapped:
        # Build a helpful error listing original (non-normalized) cwds.
        originals: list[str] = []
        for key in unmapped:
            for b in cwd_to_bundles[key]:
                originals.append(b.cwd)
                break  # one example per cwd is enough
        raise ValueError(
            "unmapped cwds found in source -- add --map for each:\n"
            + "\n".join(f"  {o}" for o in originals)
        )

    # Build the normalized-cwd -> to_path lookup.
    cwd_to_target: dict[str, str] = norm_mappings

    # 5. Compute target directories and detect collisions.
    shared_projects = SHARED_DIR / "projects"
    uuid_target_map: dict[str, tuple[Path, str | None]] = {}
    #   uuid -> (target_dir, new_uuid_or_None)

    for b in bundles:
        norm = _normalize_cwd(b.cwd)
        to_path = cwd_to_target[norm]
        target_dir = shared_projects / encode_path(to_path)
        target_jsonl = target_dir / f"{b.uuid}.jsonl"
        target_companion = target_dir / b.uuid

        collision = target_jsonl.exists() or target_companion.exists()
        if collision:
            if reid:
                new_uuid = str(uuid_mod.uuid4())
                uuid_target_map[b.uuid] = (target_dir, new_uuid)
            else:
                result.collisions.append(
                    f"{b.uuid} -> {target_dir} (from {b.cwd})"
                )
                uuid_target_map[b.uuid] = (target_dir, None)
        else:
            uuid_target_map[b.uuid] = (target_dir, None)

    # 6. Early return on collisions without reid.
    if result.collisions and not reid:
        _log(f"{len(result.collisions)} collision(s) detected, --reid not set")
        for c in result.collisions:
            _log(f"  COLLISION: {c}")
        return result

    # 7. Build path rewriters.
    rewriters = _build_rewriters(mappings)

    # 8. Copy with rewriting.
    for b in bundles:
        target_dir, new_uuid = uuid_target_map[b.uuid]
        effective_uuid = new_uuid if new_uuid else b.uuid
        is_reided = new_uuid is not None

        if is_reided:
            result.sessions_reided += 1

        target_jsonl = target_dir / f"{effective_uuid}.jsonl"

        # Rewrite and copy the main JSONL file.
        if dry_run:
            _log(f"  would copy {b.jsonl_path} -> {target_jsonl}")
        else:
            _log(f"  copying {b.jsonl_path} -> {target_jsonl}")

        lines_rewritten = _rewrite_jsonl(
            b.jsonl_path, target_jsonl, rewriters,
            old_uuid=b.uuid if is_reided else None,
            new_uuid=new_uuid,
            dry_run=dry_run,
        )
        result.lines_rewritten += lines_rewritten
        result.sessions_imported += 1

        # Companion directory (<uuid>/ with subagents/, tool-results/, etc.).
        if b.companion_dir is not None:
            target_companion = target_dir / effective_uuid
            if not dry_run:
                target_companion.mkdir(parents=True, exist_ok=True)

            for item in sorted(b.companion_dir.rglob("*")):
                if not item.is_file():
                    continue
                rel = item.relative_to(b.companion_dir)
                # If reiding, update the relative path if it contains the old UUID.
                dst_rel = rel
                if is_reided:
                    dst_rel = Path(str(rel).replace(b.uuid, effective_uuid))
                dst = target_companion / dst_rel

                if item.suffix == ".jsonl":
                    if dry_run:
                        _log(f"    would rewrite {rel}")
                    else:
                        _log(f"    rewriting {rel}")
                    lines_rewritten = _rewrite_jsonl(
                        item, dst, rewriters,
                        old_uuid=b.uuid if is_reided else None,
                        new_uuid=new_uuid,
                        dry_run=dry_run,
                    )
                    result.lines_rewritten += lines_rewritten
                else:
                    if dry_run:
                        _log(f"    would copy {rel}")
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(item), str(dst))
                        _log(f"    copied {rel}")
                result.artifacts_copied += 1

        # Scan SIMPLE_DIRS in the source root for UUID-keyed artifacts.
        for d in SIMPLE_DIRS:
            _copy_simple_artifacts(
                source_path, d, b.uuid, effective_uuid, is_reided,
                result, dry_run,
            )

    # 9. Paste cache.
    paste_src = source_path / "paste-cache"
    if paste_src.is_dir():
        paste_dst = SHARED_DIR / "paste-cache"
        if not dry_run:
            paste_dst.mkdir(parents=True, exist_ok=True)
        for item in sorted(paste_src.iterdir()):
            if not item.is_file():
                continue
            dst = paste_dst / item.name
            if dst.exists():
                continue  # content-hash-keyed, skip duplicates
            if dry_run:
                _log(f"  would copy paste-cache/{item.name}")
            else:
                shutil.copy2(str(item), str(dst))
                _log(f"  copied paste-cache/{item.name}")
            result.paste_files_copied += 1

    # 10. Summary.
    _log("summary")
    _log(f"  sessions imported:   {result.sessions_imported}")
    _log(f"  sessions re-IDed:    {result.sessions_reided}")
    _log(f"  artifacts copied:    {result.artifacts_copied}")
    _log(f"  lines rewritten:     {result.lines_rewritten}")
    _log(f"  paste files copied:  {result.paste_files_copied}")
    if dry_run:
        _log("  (dry run -- nothing written)")

    return result


def _copy_simple_artifacts(
    source_root: Path,
    dirname: str,
    old_uuid: str,
    effective_uuid: str,
    is_reided: bool,
    result: ImportResult,
    dry_run: bool,
) -> None:
    """Copy UUID-keyed artifacts from a simple directory (todos, session-env, etc.)."""
    src_dir = source_root / dirname
    if not src_dir.is_dir():
        return

    dst_base = SHARED_DIR / dirname

    if dirname == "todos":
        # Todos files: <uuid>-agent-<uuid>.json
        for item in sorted(src_dir.iterdir()):
            if not item.is_file():
                continue
            name = item.name
            if not (name.startswith(f"{old_uuid}-agent-") and name.endswith(".json")):
                continue
            if is_reided:
                new_name = name.replace(old_uuid, effective_uuid)
            else:
                new_name = name
            dst = dst_base / new_name
            if dst.exists():
                continue
            if dry_run:
                _log(f"  would copy {dirname}/{new_name}")
            else:
                dst_base.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(dst))
                _log(f"  copied {dirname}/{new_name}")
            result.artifacts_copied += 1
    else:
        # session-env/, file-history/, tasks/: direct child dirs named as UUIDs
        artifact = src_dir / old_uuid
        if not artifact.exists():
            return
        dst = dst_base / effective_uuid
        if dst.exists():
            return
        if dry_run:
            _log(f"  would copy {dirname}/{effective_uuid}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if artifact.is_dir():
                shutil.copytree(str(artifact), str(dst))
            else:
                shutil.copy2(str(artifact), str(dst))
            _log(f"  copied {dirname}/{effective_uuid}")
        result.artifacts_copied += 1
