"""Move one Claude Code session to another project directory's session store.

A session lives in the store dir named after the directory it belongs to:
``<projects>/<encoded directory>/<session>.jsonl``, with an optional
``<session>/`` folder beside it (tool results, subagent transcripts).  Claude
Code's own ``/cd`` moves a session by renaming both into the new directory's
store dir and appending one ``relocated`` record to the transcript; it reads a
session's directory as that record's ``relocatedCwd``, else as the first
``cwd`` the transcript recorded.  ``claudewheel move-session`` makes the same
move for a session that is not running, and also:

* rewrites the paths in the top-level transcript that point into the session's
  own store folder, in any spelling of ``.../projects/<old store dir>/<session>``,
  to the new store folder -- no other path, and no ``cwd`` field;
* keeps the top-level transcript's access and modification times;
* records a ``moved`` event in the session's lifecycle file;
* removes the source store dir when the move leaves it empty.

Every check runs, and every file is read, before the first change, so a
``--dry-run`` preview records exactly the changes a run would make.

Interrupted moves
-----------------

Before the first change the move writes a journal,
``~/.claudewheel/shared/session-moves/<session>.json`` (its shape is owned by
``.strictspec/session-move-journal.schema.toml``), records each step in it as
the step completes, and removes it at the end.  A rerun with the same session
and directory reads the journal and does the steps not yet done; each step
also recognizes on disk that it already happened, so a run stopped between a
step and its journal update still completes.  While a journal exists, the
session's store dirs are read as the journaled move (the transcript in one,
the folder still in the other is not a duplicate), and a move of the session
to any other directory is refused.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import effects, lifecycle, session_registry
from .lifecycle import SESSION_UUID_RE, MovedEvent
from .mv import _dump_record, _read_transcript
from .session import RELOCATED_RECORD_TYPE
from .session_stores import discover_profile_dirs, distinct_store_dirs
from .shared_store import SharedStore

if TYPE_CHECKING:
    from .workspace import Workspace

JOURNAL_FORMAT_VERSION = 1

#: The steps of a move, in the order they are done.
STEPS: tuple[str, ...] = (
    "transcript-moved",
    "folder-moved",
    "transcript-rewritten",
    "times-restored",
    "lifecycle-recorded",
    "source-removed",
)


class MoveSessionError(Exception):
    """The move is refused; nothing was changed by the run that raised it."""


@dataclass(frozen=True)
class Journal:
    """One unfinished move, as recorded in its journal file."""

    session: str
    source_store_dir: Path
    target_store_dir: Path
    target_cwd: str
    old_cwd: str | None
    transcript_atime_ns: int
    transcript_mtime_ns: int
    event_id: str
    event_at: str
    steps_done: tuple[str, ...] = field(default=())

    def to_json(self) -> str:
        data = {
            "format_version": JOURNAL_FORMAT_VERSION,
            "session": self.session,
            "source_store_dir": str(self.source_store_dir),
            "target_store_dir": str(self.target_store_dir),
            "target_cwd": self.target_cwd,
            "old_cwd": self.old_cwd,
            "transcript_atime_ns": self.transcript_atime_ns,
            "transcript_mtime_ns": self.transcript_mtime_ns,
            "event_id": self.event_id,
            "event_at": self.event_at,
            "steps_done": list(self.steps_done),
        }
        return json.dumps(data, indent=2) + "\n"


@dataclass(frozen=True)
class MoveResult:
    """What one run did (or, under ``--dry-run``, would do)."""

    source_store_dir: Path
    target_store_dir: Path
    target_cwd: str
    resumed: bool
    steps: tuple[str, ...]


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


def journal_path(ws: "Workspace", session: str) -> Path:
    """The journal file of a move of *session*."""
    return ws.shared.session_moves_dir / f"{session}.json"


def _validate_journal(text: str, where: Path) -> None:
    import strictspec

    from .strictspec_gen import session_move_journal_validator as validator

    raw = text.encode("utf-8")
    marker = strictspec.version_gate(validator._program, raw, "json")
    if not marker.ok:
        joined = "; ".join(d.message for d in marker.diagnostics)
        raise MoveSessionError(f"{where}: {joined}")
    _root, diags = validator.validate_bytes(raw, "json")
    if diags:
        joined = "; ".join(d.message for d in diags)
        raise MoveSessionError(f"{where}: {joined}")


def read_journal(path: Path) -> Journal | None:
    """The journal at *path*, or None when there is none; a damaged one is an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise MoveSessionError(f"cannot read the move journal {path}: {e}") from e
    _validate_journal(text, path)
    data = json.loads(text)
    return Journal(
        session=data["session"],
        source_store_dir=Path(data["source_store_dir"]),
        target_store_dir=Path(data["target_store_dir"]),
        target_cwd=data["target_cwd"],
        old_cwd=data["old_cwd"],
        transcript_atime_ns=data["transcript_atime_ns"],
        transcript_mtime_ns=data["transcript_mtime_ns"],
        event_id=data["event_id"],
        event_at=data["event_at"],
        steps_done=tuple(data["steps_done"]),
    )


def _write_journal(path: Path, journal: Journal, dry_run: bool) -> None:
    text = journal.to_json()
    _validate_journal(text, path)
    if effects.issue(dry_run):
        effects.write_text_atomic(path, text)


# ---------------------------------------------------------------------------
# Reading what is on disk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Holder:
    """A store dir holding something of the session."""

    store_dir: Path
    transcript: bool
    folder: bool

    def describe(self, session: str) -> str:
        held = [f"{session}.jsonl"] if self.transcript else []
        if self.folder:
            held.append(f"{session}/")
        return f"{self.store_dir}: {', '.join(held)}"


def _holders(stores: list[Path], session: str) -> list[_Holder]:
    """Every store dir, in every store, holding the session's transcript or folder."""
    found: list[_Holder] = []
    for projects in stores:
        for store_dir in sorted(p for p in projects.iterdir() if p.is_dir()):
            transcript = os.path.lexists(store_dir / f"{session}.jsonl")
            folder = os.path.lexists(store_dir / session)
            if transcript or folder:
                found.append(_Holder(store_dir, transcript, folder))
    return found


def _session_directory(text: str) -> str | None:
    """The session's directory as Claude Code reads it from its transcript.

    The ``relocatedCwd`` of the last ``relocated`` record, else the first
    ``cwd`` any record carries; None when the transcript has neither.
    """
    first_cwd: str | None = None
    relocated: str | None = None
    for line in text.split("\n"):
        if '"cwd"' not in line and '"relocatedCwd"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == RELOCATED_RECORD_TYPE:
            moved_to = record.get("relocatedCwd")
            if isinstance(moved_to, str) and moved_to:
                relocated = moved_to
        elif first_cwd is None:
            cwd = record.get("cwd")
            if isinstance(cwd, str) and cwd:
                first_cwd = cwd
    return relocated if relocated is not None else first_cwd


def _relocated_line(session: str, target_cwd: str) -> str:
    """Claude Code's relocated record, as its /cd writes it."""
    return _dump_record(
        {
            "type": RELOCATED_RECORD_TYPE,
            "sessionId": session,
            "relocatedCwd": target_cwd,
        }
    )


def _self_path_pattern(old_name: str, session: str) -> re.Pattern[str]:
    """Any spelling of a path into the session's store folder in *old_name*."""
    return re.compile(
        "/projects/" + re.escape(old_name) + "/" + re.escape(session) + r"(?![\w-])"
    )


def _rewrite_value(value: Any, key: str, pattern: re.Pattern[str], new: str) -> Any:
    if isinstance(value, str):
        # A cwd field records where the session ran; it is never rewritten.
        return value if key == "cwd" else pattern.sub(new, value)
    if isinstance(value, list):
        return [_rewrite_value(item, key, pattern, new) for item in value]
    if isinstance(value, dict):
        return {
            pattern.sub(new, k): _rewrite_value(v, k, pattern, new)
            for k, v in value.items()
        }
    return value


def _moved_transcript(
    text: str, *, session: str, old_name: str, new_name: str, target_cwd: str
) -> str:
    """*text* with its self-paths moved to *new_name* and the relocated record appended.

    A line that changes is re-serialized the way Claude Code writes it; every
    other line, including one that is not JSON, is kept byte for byte.
    """
    pattern = _self_path_pattern(old_name, session)
    replacement = f"/projects/{new_name}/{session}"
    needle = f"/{old_name}/{session}"
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if needle not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        rewritten = _rewrite_value(record, "", pattern, replacement)
        if rewritten != record:
            lines[index] = _dump_record(rewritten)
    moved = "\n".join(lines)
    lead = "\n" if moved and not moved.endswith("\n") else ""
    return f"{moved}{lead}{_relocated_line(session, target_cwd)}\n"


def _ends_with_relocated(text: str, session: str, target_cwd: str) -> bool:
    return text.rstrip("\n").rsplit("\n", 1)[-1] == _relocated_line(session, target_cwd)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def _check_not_running(
    ws: "Workspace", session: str, config_dirs: dict[str, Path], now_ms: int
) -> None:
    """Refuse a session whose process runs, or whose lifecycle says it is starting."""
    found = session_registry.read_profile_records(config_dirs)
    for name, _config_dir, record in found:
        if record.session_id == session and record.live:
            raise MoveSessionError(
                f"session {session} is running (pid {record.pid}, profile {name}): "
                "a running session cannot be moved"
            )
    life = lifecycle.summarize(
        lifecycle.read_session(
            lifecycle.session_file(ws.shared.lifecycle_dir, session)
        ),
        session=session,
    )
    state = lifecycle.derive_state(
        life,
        live=False,
        verified=False,
        status=None,
        registry_present=any(r.session_id == session for _, _, r in found),
        now_ms=now_ms,
    )
    if state == "starting":
        raise MoveSessionError(
            f"session {session} is starting: its lifecycle records a start "
            "and its process has not registered yet, so it may be running"
        )


def _refers_to(data: dict[str, Any], session: str) -> bool:
    if session in (data.get("sessionId"), data.get("resumeSessionId")):
        return True
    link = data.get("linkScanPath")
    if isinstance(link, str):
        parts = Path(link).parts
        return session in parts or f"{session}.jsonl" in parts
    return False


def _check_no_jobs(session: str, config_dirs: dict[str, Path]) -> None:
    """Refuse when a Claude Code background-job record refers to the session."""
    seen: set[Path] = set()
    naming: list[str] = []
    for config_dir in config_dirs.values():
        for state in sorted((config_dir / "jobs").glob("*/state.json")):
            real = state.resolve()
            if real in seen:
                continue
            seen.add(real)
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as e:
                raise MoveSessionError(
                    f"cannot read the background job record {state}, so whether "
                    f"it refers to session {session} is unknown: {e}"
                ) from e
            if isinstance(data, dict) and _refers_to(data, session):
                name = data.get("name")
                label = f" ({name})" if isinstance(name, str) and name else ""
                naming.append(
                    f"  background job {state}{label} refers to session {session}"
                )
    if naming:
        raise MoveSessionError(
            f"session {session} has Claude Code background jobs, which would "
            "lose it if it moved:\n" + "\n".join(naming)
        )


def _check_no_inbound_links(stores: list[Path], session: str, folder: Path) -> None:
    """Refuse when a symlink in another session's folder points into *folder*."""
    if not folder.is_dir():
        return
    inside = os.path.realpath(folder)
    links: list[str] = []
    for projects in stores:
        for store_dir in sorted(p for p in projects.iterdir() if p.is_dir()):
            for other in sorted(store_dir.iterdir()):
                if other.name == session or not SESSION_UUID_RE.match(other.name):
                    continue
                if other.is_symlink() or not other.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(other):
                    for name in sorted(dirnames + filenames):
                        link = os.path.join(dirpath, name)
                        if not os.path.islink(link):
                            continue
                        target = os.readlink(link)
                        real = os.path.realpath(os.path.join(dirpath, target))
                        if real == inside or real.startswith(inside + os.sep):
                            links.append(f"  {link} -> {target}")
    if links:
        raise MoveSessionError(
            f"symlinks in other sessions' folders point into {folder}, and "
            "moving it would break them:\n" + "\n".join(links)
        )


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------


def _resolve_target(directory: str) -> Path:
    target = Path(directory).expanduser().resolve()
    if not target.is_dir():
        raise MoveSessionError(f"the target is not an existing directory: {target}")
    return target


def _locate_fresh(
    holders: list[_Holder], session: str, target: Path, stores: list[Path]
) -> tuple[Path, Path]:
    """The source and target store dirs of a move that has no journal."""
    if not holders:
        listing = ", ".join(str(p) for p in stores)
        raise MoveSessionError(f"no session store holds {session}; looked in {listing}")
    if len(holders) > 1:
        lines = [f"  {h.describe(session)}" for h in holders]
        for h in holders:
            if h.store_dir.name == SharedStore.encode_path(str(target)):
                held = h.describe(session).split(": ", 1)[1]
                lines.append(
                    f"the target's store dir {h.store_dir} already holds {held}"
                )
        raise MoveSessionError(
            f"more than one session store dir holds {session}, and `claude "
            "--resume` finds no session that is held twice:\n" + "\n".join(lines)
        )
    [holder] = holders
    if not holder.transcript:
        raise MoveSessionError(
            f"{holder.store_dir} holds {session}/ but not {session}.jsonl, "
            "so there is no session transcript to move"
        )
    source = holder.store_dir
    target_store = source.parent / SharedStore.encode_path(str(target))
    if target_store.name == source.name:
        raise MoveSessionError(
            f"session {session} is already in {source}, the store dir of "
            f"{target}; there is nothing to move"
        )
    return source, target_store


def _locate_journaled(
    holders: list[_Holder], session: str, journal: Journal, path: Path, target: Path
) -> None:
    """Refuse a rerun the journal does not describe."""
    if journal.target_cwd != str(target):
        raise MoveSessionError(
            f"a move of session {session} to {journal.target_cwd} was interrupted "
            f"and its journal {path} is still there; finish it first with "
            f"`claudewheel move-session {session} {journal.target_cwd}`"
        )
    journaled = {journal.source_store_dir, journal.target_store_dir}
    strays = [h for h in holders if h.store_dir not in journaled]
    if strays or not any(h.transcript for h in holders):
        found = "\n".join(f"  {h.describe(session)}" for h in holders) or "  nothing"
        raise MoveSessionError(
            f"the journal {path} records a move of session {session} from "
            f"{journal.source_store_dir} to {journal.target_store_dir}, but the "
            f"session stores hold:\n{found}"
        )


def move_session(
    ws: "Workspace", session: str, directory: str, *, dry_run: bool
) -> MoveResult:
    """Move *session* to *directory*'s session store, or finish a journaled move.

    Raises :class:`MoveSessionError` for every refusal, before anything is
    changed.  An effect that fails part way raises its own error and leaves
    the journal, so a rerun finishes the move.
    """
    if not SESSION_UUID_RE.match(session):
        raise MoveSessionError(
            f"{session!r} is not a session id: a session id is a full lowercase "
            "UUID (8-4-4-4-12 hex digits), as Claude Code records it"
        )
    target = _resolve_target(directory)
    jpath = journal_path(ws, session)
    journal = read_journal(jpath)

    profile_dirs = discover_profile_dirs(ws)
    stores = distinct_store_dirs(profile_dirs)
    holders = _holders(stores, session)

    if journal is None:
        source, target_store = _locate_fresh(holders, session, target, stores)
    else:
        _locate_journaled(holders, session, journal, jpath, target)
        source, target_store = journal.source_store_dir, journal.target_store_dir

    config_dirs = {
        p.name: ws.profiles.path_for(p.name) for p in ws.profiles.enumerate()
    }
    now_ms = time.time_ns() // 1_000_000
    _check_not_running(ws, session, config_dirs, now_ms)
    _check_no_jobs(session, config_dirs)
    folder = source / session if (source / session).exists() else target_store / session
    _check_no_inbound_links(stores, session, folder)

    # Read everything the steps need, before the first change.
    old_transcript = source / f"{session}.jsonl"
    new_transcript = target_store / f"{session}.jsonl"
    at_source = old_transcript.exists()
    current = old_transcript if at_source else new_transcript
    # Stat before reading: the read itself may update the access time.
    st = os.stat(current)
    text = _read_transcript(current)
    if journal is None:
        journal = Journal(
            session=session,
            source_store_dir=source,
            target_store_dir=target_store,
            target_cwd=str(target),
            old_cwd=_session_directory(text),
            transcript_atime_ns=st.st_atime_ns,
            transcript_mtime_ns=st.st_mtime_ns,
            event_id=lifecycle.new_event_id(),
            event_at=lifecycle.now_timestamp(now_ms),
        )
        resumed = False
    else:
        resumed = True
    rewritten_already = not at_source and _ends_with_relocated(
        text, session, str(target)
    )
    new_text = _moved_transcript(
        text,
        session=session,
        old_name=source.name,
        new_name=target_store.name,
        target_cwd=str(target),
    )
    lifecycle_file = lifecycle.session_file(ws.shared.lifecycle_dir, session)
    recorded = any(
        e.id == journal.event_id for e in lifecycle.read_session(lifecycle_file)
    )
    moved_names = {f"{session}.jsonl", session}
    source_left = (
        sorted(p.name for p in source.iterdir() if p.name not in moved_names)
        if source.is_dir()
        else None
    )

    performed: list[str] = []

    def done(step: str) -> None:
        nonlocal journal
        assert journal is not None
        journal = replace(journal, steps_done=(*journal.steps_done, step))
        _write_journal(jpath, journal, dry_run)
        performed.append(step)

    if not resumed:
        if effects.issue(dry_run):
            effects.mkdir(jpath.parent, parents=True, exist_ok=True)
        _write_journal(jpath, journal, dry_run)
    else:
        effects.info(f"Finishing the interrupted move recorded in {jpath}")

    if "transcript-moved" not in journal.steps_done:
        if at_source:
            if effects.issue(dry_run):
                effects.mkdir(target_store, parents=True, exist_ok=True)
                effects.rename(old_transcript, new_transcript)
        done("transcript-moved")

    if "folder-moved" not in journal.steps_done:
        if os.path.lexists(source / session):
            if effects.issue(dry_run):
                effects.mkdir(target_store, parents=True, exist_ok=True)
                effects.rename(source / session, target_store / session)
        done("folder-moved")

    if "transcript-rewritten" not in journal.steps_done:
        if not rewritten_already:
            if effects.issue(dry_run):
                effects.write_text_atomic(new_transcript, new_text)
        done("transcript-rewritten")

    if "times-restored" not in journal.steps_done:
        if effects.issue(dry_run):
            effects.set_times(
                new_transcript,
                atime_ns=journal.transcript_atime_ns,
                mtime_ns=journal.transcript_mtime_ns,
            )
        done("times-restored")

    if "lifecycle-recorded" not in journal.steps_done:
        if not recorded and effects.issue(dry_run):
            lifecycle.append_event(
                ws.shared.lifecycle_dir,
                MovedEvent(
                    id=journal.event_id,
                    at=journal.event_at,
                    session=session,
                    source="user",
                    old_cwd=journal.old_cwd,
                    new_cwd=journal.target_cwd,
                    old_transcript=str(old_transcript),
                    new_transcript=str(new_transcript),
                ),
            )
        done("lifecycle-recorded")

    if "source-removed" not in journal.steps_done:
        if source_left == [] and effects.issue(dry_run):
            effects.rmdir(source)
        done("source-removed")

    if effects.issue(dry_run):
        effects.remove(jpath)

    effects.info(
        f"{'Would move' if dry_run else 'Moved'} session {session} to {target}: "
        f"{old_transcript} -> {new_transcript}"
    )
    if source_left:
        effects.info(
            f"The store dir {source} {'would stay' if dry_run else 'stays'}, "
            f"holding: {', '.join(source_left)}"
        )
    return MoveResult(
        source_store_dir=source,
        target_store_dir=target_store,
        target_cwd=str(target),
        resumed=resumed,
        steps=tuple(performed),
    )
