"""Read Claude Code's per-session registry into typed, liveness-checked records.

Claude Code registers every process it starts under
``<config_dir>/sessions/<pid>.json`` -- one JSON document per process, written
at startup and unlinked on exit.  A crash, a kill -9 or a machine reboot leaves
the file behind, so the directory is a set of *claims*, not a set of live
sessions, and the operating system recycles PIDs freely.  This module is the one
place claudewheel turns those claims into records it can act on.

The record shape (observed live, and against Claude Code 2.1.226's own writer)::

    {"pid":1485597,"sessionId":"4d97ca01-...","cwd":"/home/m/Projects",
     "startedAt":1786521494735,"procStart":"654274470","version":"2.1.226",
     "peerProtocol":1,"kind":"interactive","entrypoint":"cli",
     "messagingSocketPath":"/run/user/1000/cc-socks/1485597.sock",
     "name":"projects-9a","nameSource":"derived","status":"busy",
     "updatedAt":1786540262239,"statusUpdatedAt":1786540262239}

Two fields carry a start time and they are not interchangeable.  ``startedAt``
is wall-clock milliseconds -- useful for display, useless for identity, because
a reboot resets nothing and two processes can share a millisecond.
``procStart`` is the kernel's own start-time token for that process (field 22 of
``/proc/<pid>/stat``, in clock ticks since boot), which is exactly what
distinguishes "PID 1485597, the process that wrote this file" from "PID 1485597,
whatever the kernel handed that number to afterwards".  A record is live only
when its PID exists *and* that token still matches -- the phantom filter.

Where the token cannot be read the filter cannot run.  ``/proc`` does not exist
outside Linux, and Claude Code records ``procStartFt`` (a ``ps -o lstart=``
string) instead on those platforms; claudewheel does not read that field, so
there liveness degrades to plain PID existence.  That is the same answer Claude
Code's own comparison gives when either side of the pair is unavailable, and it
is stated here rather than hidden: on Linux, where the launcher actually runs,
the filter is exact.

Kinds and what they mean for policy
-----------------------------------

``kind`` is one of ``interactive``, ``bg``, ``daemon`` and ``daemon-worker``
(Claude Code derives it from ``CLAUDE_CODE_SESSION_KIND`` and defaults to
``interactive``).  Only an interactive session is a human sitting in front of
the profile, and only that blocks a delete or a rename -- a background job or a
daemon worker holding the profile is not a reason to refuse, per the program's
ruling that deletion offers the user a choice about those rather than a veto.

Only those three background kinds are read as background.  An unlabelled record
and a record carrying a kind claudewheel has never heard of -- a future Claude
Code kind, a typo -- are both read as interactive: the conservative direction,
so nothing can silently become deletable by wearing a name this module does not
recognize.  Widening the background set is a deliberate edit to
``BACKGROUND_KINDS``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import effects

#: The registry directory inside a Claude Code config dir.
SESSIONS_DIRNAME = "sessions"

#: The kind of a session a human is sitting in front of.
KIND_INTERACTIVE = "interactive"

#: The kinds Claude Code writes for work that is not a human's terminal.
BACKGROUND_KINDS = frozenset({"bg", "daemon", "daemon-worker"})


@dataclass(frozen=True)
class SessionRecord:
    """One registry file, parsed, with its liveness already resolved.

    ``live`` is the phantom-filtered answer: the PID exists and, where the
    kernel start token is available on both sides, still names the process that
    wrote the file.  ``proc_start`` is kept so a caller can tell "no token was
    recorded" from "the token matched"; it is normalized by
    :func:`recorded_token`, so a blank field arrives here as None -- absent,
    which is the reading that cannot make a live process look dead.
    """

    path: Path
    pid: int
    kind: str
    live: bool
    session_id: str | None = None
    cwd: str | None = None
    status: str | None = None
    name: str | None = None
    version: str | None = None
    started_at: int | None = None
    proc_start: str | None = None

    @property
    def interactive(self) -> bool:
        """True when this record is a human's session rather than background work.

        The test is "not one of the known background kinds", not "equal to
        ``interactive``": a kind claudewheel has never heard of is read the
        conservative way, so it can never silently become deletable.
        """
        return self.kind not in BACKGROUND_KINDS


def process_start_token(pid: int) -> str | None:
    """The kernel start-time token of the live process *pid*, or None.

    None means "no answer available", which covers a dead process, a
    ``/proc`` claudewheel may not read, and a platform without ``/proc`` at all.
    The parse mirrors Claude Code's: split *after* the last ``)`` so a process
    whose name contains spaces or parentheses cannot shift the field index.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    tail = stat[stat.rfind(")") + 1 :].split()
    # tail[0] is field 3 (state), so field 22 (starttime) is tail[19].
    if len(tail) < 20:
        return None
    return tail[19]


def pid_exists(pid: int) -> bool:
    """True when *pid* names a process this machine currently has.

    The package's one liveness probe: :mod:`claudewheel.processes` binds its
    ``alive`` name to this function rather than growing a second one.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # effects: exempt -- signal 0 probes, it does not signal
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just belongs to another user.
        return True
    except OSError:
        return False
    return True


def recorded_token(value: str | None) -> str | None:
    """*value* as a start token, or None when the record carries none.

    An empty or blank string is read as ABSENT, never as a token that fails to
    match.  Read as a mismatch it would be the worst possible answer: a running
    process would come out "provably dead", so a prune would delete its file
    and the delete and rename guards would stop seeing it.  Absent is the
    conservative reading -- liveness degrades to plain PID existence, which is
    the same answer a platform without ``/proc`` gets.
    """
    if value is None:
        return None
    return value.strip() or None


def is_live(pid: int, proc_start: str | None) -> bool:
    """Apply the phantom filter to one claim.

    Public because the answer is needed after the registry has been read, too:
    a snapshot taken when a screen opened says nothing about the moment the
    user acts on it, and the pid may by then name a different process.  This is
    the package's one implementation of that question.

    *proc_start* is normalized by :func:`recorded_token` first, so a caller
    holding a raw field value gets the same answer as one holding a parsed
    record's.
    """
    if not pid_exists(pid):
        return False
    proc_start = recorded_token(proc_start)
    if proc_start is None:
        return True
    actual = process_start_token(pid)
    if actual is None:
        return True
    return actual == proc_start


def _text(value: object) -> str | None:
    """A string field, or None when absent or the wrong type."""
    return value if isinstance(value, str) else None


def _parse(path: Path) -> SessionRecord | None:
    """Parse one registry file, or None when it is not one.

    Everything unreadable is skipped rather than raised: the directory belongs
    to another program, and a torn write from a session starting up must not
    take down a delete guard.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_pid = data.get("pid")
    if isinstance(raw_pid, int) and not isinstance(raw_pid, bool):
        pid = raw_pid
    elif path.stem.isdigit():
        # The filename IS the PID; a record missing the field is still a claim.
        pid = int(path.stem)
    else:
        return None
    proc_start = recorded_token(_text(data.get("procStart")))
    started_at = data.get("startedAt")
    return SessionRecord(
        path=path,
        pid=pid,
        kind=_text(data.get("kind")) or KIND_INTERACTIVE,
        live=is_live(pid, proc_start),
        session_id=_text(data.get("sessionId")),
        cwd=_text(data.get("cwd")),
        status=_text(data.get("status")),
        name=_text(data.get("name")),
        version=_text(data.get("version")),
        started_at=started_at if isinstance(started_at, int) else None,
        proc_start=proc_start,
    )


def _still_the_same_file(record: SessionRecord) -> bool:
    """True when *record*'s path still holds the very record *record* was read from.

    Identity is the pair that names a process for good: the PID and the kernel
    start token recorded alongside it.  Everything else in the document -- the
    status, the name, the two clocks -- is rewritten by a running session as it
    works, so comparing it would refuse to prune files that are genuinely
    stale.

    Anything that is not that exact pair is a no: a document rewritten by
    another process, a rewrite caught half-written, a file already gone.
    """
    current = _parse(record.path)
    if current is None:
        return False
    return current.pid == record.pid and current.proc_start == record.proc_start


def read_records(config_dir: Path) -> list[SessionRecord]:
    """Every parseable registry record under *config_dir*, live or not.

    Sorted by PID so callers and their tests get a stable order.  A missing
    ``sessions/`` directory is an empty registry, not an error.
    """
    sessions_dir = Path(config_dir) / SESSIONS_DIRNAME
    if not sessions_dir.is_dir():
        return []
    records = []
    try:
        entries = sorted(sessions_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.suffix != ".json" or not entry.is_file():
            continue
        record = _parse(entry)
        if record is not None:
            records.append(record)
    records.sort(key=lambda r: r.pid)
    return records


def live_records(config_dir: Path) -> list[SessionRecord]:
    """The records under *config_dir* whose processes are really running."""
    return [r for r in read_records(config_dir) if r.live]


def live_interactive_records(config_dir: Path) -> list[SessionRecord]:
    """The live records that are a human's session rather than background work."""
    return [r for r in live_records(config_dir) if r.interactive]


def prune(records: Iterable[SessionRecord]) -> list[SessionRecord]:
    """Delete the registry files of *records* that are provably dead, and return them.

    Three rules, and the last two are why the first is safe:

    * **Only a provably dead record is pruned.** Liveness is re-probed here
      rather than read off the record's own ``live`` field: the field is as old
      as the snapshot the caller is holding, and a screen the user sat in front
      of for a minute has an answer from a minute ago.  "Provably dead" is
      :func:`is_live` saying no -- the PID is gone, or it exists and carries a
      different kernel start token, so the process the file describes is gone
      whatever now wears its number.  Where no token is available on either
      side the filter cannot run, :func:`is_live` says yes, and the file stays:
      not proven dead is not pruned.
    * **Only the file the record was read from is unlinked.** Re-probing the
      *process* is not enough, because the file is a second thing that can
      change underneath the snapshot: the kernel may hand the PID to a new
      Claude Code session, which writes its own record over the same
      ``<pid>.json``.  The stale record's token then mismatches, the record
      reads provably dead, and the unlink would take the *live* session's file
      -- blinding the delete and rename guards to a session running right now.
      So the path is re-read immediately before the unlink and removed only
      when it still holds that record's own PID and start token
      (:func:`_still_the_same_file`).
    * **An unparseable file is never pruned**, at both ends: this works from
      records, and a record only exists for a file :func:`read_records` could
      parse; and a file that has since become unparseable fails the identity
      re-read.  That is what protects a file *being written right now* by a
      session starting up, whether the write began before the snapshot or after
      it.  Pruning must never be rewritten to walk the directory itself.

    Only the caller that actually removed a file reports it pruned: a file
    another screen already removed has no identity left to match, so it is
    skipped, as is one that cannot be removed.  Neither stops the rest.
    """
    pruned: list[SessionRecord] = []
    for record in records:
        if is_live(record.pid, record.proc_start):
            continue
        if not _still_the_same_file(record):
            continue
        try:
            effects.remove(record.path, missing_ok=True)
        except OSError:
            continue
        pruned.append(record)
    return pruned


def has_live_interactive(config_dir: Path) -> bool:
    """True when a human's session is live in *config_dir*.

    The predicate both the delete guard and the rename guard read.
    """
    return any(r.interactive and r.live for r in read_records(config_dir))
