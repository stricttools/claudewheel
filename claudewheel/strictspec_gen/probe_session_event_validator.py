# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.6.0
# schema:              claudewheel-probe-session-event (format_version 1)
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
GENERATED_BY = "0.6.0"
# GENERATED_CODE_FORMAT is the shape of generated code this file was written to.
# The runtime pairing guard hard-errors unless this format is one the linked
# runtime reads; the remedy is regeneration.
GENERATED_CODE_FORMAT = 2
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "probe-session-event.schema.toml": "# strictspec schema for one claudewheel PROBE SESSION line.\n# Source of truth for the Python side: claudewheel/probe.py; the only writer is\n# the hook-deliver-probe-reports hook script.\n# One document = one JSONL line = one fact about one session's tool calls or\n# subagents that report delivery needs.\n#\n# WHAT THE STORE IS: an append-only log, one file per Claude Code session\n# (`~/.claudewheel/shared/probes/sessions/<session-uuid>.jsonl`), recording\n# when each Bash tool call started and ended (so a call that ends OOM-killed\n# is told whether another call was running at the kill) and which subagents\n# were launched, for which task, and when they finished (so a report for a\n# finished subagent reaches its main conversation, labeled).\n#\n# Times are integer milliseconds since the epoch, not RFC 3339 text: the hook\n# compares them with numbers from Claude Code's payloads, in jq.\n#\n# EVERY FIELD IS PRESENT ON EVERY LINE IT BELONGS TO. A fact that is not known\n# is written as `null`, never omitted.\n\nname = \"claudewheel-probe-session-event\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"jsonl\"\nrole = \"schema\"\nroot = \"ProbeSessionEvent\"\ntargets = [\"python\", \"go\"]\ndescription = \"One JSONL probe session line: a Bash call starting or ending, or a subagent launched or finished.\"\n\n[types.CallStartedEvent]\ntype = \"record\"\ndescription = \"A Bash tool call started.\"\n\n[types.CallStartedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.CallStartedEvent.fields.at_ms]\ntype = \"integer\"\nrequired = true\ndescription = \"When the call started, in milliseconds since the epoch.\"\n\n[types.CallStartedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"call-started\"\nrequired = true\n\n[types.CallStartedEvent.fields.call]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Claude Code's tool_use_id of the call.\"\n\n[types.CallStartedEvent.fields.agent]\ntype = \"nullable\"\nrequired = true\ndescription = \"The subagent making the call. Null for the main conversation.\"\n[types.CallStartedEvent.fields.agent.inner]\ntype = \"string\"\nregex = \"^[0-9a-zA-Z_-]+$\"\n\n[types.CallStartedEvent.fields.timeout_ms]\ntype = \"integer\"\nrequired = true\ndescription = \"How long the call may run: its own timeout, or the Bash tool's default. Bounds a call whose end was never recorded.\"\n\n[types.CallEndedEvent]\ntype = \"record\"\ndescription = \"A Bash tool call ended, successfully or not.\"\n\n[types.CallEndedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.CallEndedEvent.fields.at_ms]\ntype = \"integer\"\nrequired = true\ndescription = \"When the call ended, in milliseconds since the epoch.\"\n\n[types.CallEndedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"call-ended\"\nrequired = true\n\n[types.CallEndedEvent.fields.call]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Claude Code's tool_use_id of the call.\"\n\n[types.CallEndedEvent.fields.agent]\ntype = \"nullable\"\nrequired = true\ndescription = \"The subagent that made the call. Null for the main conversation.\"\n[types.CallEndedEvent.fields.agent.inner]\ntype = \"string\"\nregex = \"^[0-9a-zA-Z_-]+$\"\n\n[types.AgentLaunchedEvent]\ntype = \"record\"\ndescription = \"The session launched a subagent.\"\n\n[types.AgentLaunchedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.AgentLaunchedEvent.fields.at_ms]\ntype = \"integer\"\nrequired = true\ndescription = \"When the launch was recorded, in milliseconds since the epoch.\"\n\n[types.AgentLaunchedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"agent-launched\"\nrequired = true\n\n[types.AgentLaunchedEvent.fields.agent]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-zA-Z_-]+$\"\ndescription = \"The subagent's id (the Agent call's agentId).\"\n\n[types.AgentLaunchedEvent.fields.task]\ntype = \"nullable\"\nrequired = true\ndescription = \"The Agent call's description of the task. Null when the call gave none.\"\n[types.AgentLaunchedEvent.fields.task.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.AgentFinishedEvent]\ntype = \"record\"\ndescription = \"A subagent finished (SubagentStop).\"\n\n[types.AgentFinishedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.AgentFinishedEvent.fields.at_ms]\ntype = \"integer\"\nrequired = true\ndescription = \"When the subagent finished, in milliseconds since the epoch.\"\n\n[types.AgentFinishedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"agent-finished\"\nrequired = true\n\n[types.AgentFinishedEvent.fields.agent]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-zA-Z_-]+$\"\ndescription = \"The subagent's id.\"\n\n[types.ProbeSessionEvent]\ntype = \"discriminated-union\"\ndiscriminator = \"kind\"\ndescription = \"One probe session line. The `kind` field selects the arm; an unrecognized kind is a hard error, never a skipped line.\"\n\n[types.ProbeSessionEvent.arms.call-started]\ntype = \"CallStartedEvent\"\n\n[types.ProbeSessionEvent.arms.call-ended]\ntype = \"CallEndedEvent\"\n\n[types.ProbeSessionEvent.arms.agent-launched]\ntype = \"AgentLaunchedEvent\"\n\n[types.ProbeSessionEvent.arms.agent-finished]\ntype = \"AgentFinishedEvent\"\n",
}
_EMBEDDED_MAIN_FILE = "probe-session-event.schema.toml"

# Pairing: this file's generated-code format must be one the runtime reads. This
# runs at import, so a runtime that cannot read it hard-errors before any
# validation is attempted.
strictspec.require_generated_code_format(GENERATED_CODE_FORMAT, GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the constraint
    vocabulary.
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
class CallStartedEvent:
    """Frozen typed binding of the "CallStartedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    at_ms: int
    kind: str
    call: str
    agent: str | None
    timeout_ms: int

    def with_format_version(self, v: int) -> CallStartedEvent:
        return replace(self, format_version=v)

    def with_at_ms(self, v: int) -> CallStartedEvent:
        return replace(self, at_ms=v)

    def with_kind(self, v: str) -> CallStartedEvent:
        return replace(self, kind=v)

    def with_call(self, v: str) -> CallStartedEvent:
        return replace(self, call=v)

    def with_agent(self, v: str | None) -> CallStartedEvent:
        return replace(self, agent=v)

    def with_timeout_ms(self, v: int) -> CallStartedEvent:
        return replace(self, timeout_ms=v)


def _bind_CallStartedEvent(v: Value) -> CallStartedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_at_ms = v.field("at_ms")
    f_kind = v.field("kind")
    f_call = v.field("call")
    f_agent = v.field("agent")
    f_timeout_ms = v.field("timeout_ms")
    return CallStartedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        at_ms=(f_at_ms[0].int()[0] if f_at_ms[1] else 0),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        call=(f_call[0].string()[0] if f_call[1] else ""),
        agent=((None if f_agent[0].is_null() else f_agent[0].string()[0]) if f_agent[1] else None),
        timeout_ms=(f_timeout_ms[0].int()[0] if f_timeout_ms[1] else 0),
    )


@dataclass(frozen=True, kw_only=True)
class CallEndedEvent:
    """Frozen typed binding of the "CallEndedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    at_ms: int
    kind: str
    call: str
    agent: str | None

    def with_format_version(self, v: int) -> CallEndedEvent:
        return replace(self, format_version=v)

    def with_at_ms(self, v: int) -> CallEndedEvent:
        return replace(self, at_ms=v)

    def with_kind(self, v: str) -> CallEndedEvent:
        return replace(self, kind=v)

    def with_call(self, v: str) -> CallEndedEvent:
        return replace(self, call=v)

    def with_agent(self, v: str | None) -> CallEndedEvent:
        return replace(self, agent=v)


def _bind_CallEndedEvent(v: Value) -> CallEndedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_at_ms = v.field("at_ms")
    f_kind = v.field("kind")
    f_call = v.field("call")
    f_agent = v.field("agent")
    return CallEndedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        at_ms=(f_at_ms[0].int()[0] if f_at_ms[1] else 0),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        call=(f_call[0].string()[0] if f_call[1] else ""),
        agent=((None if f_agent[0].is_null() else f_agent[0].string()[0]) if f_agent[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class AgentLaunchedEvent:
    """Frozen typed binding of the "AgentLaunchedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    at_ms: int
    kind: str
    agent: str
    task: str | None

    def with_format_version(self, v: int) -> AgentLaunchedEvent:
        return replace(self, format_version=v)

    def with_at_ms(self, v: int) -> AgentLaunchedEvent:
        return replace(self, at_ms=v)

    def with_kind(self, v: str) -> AgentLaunchedEvent:
        return replace(self, kind=v)

    def with_agent(self, v: str) -> AgentLaunchedEvent:
        return replace(self, agent=v)

    def with_task(self, v: str | None) -> AgentLaunchedEvent:
        return replace(self, task=v)


def _bind_AgentLaunchedEvent(v: Value) -> AgentLaunchedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_at_ms = v.field("at_ms")
    f_kind = v.field("kind")
    f_agent = v.field("agent")
    f_task = v.field("task")
    return AgentLaunchedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        at_ms=(f_at_ms[0].int()[0] if f_at_ms[1] else 0),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        agent=(f_agent[0].string()[0] if f_agent[1] else ""),
        task=((None if f_task[0].is_null() else f_task[0].string()[0]) if f_task[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class AgentFinishedEvent:
    """Frozen typed binding of the "AgentFinishedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    at_ms: int
    kind: str
    agent: str

    def with_format_version(self, v: int) -> AgentFinishedEvent:
        return replace(self, format_version=v)

    def with_at_ms(self, v: int) -> AgentFinishedEvent:
        return replace(self, at_ms=v)

    def with_kind(self, v: str) -> AgentFinishedEvent:
        return replace(self, kind=v)

    def with_agent(self, v: str) -> AgentFinishedEvent:
        return replace(self, agent=v)


def _bind_AgentFinishedEvent(v: Value) -> AgentFinishedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_at_ms = v.field("at_ms")
    f_kind = v.field("kind")
    f_agent = v.field("agent")
    return AgentFinishedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        at_ms=(f_at_ms[0].int()[0] if f_at_ms[1] else 0),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        agent=(f_agent[0].string()[0] if f_agent[1] else ""),
    )


