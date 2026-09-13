# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.2.4
# schema:              claudewheel-lifecycle-event (format_version 1)
# regenerate:          strictspec gen --manifest strictspec.toml
#
# Released under the MIT license (unencumbered). This file is machine-generated;
# edit the schema and regenerate, never this file.
# ruff: noqa
from __future__ import annotations

from dataclasses import dataclass, replace

import strictspec
from strictspec import Diagnostic, Value

# GENERATED_BY is the strictspec release that produced this file. The runtime
# pairing guard hard-errors unless it matches the linked runtime exactly.
GENERATED_BY = "0.2.4"
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "lifecycle-event.schema.toml": "# strictspec schema for one claudewheel SESSION LIFECYCLE line.\n# Source of truth for the Python side: claudewheel/lifecycle.py.\n# One document = one JSONL line = one lifecycle event.\n#\n# WHAT THE STORE IS: an append-only log, one file per Claude Code session\n# (`~/.claudewheel/shared/lifecycle/<session-uuid>.jsonl`), recording what\n# happened to that session -- that it started, that it was named, that the user\n# marked it, that it ended. Claude Code's own per-session registry is written\n# only while a process lives and says nothing once it is gone; this store is\n# what remains afterwards, and it is the only place a dead session's fate is\n# recorded.\n#\n# SCOPE: strictspec owns the raw DOCUMENT SHAPE -- the per-line format_version\n# marker, the `kind` discriminator and its arm set, field types, enums, required\n# fields, and unknown-key rejection. claudewheel keeps what strictspec cannot\n# see: ordering events by `at`, latest-wins reading of a file, the cross-event\n# rule that an `ended` older than the newest `started` is a previous run's, and\n# the state a reader derives from all of it together.\n#\n# EVERY FIELD IS PRESENT ON EVERY LINE IT BELONGS TO. A fact that is not known\n# is written as `null`, never omitted: absence would be a second spelling of\n# \"unknown\" that a writer could produce by forgetting a key, and a reader could\n# not tell the two apart.\n\nname = \"claudewheel-lifecycle-event\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"jsonl\"\nrole = \"schema\"\nroot = \"LifecycleEvent\"\ntargets = [\"python\"]\ndescription = \"One JSONL session lifecycle line: a single fact about one Claude Code session, discriminated by `kind`.\"\n\n# ---------------------------------------------------------------------------\n# Event arms\n#\n# Each arm repeats the common fields (format_version, id, at, session, source,\n# kind) rather than factoring them into a wrapper: `kind` is the discriminator\n# and must sit at the document root for the union to select an arm without an\n# extra nesting level.\n# ---------------------------------------------------------------------------\n\n[types.StartedEvent]\ntype = \"record\"\ndescription = \"A session began running, under a stated config directory and working directory.\"\n\n[types.StartedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.StartedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.StartedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.StartedEvent.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session uuid. Also the name of the file this line is in.\"\n\n[types.StartedEvent.fields.source]\ntype = \"enum\"\nrequired = true\nvalues = [\"hook\", \"user\", \"sweep\"]\ndescription = \"Who wrote the line: a Claude Code hook script, a user keypress in claudewheel, or claudewheel's own observation pass.\"\n\n[types.StartedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"started\"\nrequired = true\n\n[types.StartedEvent.fields.cwd]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The working directory the session runs in.\"\n\n[types.StartedEvent.fields.config_dir]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The Claude Code config directory the session runs under -- a claudewheel profile directory, or ~/.claude for an unmanaged launch.\"\n\n[types.StartedEvent.fields.profile]\ntype = \"nullable\"\nrequired = true\ndescription = \"The claudewheel profile name, when claudewheel launched the session. Null for a launch claudewheel did not make.\"\n[types.StartedEvent.fields.profile.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.StartedEvent.fields.claude_version]\ntype = \"nullable\"\nrequired = true\ndescription = \"The Claude Code version the session runs, when the writer knows it.\"\n[types.StartedEvent.fields.claude_version.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.StartedEvent.fields.model]\ntype = \"nullable\"\nrequired = true\ndescription = \"The model the session was launched with, when the writer knows it.\"\n[types.StartedEvent.fields.model.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.StartedEvent.fields.permissions]\ntype = \"nullable\"\nrequired = true\ndescription = \"claudewheel's permissions selection for the launch. Null for a launch claudewheel did not make.\"\n[types.StartedEvent.fields.permissions.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.StartedEvent.fields.entry]\ntype = \"enum\"\nrequired = true\nvalues = [\"startup\", \"resume\", \"clear\", \"compact\", \"fork\"]\ndescription = \"How the session was entered, in Claude Code's own SessionStart `source` vocabulary, used verbatim.\"\n\n[types.StartedEvent.fields.transcript]\ntype = \"nullable\"\nrequired = true\ndescription = \"Path to the session's transcript JSONL, when the writer knows it.\"\n[types.StartedEvent.fields.transcript.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.StartedEvent.fields.pid]\ntype = \"nullable\"\nrequired = true\ndescription = \"The session process id, when the writer knows it. A pid is reused by the kernel, so it identifies a process only together with `at`.\"\n[types.StartedEvent.fields.pid.inner]\ntype = \"integer\"\n\n[types.EndedEvent]\ntype = \"record\"\ndescription = \"A session stopped running, either by exiting or by dying without one.\"\n\n[types.EndedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.EndedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.EndedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.EndedEvent.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session uuid. Also the name of the file this line is in.\"\n\n[types.EndedEvent.fields.source]\ntype = \"enum\"\nrequired = true\nvalues = [\"hook\", \"user\", \"sweep\"]\ndescription = \"Who wrote the line: a Claude Code hook script, a user keypress in claudewheel, or claudewheel's own observation pass.\"\n\n[types.EndedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"ended\"\nrequired = true\n\n[types.EndedEvent.fields.outcome]\ntype = \"enum\"\nrequired = true\nvalues = [\"exited\", \"crashed\"]\ndescription = \"\\\"exited\\\" is a SessionEnd Claude Code reported; \\\"crashed\\\" is the observation pass finding no live process and no SessionEnd.\"\n\n[types.EndedEvent.fields.reason]\ntype = \"nullable\"\nrequired = true\ndescription = \"Claude Code's SessionEnd reason (clear, resume, logout, prompt_input_exit, other). Null when the line was written by the observation pass, which has no reason to report.\"\n[types.EndedEvent.fields.reason.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.EndedEvent.fields.detail]\ntype = \"nullable\"\nrequired = true\ndescription = \"Free text explaining the outcome to a human. Never dispatched on.\"\n[types.EndedEvent.fields.detail.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.NamedEvent]\ntype = \"record\"\ndescription = \"A session carried a display name. Recorded because Claude Code's registry holds the name only while the process lives.\"\n\n[types.NamedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.NamedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.NamedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.NamedEvent.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session uuid. Also the name of the file this line is in.\"\n\n[types.NamedEvent.fields.source]\ntype = \"enum\"\nrequired = true\nvalues = [\"hook\", \"user\", \"sweep\"]\ndescription = \"Who wrote the line: a Claude Code hook script, a user keypress in claudewheel, or claudewheel's own observation pass.\"\n\n[types.NamedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"named\"\nrequired = true\n\n[types.NamedEvent.fields.name]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The session's display name, as seen.\"\n\n[types.NamedEvent.fields.name_source]\ntype = \"nullable\"\nrequired = true\ndescription = \"Claude Code's registry nameSource for the name (user, peer, derived, collision, auto, hook). Free text rather than an enum: it is Claude Code's vocabulary, it grows without notice, and claudewheel only displays it.\"\n[types.NamedEvent.fields.name_source.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.MarkEvent]\ntype = \"record\"\ndescription = \"The user marked the session, or cleared a previous mark. The one kind a person authors directly.\"\n\n[types.MarkEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.MarkEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.MarkEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.MarkEvent.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session uuid. Also the name of the file this line is in.\"\n\n[types.MarkEvent.fields.source]\ntype = \"enum\"\nrequired = true\nvalues = [\"hook\", \"user\", \"sweep\"]\ndescription = \"Who wrote the line: a Claude Code hook script, a user keypress in claudewheel, or claudewheel's own observation pass.\"\n\n[types.MarkEvent.fields.kind]\ntype = \"literal\"\nvalue = \"mark\"\nrequired = true\n\n[types.MarkEvent.fields.state]\ntype = \"nullable\"\nrequired = true\ndescription = \"The mark the user set. Null CLEARS whatever mark an earlier line set -- that is the only way a mark goes away, because the store is append-only.\"\n[types.MarkEvent.fields.state.inner]\ntype = \"enum\"\nvalues = [\"on-hold\", \"blocked\", \"done\"]\n\n[types.MarkEvent.fields.note]\ntype = \"nullable\"\nrequired = true\ndescription = \"The user's own note about the mark. Free text, never dispatched on.\"\n[types.MarkEvent.fields.note.inner]\ntype = \"string\"\nnon_empty = true\n\n# ---------------------------------------------------------------------------\n# The document root: one line is exactly one event, selected by `kind`.\n# ---------------------------------------------------------------------------\n\n[types.LifecycleEvent]\ntype = \"discriminated-union\"\ndiscriminator = \"kind\"\ndescription = \"One session lifecycle line. The `kind` field selects the arm; an unrecognized kind is a hard error, never a skipped line.\"\n\n[types.LifecycleEvent.arms.started]\ntype = \"StartedEvent\"\n\n[types.LifecycleEvent.arms.ended]\ntype = \"EndedEvent\"\n\n[types.LifecycleEvent.arms.named]\ntype = \"NamedEvent\"\n\n[types.LifecycleEvent.arms.mark]\ntype = \"MarkEvent\"\n",
}
_EMBEDDED_MAIN_FILE = "lifecycle-event.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return v, result.diagnostics


def validate_value(v: Value) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return v, result.diagnostics


@dataclass(frozen=True, kw_only=True)
class StartedEvent:
    """Frozen typed binding of the "StartedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    session: str
    source: str
    kind: str
    cwd: str
    config_dir: str
    profile: str | None
    claude_version: str | None
    model: str | None
    permissions: str | None
    entry: str
    transcript: str | None
    pid: int | None

    def with_format_version(self, v: int) -> StartedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> StartedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> StartedEvent:
        return replace(self, at=v)

    def with_session(self, v: str) -> StartedEvent:
        return replace(self, session=v)

    def with_source(self, v: str) -> StartedEvent:
        return replace(self, source=v)

    def with_kind(self, v: str) -> StartedEvent:
        return replace(self, kind=v)

    def with_cwd(self, v: str) -> StartedEvent:
        return replace(self, cwd=v)

    def with_config_dir(self, v: str) -> StartedEvent:
        return replace(self, config_dir=v)

    def with_profile(self, v: str | None) -> StartedEvent:
        return replace(self, profile=v)

    def with_claude_version(self, v: str | None) -> StartedEvent:
        return replace(self, claude_version=v)

    def with_model(self, v: str | None) -> StartedEvent:
        return replace(self, model=v)

    def with_permissions(self, v: str | None) -> StartedEvent:
        return replace(self, permissions=v)

    def with_entry(self, v: str) -> StartedEvent:
        return replace(self, entry=v)

    def with_transcript(self, v: str | None) -> StartedEvent:
        return replace(self, transcript=v)

    def with_pid(self, v: int | None) -> StartedEvent:
        return replace(self, pid=v)


def _bind_StartedEvent(v: Value) -> StartedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_session = v.field("session")
    f_source = v.field("source")
    f_kind = v.field("kind")
    f_cwd = v.field("cwd")
    f_config_dir = v.field("config_dir")
    f_profile = v.field("profile")
    f_claude_version = v.field("claude_version")
    f_model = v.field("model")
    f_permissions = v.field("permissions")
    f_entry = v.field("entry")
    f_transcript = v.field("transcript")
    f_pid = v.field("pid")
    return StartedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        source=(f_source[0].string()[0] if f_source[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        cwd=(f_cwd[0].string()[0] if f_cwd[1] else ""),
        config_dir=(f_config_dir[0].string()[0] if f_config_dir[1] else ""),
        profile=((None if f_profile[0].is_null() else f_profile[0].string()[0]) if f_profile[1] else None),
        claude_version=((None if f_claude_version[0].is_null() else f_claude_version[0].string()[0]) if f_claude_version[1] else None),
        model=((None if f_model[0].is_null() else f_model[0].string()[0]) if f_model[1] else None),
        permissions=((None if f_permissions[0].is_null() else f_permissions[0].string()[0]) if f_permissions[1] else None),
        entry=(f_entry[0].string()[0] if f_entry[1] else ""),
        transcript=((None if f_transcript[0].is_null() else f_transcript[0].string()[0]) if f_transcript[1] else None),
        pid=((None if f_pid[0].is_null() else f_pid[0].int()[0]) if f_pid[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class EndedEvent:
    """Frozen typed binding of the "EndedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    session: str
    source: str
    kind: str
    outcome: str
    reason: str | None
    detail: str | None

    def with_format_version(self, v: int) -> EndedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> EndedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> EndedEvent:
        return replace(self, at=v)

    def with_session(self, v: str) -> EndedEvent:
        return replace(self, session=v)

    def with_source(self, v: str) -> EndedEvent:
        return replace(self, source=v)

    def with_kind(self, v: str) -> EndedEvent:
        return replace(self, kind=v)

    def with_outcome(self, v: str) -> EndedEvent:
        return replace(self, outcome=v)

    def with_reason(self, v: str | None) -> EndedEvent:
        return replace(self, reason=v)

    def with_detail(self, v: str | None) -> EndedEvent:
        return replace(self, detail=v)


def _bind_EndedEvent(v: Value) -> EndedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_session = v.field("session")
    f_source = v.field("source")
    f_kind = v.field("kind")
    f_outcome = v.field("outcome")
    f_reason = v.field("reason")
    f_detail = v.field("detail")
    return EndedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        source=(f_source[0].string()[0] if f_source[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        outcome=(f_outcome[0].string()[0] if f_outcome[1] else ""),
        reason=((None if f_reason[0].is_null() else f_reason[0].string()[0]) if f_reason[1] else None),
        detail=((None if f_detail[0].is_null() else f_detail[0].string()[0]) if f_detail[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class NamedEvent:
    """Frozen typed binding of the "NamedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    session: str
    source: str
    kind: str
    name: str
    name_source: str | None

    def with_format_version(self, v: int) -> NamedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> NamedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> NamedEvent:
        return replace(self, at=v)

    def with_session(self, v: str) -> NamedEvent:
        return replace(self, session=v)

    def with_source(self, v: str) -> NamedEvent:
        return replace(self, source=v)

    def with_kind(self, v: str) -> NamedEvent:
        return replace(self, kind=v)

    def with_name(self, v: str) -> NamedEvent:
        return replace(self, name=v)

    def with_name_source(self, v: str | None) -> NamedEvent:
        return replace(self, name_source=v)


def _bind_NamedEvent(v: Value) -> NamedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_session = v.field("session")
    f_source = v.field("source")
    f_kind = v.field("kind")
    f_name = v.field("name")
    f_name_source = v.field("name_source")
    return NamedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        source=(f_source[0].string()[0] if f_source[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        name=(f_name[0].string()[0] if f_name[1] else ""),
        name_source=((None if f_name_source[0].is_null() else f_name_source[0].string()[0]) if f_name_source[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class MarkEvent:
    """Frozen typed binding of the "MarkEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    session: str
    source: str
    kind: str
    state: str | None
    note: str | None

    def with_format_version(self, v: int) -> MarkEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> MarkEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> MarkEvent:
        return replace(self, at=v)

    def with_session(self, v: str) -> MarkEvent:
        return replace(self, session=v)

    def with_source(self, v: str) -> MarkEvent:
        return replace(self, source=v)

    def with_kind(self, v: str) -> MarkEvent:
        return replace(self, kind=v)

    def with_state(self, v: str | None) -> MarkEvent:
        return replace(self, state=v)

    def with_note(self, v: str | None) -> MarkEvent:
        return replace(self, note=v)


def _bind_MarkEvent(v: Value) -> MarkEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_session = v.field("session")
    f_source = v.field("source")
    f_kind = v.field("kind")
    f_state = v.field("state")
    f_note = v.field("note")
    return MarkEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        source=(f_source[0].string()[0] if f_source[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        state=((None if f_state[0].is_null() else f_state[0].string()[0]) if f_state[1] else None),
        note=((None if f_note[0].is_null() else f_note[0].string()[0]) if f_note[1] else None),
    )


