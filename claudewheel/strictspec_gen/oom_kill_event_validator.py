# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.4.0
# schema:              claudewheel-oom-kill-event (format_version 1)
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
    "oom-kill-event.schema.toml": "# strictspec schema for one claudewheel OOM KILL line.\n# Source of truth for the Python side: claudewheel/probe.py.\n# One document = one JSONL line = one OOM kill the probe runner read.\n#\n# WHAT THE STORE IS: an append-only log\n# (`~/.claudewheel/shared/probes/kills.jsonl`) of every entry systemd's user\n# manager wrote for a unit whose process the kernel's OOM killer killed, as\n# claudewheel-probe-runner.service read it from the user journal, with the\n# Claude Code session it was attributed to and the reports it produced. A kill\n# no session and no subscription could take is recorded here with no reports,\n# so nothing is dropped silently.\n#\n# EVERY FIELD IS PRESENT ON EVERY LINE IT BELONGS TO. A fact that is not known\n# is written as `null`, never omitted.\n\nname = \"claudewheel-oom-kill-event\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"jsonl\"\nrole = \"schema\"\nroot = \"OomKillEvent\"\ntargets = [\"python\"]\ndescription = \"One JSONL OOM kill line: a kill systemd reported for one unit, and where its reports went.\"\n\n[types.OomKillEvent]\ntype = \"record\"\ndescription = \"One OOM kill in one systemd user unit.\"\n\n[types.OomKillEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.OomKillEvent.fields.id]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The kill id, unique across the file. Every report of this kill names it.\"\n\n[types.OomKillEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the runner recorded the kill (RFC 3339 UTC, millisecond precision).\"\n\n[types.OomKillEvent.fields.killed_at_us]\ntype = \"integer\"\nrequired = true\ndescription = \"When systemd wrote the kill to the journal, in microseconds since the epoch (the entry's __REALTIME_TIMESTAMP). An integer so a hook can compare it with a tool call's window without parsing a date.\"\n\n[types.OomKillEvent.fields.unit]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The systemd user unit the kill happened in.\"\n\n[types.OomKillEvent.fields.cursor]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The journal cursor of the entry, so a restarted runner resumes after it.\"\n\n[types.OomKillEvent.fields.scope]\ntype = \"enum\"\nrequired = true\nvalues = [\"session\", \"heavy\", \"other\"]\ndescription = \"Which kind of unit it was: a claudewheel session scope, a heavy job's scope, or any other unit.\"\n\n[types.OomKillEvent.fields.session]\ntype = \"nullable\"\nrequired = true\ndescription = \"The Claude Code session whose command was killed. Null when the kill could not be attributed to one.\"\n[types.OomKillEvent.fields.session.inner]\ntype = \"string\"\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\n\n[types.OomKillEvent.fields.command]\ntype = \"nullable\"\nrequired = true\ndescription = \"The command a heavy job ran, from its scope's description. Null for any other unit.\"\n[types.OomKillEvent.fields.command.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.OomKillEvent.fields.unattributed]\ntype = \"nullable\"\nrequired = true\ndescription = \"Why the kill has no session. Null when it has one.\"\n[types.OomKillEvent.fields.unattributed.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.OomKillEvent.fields.reports]\ntype = \"array\"\nrequired = true\ndescription = \"The ids of the reports the kill produced. Empty when nothing took it: an unrouted kill.\"\n[types.OomKillEvent.fields.reports.items]\ntype = \"string\"\nregex = \"^[0-9a-f]{16}$\"\n\n[types.OomKillEvent.fields.probes]\ntype = \"array\"\nrequired = true\ndescription = \"The ids of the probes that saw the kill, whether or not a subscriber took a report. A probe's count is the number of kills naming it.\"\n[types.OomKillEvent.fields.probes.items]\ntype = \"string\"\nregex = \"^[0-9a-f]{16}$\"\n",
}
_EMBEDDED_MAIN_FILE = "oom-kill-event.schema.toml"

# Pairing: this file's generated-code format must be one the runtime reads. This
# runs at import, so a runtime that cannot read it hard-errors before any
# validation is attempted.
strictspec.require_generated_code_format(GENERATED_CODE_FORMAT, GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[OomKillEvent | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[OomKillEvent | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the constraint
    vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_OomKillEvent(v), result.diagnostics


def validate_value(v: Value) -> tuple[OomKillEvent | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_OomKillEvent(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class OomKillEvent:
    """Frozen typed binding of the "OomKillEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    killed_at_us: int
    unit: str
    cursor: str
    scope: str
    session: str | None
    command: str | None
    unattributed: str | None
    reports: list[Value]
    probes: list[Value]

    def with_format_version(self, v: int) -> OomKillEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> OomKillEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> OomKillEvent:
        return replace(self, at=v)

    def with_killed_at_us(self, v: int) -> OomKillEvent:
        return replace(self, killed_at_us=v)

    def with_unit(self, v: str) -> OomKillEvent:
        return replace(self, unit=v)

    def with_cursor(self, v: str) -> OomKillEvent:
        return replace(self, cursor=v)

    def with_scope(self, v: str) -> OomKillEvent:
        return replace(self, scope=v)

    def with_session(self, v: str | None) -> OomKillEvent:
        return replace(self, session=v)

    def with_command(self, v: str | None) -> OomKillEvent:
        return replace(self, command=v)

    def with_unattributed(self, v: str | None) -> OomKillEvent:
        return replace(self, unattributed=v)

    def with_reports(self, v: list[Value]) -> OomKillEvent:
        return replace(self, reports=v)

    def with_probes(self, v: list[Value]) -> OomKillEvent:
        return replace(self, probes=v)


def _bind_OomKillEvent(v: Value) -> OomKillEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_killed_at_us = v.field("killed_at_us")
    f_unit = v.field("unit")
    f_cursor = v.field("cursor")
    f_scope = v.field("scope")
    f_session = v.field("session")
    f_command = v.field("command")
    f_unattributed = v.field("unattributed")
    f_reports = v.field("reports")
    f_probes = v.field("probes")
    return OomKillEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        killed_at_us=(f_killed_at_us[0].int()[0] if f_killed_at_us[1] else 0),
        unit=(f_unit[0].string()[0] if f_unit[1] else ""),
        cursor=(f_cursor[0].string()[0] if f_cursor[1] else ""),
        scope=(f_scope[0].string()[0] if f_scope[1] else ""),
        session=((None if f_session[0].is_null() else f_session[0].string()[0]) if f_session[1] else None),
        command=((None if f_command[0].is_null() else f_command[0].string()[0]) if f_command[1] else None),
        unattributed=((None if f_unattributed[0].is_null() else f_unattributed[0].string()[0]) if f_unattributed[1] else None),
        reports=([e for e in f_reports[0].items()] if f_reports[1] else []),
        probes=([e for e in f_probes[0].items()] if f_probes[1] else []),
    )


