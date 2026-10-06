# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.4.0
# schema:              claudewheel-session-move-journal (format_version 1)
# regenerate:          strictspec gen --manifest strictspec.toml
#
# Released under the MIT license (unencumbered). This file is machine-generated;
# edit the schema and regenerate, never this file.
# ruff: noqa
from __future__ import annotations

from dataclasses import dataclass, replace

import strictspec
from strictspec import Diagnostic, Value

# GENERATED_BY is the strictspec release that produced this file. It is
# INFORMATIONAL: pairing is on GENERATED_CODE_FORMAT below, so a later release
# of the runtime reads this file unchanged, and no tool may derive a dependency
# floor from this string.
GENERATED_BY = "0.4.0"
# GENERATED_CODE_FORMAT is the shape of generated code this file was written to.
# The runtime pairing guard hard-errors unless this format is one the linked
# runtime reads; the remedy is regeneration.
GENERATED_CODE_FORMAT = 2
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "session-move-journal.schema.toml": "# strictspec schema for one claudewheel SESSION MOVE JOURNAL.\n# Source of truth for the Python side: claudewheel/session_move.py.\n# One document = one JSON file = one session move that has not finished.\n#\n# WHAT THE STORE IS: one file per session being moved, at\n# `~/.claudewheel/shared/session-moves/<session-uuid>.json`, written by\n# `claudewheel move-session` before it changes anything and removed when the\n# move finishes. While it exists, the move was interrupted: a rerun with the\n# same session and directory reads it and completes the steps not yet done,\n# and a move of the same session to any other directory is refused.\n#\n# EVERY FIELD IS PRESENT. A fact that is not known is written as `null`.\n\nname = \"claudewheel-session-move-journal\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"json\"\nrole = \"schema\"\nroot = \"SessionMoveJournal\"\ntargets = [\"python\"]\ndescription = \"One session move that has not finished: where the session moves, what it was before, and which steps are done.\"\n\n[types.SessionMoveJournal]\ntype = \"record\"\ndescription = \"One session move, recorded before its first change and updated as each step completes.\"\n\n[types.SessionMoveJournal.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Document format_version marker. Every journal claudewheel writes carries 1.\"\n\n[types.SessionMoveJournal.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session being moved. Also the name of the file.\"\n\n[types.SessionMoveJournal.fields.source_store_dir]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The session store dir the session is moved out of (a projects/<encoded directory> path).\"\n\n[types.SessionMoveJournal.fields.target_store_dir]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The session store dir the session is moved into, in the same projects directory.\"\n\n[types.SessionMoveJournal.fields.target_cwd]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The directory the session belongs to after the move, as the command resolved it.\"\n\n[types.SessionMoveJournal.fields.old_cwd]\ntype = \"nullable\"\nrequired = true\ndescription = \"The directory the session belonged to before the move, as Claude Code read it then. Null when its transcript recorded none.\"\n[types.SessionMoveJournal.fields.old_cwd.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.SessionMoveJournal.fields.transcript_atime_ns]\ntype = \"integer\"\nrequired = true\ndescription = \"The transcript's access time before the move, in nanoseconds since the epoch; the move puts it back.\"\n\n[types.SessionMoveJournal.fields.transcript_mtime_ns]\ntype = \"integer\"\nrequired = true\ndescription = \"The transcript's modification time before the move, in nanoseconds since the epoch; the move puts it back.\"\n\n[types.SessionMoveJournal.fields.event_id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The id of the lifecycle `moved` event the move records, chosen before the first change so a rerun can tell whether it was written.\"\n\n[types.SessionMoveJournal.fields.event_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"The `at` of that lifecycle event (RFC 3339 UTC, millisecond precision).\"\n\n[types.SessionMoveJournal.fields.steps_done]\ntype = \"array\"\nrequired = true\ndescription = \"The steps of the move already done, in the order they were done.\"\n[types.SessionMoveJournal.fields.steps_done.item]\ntype = \"enum\"\nvalues = [\"transcript-moved\", \"folder-moved\", \"transcript-rewritten\", \"times-restored\", \"lifecycle-recorded\", \"source-removed\"]\n",
}
_EMBEDDED_MAIN_FILE = "session-move-journal.schema.toml"

# Pairing: this file's generated-code format must be one the runtime reads. This
# runs at import, so a runtime that cannot read it hard-errors before any
# validation is attempted.
strictspec.require_generated_code_format(GENERATED_CODE_FORMAT, GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[SessionMoveJournal | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[SessionMoveJournal | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the constraint
    vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_SessionMoveJournal(v), result.diagnostics


def validate_value(v: Value) -> tuple[SessionMoveJournal | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_SessionMoveJournal(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class SessionMoveJournal:
    """Frozen typed binding of the "SessionMoveJournal" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    session: str
    source_store_dir: str
    target_store_dir: str
    target_cwd: str
    old_cwd: str | None
    transcript_atime_ns: int
    transcript_mtime_ns: int
    event_id: str
    event_at: str
    steps_done: list[str]

    def with_format_version(self, v: int) -> SessionMoveJournal:
        return replace(self, format_version=v)

    def with_session(self, v: str) -> SessionMoveJournal:
        return replace(self, session=v)

    def with_source_store_dir(self, v: str) -> SessionMoveJournal:
        return replace(self, source_store_dir=v)

    def with_target_store_dir(self, v: str) -> SessionMoveJournal:
        return replace(self, target_store_dir=v)

    def with_target_cwd(self, v: str) -> SessionMoveJournal:
        return replace(self, target_cwd=v)

    def with_old_cwd(self, v: str | None) -> SessionMoveJournal:
        return replace(self, old_cwd=v)

    def with_transcript_atime_ns(self, v: int) -> SessionMoveJournal:
        return replace(self, transcript_atime_ns=v)

    def with_transcript_mtime_ns(self, v: int) -> SessionMoveJournal:
        return replace(self, transcript_mtime_ns=v)

    def with_event_id(self, v: str) -> SessionMoveJournal:
        return replace(self, event_id=v)

    def with_event_at(self, v: str) -> SessionMoveJournal:
        return replace(self, event_at=v)

    def with_steps_done(self, v: list[str]) -> SessionMoveJournal:
        return replace(self, steps_done=v)


def _bind_SessionMoveJournal(v: Value) -> SessionMoveJournal | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_session = v.field("session")
    f_source_store_dir = v.field("source_store_dir")
    f_target_store_dir = v.field("target_store_dir")
    f_target_cwd = v.field("target_cwd")
    f_old_cwd = v.field("old_cwd")
    f_transcript_atime_ns = v.field("transcript_atime_ns")
    f_transcript_mtime_ns = v.field("transcript_mtime_ns")
    f_event_id = v.field("event_id")
    f_event_at = v.field("event_at")
    f_steps_done = v.field("steps_done")
    return SessionMoveJournal(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        source_store_dir=(f_source_store_dir[0].string()[0] if f_source_store_dir[1] else ""),
        target_store_dir=(f_target_store_dir[0].string()[0] if f_target_store_dir[1] else ""),
        target_cwd=(f_target_cwd[0].string()[0] if f_target_cwd[1] else ""),
        old_cwd=((None if f_old_cwd[0].is_null() else f_old_cwd[0].string()[0]) if f_old_cwd[1] else None),
        transcript_atime_ns=(f_transcript_atime_ns[0].int()[0] if f_transcript_atime_ns[1] else 0),
        transcript_mtime_ns=(f_transcript_mtime_ns[0].int()[0] if f_transcript_mtime_ns[1] else 0),
        event_id=(f_event_id[0].string()[0] if f_event_id[1] else ""),
        event_at=(f_event_at[0].datetime()[0] if f_event_at[1] else ""),
        steps_done=([e.string()[0] for e in f_steps_done[0].items()] if f_steps_done[1] else []),
    )


