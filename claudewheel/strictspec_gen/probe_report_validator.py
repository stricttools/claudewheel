# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.4.0
# schema:              claudewheel-probe-report (format_version 1)
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
    "probe-report.schema.toml": "# strictspec schema for one claudewheel PROBE REPORT.\n# Source of truth for the Python side: claudewheel/probe.py.\n# One document = one JSON file = one report to one conversation.\n#\n# WHAT THE STORE IS: one file per report under\n# `~/.claudewheel/shared/probes/reports/<state>/<session>/`, where the state\n# directory (pending, handed, delivered, expired) is the report's delivery\n# state and the file name `<report-id>.<recipient>.json` says which\n# conversation of the session receives it: `main`, `agent-<agent id>`, or\n# `unbound-<subscription id>` while the subscription waits for its binding.\n# A report moves between states by rename; its content never changes, except\n# that binding a subscription fills in the subagent and its task.\n#\n# EVERY FIELD IS PRESENT. A fact that is not known is written as `null`.\n\nname = \"claudewheel-probe-report\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"json\"\nrole = \"schema\"\nroot = \"ProbeReport\"\ntargets = [\"python\"]\ndescription = \"One report of one OOM kill to one conversation of one Claude Code session.\"\n\n[types.ProbeReport]\ntype = \"record\"\ndescription = \"One report, as queued for delivery.\"\n\n[types.ProbeReport.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Document format_version marker. Every report claudewheel writes carries 1.\"\n\n[types.ProbeReport.fields.id]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The report id. The delivered text carries it, which is how a delivery is confirmed in the session's transcript.\"\n\n[types.ProbeReport.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the report was queued (RFC 3339 UTC, millisecond precision).\"\n\n[types.ProbeReport.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session the report goes to.\"\n\n[types.ProbeReport.fields.agent]\ntype = \"nullable\"\nrequired = true\ndescription = \"The subagent the report is for, as Claude Code's agent_id names it. Null for the main conversation.\"\n[types.ProbeReport.fields.agent.inner]\ntype = \"string\"\nregex = \"^[0-9a-zA-Z_-]+$\"\n\n[types.ProbeReport.fields.task]\ntype = \"nullable\"\nrequired = true\ndescription = \"The task the subagent was given (the Agent call's description), when known. Labels a report that reaches the main conversation because the subagent has finished.\"\n[types.ProbeReport.fields.task.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.ProbeReport.fields.probe]\ntype = \"nullable\"\nrequired = true\ndescription = \"The probe that produced the report. Null for the report every session gets of its own commands' kills, which no probe produces.\"\n[types.ProbeReport.fields.probe.inner]\ntype = \"string\"\nregex = \"^[0-9a-f]{16}$\"\n\n[types.ProbeReport.fields.subscription]\ntype = \"nullable\"\nrequired = true\ndescription = \"The subscription the report was produced for. Null exactly when `probe` is null.\"\n[types.ProbeReport.fields.subscription.inner]\ntype = \"string\"\nregex = \"^[0-9a-f]{16}$\"\n\n[types.ProbeReport.fields.kill]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The OOM kill the report is about, by its id in kills.jsonl.\"\n\n[types.ProbeReport.fields.text]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"What the conversation is told.\"\n",
}
_EMBEDDED_MAIN_FILE = "probe-report.schema.toml"

# Pairing: this file's generated-code format must be one the runtime reads. This
# runs at import, so a runtime that cannot read it hard-errors before any
# validation is attempted.
strictspec.require_generated_code_format(GENERATED_CODE_FORMAT, GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[ProbeReport | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[ProbeReport | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the constraint
    vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_ProbeReport(v), result.diagnostics


def validate_value(v: Value) -> tuple[ProbeReport | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_ProbeReport(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class ProbeReport:
    """Frozen typed binding of the "ProbeReport" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    session: str
    agent: str | None
    task: str | None
    probe: str | None
    subscription: str | None
    kill: str
    text: str

    def with_format_version(self, v: int) -> ProbeReport:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> ProbeReport:
        return replace(self, id=v)

    def with_at(self, v: str) -> ProbeReport:
        return replace(self, at=v)

    def with_session(self, v: str) -> ProbeReport:
        return replace(self, session=v)

    def with_agent(self, v: str | None) -> ProbeReport:
        return replace(self, agent=v)

    def with_task(self, v: str | None) -> ProbeReport:
        return replace(self, task=v)

    def with_probe(self, v: str | None) -> ProbeReport:
        return replace(self, probe=v)

    def with_subscription(self, v: str | None) -> ProbeReport:
        return replace(self, subscription=v)

    def with_kill(self, v: str) -> ProbeReport:
        return replace(self, kill=v)

    def with_text(self, v: str) -> ProbeReport:
        return replace(self, text=v)


def _bind_ProbeReport(v: Value) -> ProbeReport | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_session = v.field("session")
    f_agent = v.field("agent")
    f_task = v.field("task")
    f_probe = v.field("probe")
    f_subscription = v.field("subscription")
    f_kill = v.field("kill")
    f_text = v.field("text")
    return ProbeReport(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        agent=((None if f_agent[0].is_null() else f_agent[0].string()[0]) if f_agent[1] else None),
        task=((None if f_task[0].is_null() else f_task[0].string()[0]) if f_task[1] else None),
        probe=((None if f_probe[0].is_null() else f_probe[0].string()[0]) if f_probe[1] else None),
        subscription=((None if f_subscription[0].is_null() else f_subscription[0].string()[0]) if f_subscription[1] else None),
        kill=(f_kill[0].string()[0] if f_kill[1] else ""),
        text=(f_text[0].string()[0] if f_text[1] else ""),
    )


