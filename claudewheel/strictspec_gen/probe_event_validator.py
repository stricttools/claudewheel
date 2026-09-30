# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.4.0
# schema:              claudewheel-probe-event (format_version 1)
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
    "probe-event.schema.toml": "# strictspec schema for one claudewheel PROBE line.\n# Source of truth for the Python side: claudewheel/probe.py.\n# One document = one JSONL line = one event in one probe's life.\n#\n# WHAT THE STORE IS: an append-only log, one file per probe\n# (`~/.claudewheel/shared/probes/probes/<probe-id>.jsonl`). A probe watches for\n# one kind of event (the only kind is `oom-kill`) in one Claude Code session or\n# in all of them, until its deadline or an earlier stop, and hands each report\n# to the sessions subscribed to it. The log records that the probe was created,\n# who subscribed, which conversation each subscription is bound to, who\n# unsubscribed, and how the probe ended.\n#\n# SCOPE: strictspec owns the raw DOCUMENT SHAPE. claudewheel keeps what\n# strictspec cannot see: that every line of a file names the probe the file is\n# named after, that a `created` line comes first, and the state a reader\n# derives from the whole file.\n#\n# EVERY FIELD IS PRESENT ON EVERY LINE IT BELONGS TO. A fact that is not known\n# is written as `null`, never omitted.\n\nname = \"claudewheel-probe-event\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"jsonl\"\nrole = \"schema\"\nroot = \"ProbeEvent\"\ntargets = [\"python\"]\ndescription = \"One JSONL probe line: a single fact about one probe, discriminated by `kind`.\"\n\n[types.CreatedEvent]\ntype = \"record\"\ndescription = \"A session created the probe. The creating session is subscribed to it by a `subscribed` line written with this one.\"\n\n[types.CreatedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.CreatedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.CreatedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.CreatedEvent.fields.probe]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The probe id. Also the name of the file this line is in.\"\n\n[types.CreatedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"created\"\nrequired = true\n\n[types.CreatedEvent.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session that created the probe.\"\n\n[types.CreatedEvent.fields.probe_kind]\ntype = \"enum\"\nrequired = true\nvalues = [\"oom-kill\"]\ndescription = \"What the probe watches for. `oom-kill` is systemd's result term for a unit whose process the kernel's OOM killer killed.\"\n\n[types.CreatedEvent.fields.watch_session]\ntype = \"nullable\"\nrequired = true\ndescription = \"The one Claude Code session whose events the probe watches. Null watches every session, which the creator had to spell out.\"\n[types.CreatedEvent.fields.watch_session.inner]\ntype = \"string\"\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\n\n[types.CreatedEvent.fields.deadline]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the probe ends at the latest. Every probe states one.\"\n\n[types.CreatedEvent.fields.until_count]\ntype = \"nullable\"\nrequired = true\ndescription = \"End the probe once it has seen this many events. Null when no count was given.\"\n[types.CreatedEvent.fields.until_count.inner]\ntype = \"integer\"\n\n[types.CreatedEvent.fields.until_watched_ends]\ntype = \"boolean\"\nrequired = true\ndescription = \"End the probe when the watched session ends. Always false for a probe watching every session.\"\n\n[types.CreatedEvent.fields.until_file]\ntype = \"nullable\"\nrequired = true\ndescription = \"End the probe once this absolute path exists. Null when no file was given.\"\n[types.CreatedEvent.fields.until_file.inner]\ntype = \"string\"\nnon_empty = true\n\n[types.SubscribedEvent]\ntype = \"record\"\ndescription = \"A session subscribed to the probe. Which of its conversations receives the reports is decided by the `bound` line that follows.\"\n\n[types.SubscribedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.SubscribedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.SubscribedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.SubscribedEvent.fields.probe]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The probe id. Also the name of the file this line is in.\"\n\n[types.SubscribedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"subscribed\"\nrequired = true\n\n[types.SubscribedEvent.fields.subscription]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The subscription id, unique across every probe.\"\n\n[types.SubscribedEvent.fields.session]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\"\ndescription = \"The Claude Code session that subscribed.\"\n\n[types.BoundEvent]\ntype = \"record\"\ndescription = \"A subscription was bound to the conversation whose tool call created it: the main conversation, or one subagent. Written by the hook that reads the call's own payload.\"\n\n[types.BoundEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.BoundEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.BoundEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.BoundEvent.fields.probe]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The probe id. Also the name of the file this line is in.\"\n\n[types.BoundEvent.fields.kind]\ntype = \"literal\"\nvalue = \"bound\"\nrequired = true\n\n[types.BoundEvent.fields.subscription]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The subscription being bound.\"\n\n[types.BoundEvent.fields.agent]\ntype = \"nullable\"\nrequired = true\ndescription = \"The subagent the subscription belongs to, as Claude Code's agent_id names it. Null for the main conversation.\"\n[types.BoundEvent.fields.agent.inner]\ntype = \"string\"\nregex = \"^[0-9a-zA-Z_-]+$\"\n\n[types.UnsubscribedEvent]\ntype = \"record\"\ndescription = \"A subscription was removed; the probe no longer reports to it.\"\n\n[types.UnsubscribedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.UnsubscribedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.UnsubscribedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.UnsubscribedEvent.fields.probe]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The probe id. Also the name of the file this line is in.\"\n\n[types.UnsubscribedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"unsubscribed\"\nrequired = true\n\n[types.UnsubscribedEvent.fields.subscription]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The subscription removed.\"\n\n[types.EndedEvent]\ntype = \"record\"\ndescription = \"The probe ended. It watches nothing more, and its undelivered reports to sessions that have ended are expired.\"\n\n[types.EndedEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version marker. Every line claudewheel writes carries 1.\"\n\n[types.EndedEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.EndedEvent.fields.at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event happened (RFC 3339 UTC, millisecond precision).\"\n\n[types.EndedEvent.fields.probe]\ntype = \"string\"\nrequired = true\nregex = \"^[0-9a-f]{16}$\"\ndescription = \"The probe id. Also the name of the file this line is in.\"\n\n[types.EndedEvent.fields.kind]\ntype = \"literal\"\nvalue = \"ended\"\nrequired = true\n\n[types.EndedEvent.fields.reason]\ntype = \"enum\"\nrequired = true\nvalues = [\"stopped\", \"deadline\", \"count\", \"watched-ended\", \"file\"]\ndescription = \"Why the probe ended: an explicit stop, its deadline, its count reached, the watched session ended, or its file appeared.\"\n\n[types.ProbeEvent]\ntype = \"discriminated-union\"\ndiscriminator = \"kind\"\ndescription = \"One probe line. The `kind` field selects the arm; an unrecognized kind is a hard error, never a skipped line.\"\n\n[types.ProbeEvent.arms.created]\ntype = \"CreatedEvent\"\n\n[types.ProbeEvent.arms.subscribed]\ntype = \"SubscribedEvent\"\n\n[types.ProbeEvent.arms.bound]\ntype = \"BoundEvent\"\n\n[types.ProbeEvent.arms.unsubscribed]\ntype = \"UnsubscribedEvent\"\n\n[types.ProbeEvent.arms.ended]\ntype = \"EndedEvent\"\n",
}
_EMBEDDED_MAIN_FILE = "probe-event.schema.toml"

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
class CreatedEvent:
    """Frozen typed binding of the "CreatedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    probe: str
    kind: str
    session: str
    probe_kind: str
    watch_session: str | None
    deadline: str
    until_count: int | None
    until_watched_ends: bool
    until_file: str | None

    def with_format_version(self, v: int) -> CreatedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> CreatedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> CreatedEvent:
        return replace(self, at=v)

    def with_probe(self, v: str) -> CreatedEvent:
        return replace(self, probe=v)

    def with_kind(self, v: str) -> CreatedEvent:
        return replace(self, kind=v)

    def with_session(self, v: str) -> CreatedEvent:
        return replace(self, session=v)

    def with_probe_kind(self, v: str) -> CreatedEvent:
        return replace(self, probe_kind=v)

    def with_watch_session(self, v: str | None) -> CreatedEvent:
        return replace(self, watch_session=v)

    def with_deadline(self, v: str) -> CreatedEvent:
        return replace(self, deadline=v)

    def with_until_count(self, v: int | None) -> CreatedEvent:
        return replace(self, until_count=v)

    def with_until_watched_ends(self, v: bool) -> CreatedEvent:
        return replace(self, until_watched_ends=v)

    def with_until_file(self, v: str | None) -> CreatedEvent:
        return replace(self, until_file=v)


def _bind_CreatedEvent(v: Value) -> CreatedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_probe = v.field("probe")
    f_kind = v.field("kind")
    f_session = v.field("session")
    f_probe_kind = v.field("probe_kind")
    f_watch_session = v.field("watch_session")
    f_deadline = v.field("deadline")
    f_until_count = v.field("until_count")
    f_until_watched_ends = v.field("until_watched_ends")
    f_until_file = v.field("until_file")
    return CreatedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        probe=(f_probe[0].string()[0] if f_probe[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
        probe_kind=(f_probe_kind[0].string()[0] if f_probe_kind[1] else ""),
        watch_session=((None if f_watch_session[0].is_null() else f_watch_session[0].string()[0]) if f_watch_session[1] else None),
        deadline=(f_deadline[0].datetime()[0] if f_deadline[1] else ""),
        until_count=((None if f_until_count[0].is_null() else f_until_count[0].int()[0]) if f_until_count[1] else None),
        until_watched_ends=(f_until_watched_ends[0].bool()[0] if f_until_watched_ends[1] else False),
        until_file=((None if f_until_file[0].is_null() else f_until_file[0].string()[0]) if f_until_file[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class SubscribedEvent:
    """Frozen typed binding of the "SubscribedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    probe: str
    kind: str
    subscription: str
    session: str

    def with_format_version(self, v: int) -> SubscribedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> SubscribedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> SubscribedEvent:
        return replace(self, at=v)

    def with_probe(self, v: str) -> SubscribedEvent:
        return replace(self, probe=v)

    def with_kind(self, v: str) -> SubscribedEvent:
        return replace(self, kind=v)

    def with_subscription(self, v: str) -> SubscribedEvent:
        return replace(self, subscription=v)

    def with_session(self, v: str) -> SubscribedEvent:
        return replace(self, session=v)


def _bind_SubscribedEvent(v: Value) -> SubscribedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_probe = v.field("probe")
    f_kind = v.field("kind")
    f_subscription = v.field("subscription")
    f_session = v.field("session")
    return SubscribedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        probe=(f_probe[0].string()[0] if f_probe[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        subscription=(f_subscription[0].string()[0] if f_subscription[1] else ""),
        session=(f_session[0].string()[0] if f_session[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class BoundEvent:
    """Frozen typed binding of the "BoundEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    probe: str
    kind: str
    subscription: str
    agent: str | None

    def with_format_version(self, v: int) -> BoundEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> BoundEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> BoundEvent:
        return replace(self, at=v)

    def with_probe(self, v: str) -> BoundEvent:
        return replace(self, probe=v)

    def with_kind(self, v: str) -> BoundEvent:
        return replace(self, kind=v)

    def with_subscription(self, v: str) -> BoundEvent:
        return replace(self, subscription=v)

    def with_agent(self, v: str | None) -> BoundEvent:
        return replace(self, agent=v)


def _bind_BoundEvent(v: Value) -> BoundEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_probe = v.field("probe")
    f_kind = v.field("kind")
    f_subscription = v.field("subscription")
    f_agent = v.field("agent")
    return BoundEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        probe=(f_probe[0].string()[0] if f_probe[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        subscription=(f_subscription[0].string()[0] if f_subscription[1] else ""),
        agent=((None if f_agent[0].is_null() else f_agent[0].string()[0]) if f_agent[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class UnsubscribedEvent:
    """Frozen typed binding of the "UnsubscribedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    probe: str
    kind: str
    subscription: str

    def with_format_version(self, v: int) -> UnsubscribedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> UnsubscribedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> UnsubscribedEvent:
        return replace(self, at=v)

    def with_probe(self, v: str) -> UnsubscribedEvent:
        return replace(self, probe=v)

    def with_kind(self, v: str) -> UnsubscribedEvent:
        return replace(self, kind=v)

    def with_subscription(self, v: str) -> UnsubscribedEvent:
        return replace(self, subscription=v)


def _bind_UnsubscribedEvent(v: Value) -> UnsubscribedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_probe = v.field("probe")
    f_kind = v.field("kind")
    f_subscription = v.field("subscription")
    return UnsubscribedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        probe=(f_probe[0].string()[0] if f_probe[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        subscription=(f_subscription[0].string()[0] if f_subscription[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class EndedEvent:
    """Frozen typed binding of the "EndedEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    at: str
    probe: str
    kind: str
    reason: str

    def with_format_version(self, v: int) -> EndedEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> EndedEvent:
        return replace(self, id=v)

    def with_at(self, v: str) -> EndedEvent:
        return replace(self, at=v)

    def with_probe(self, v: str) -> EndedEvent:
        return replace(self, probe=v)

    def with_kind(self, v: str) -> EndedEvent:
        return replace(self, kind=v)

    def with_reason(self, v: str) -> EndedEvent:
        return replace(self, reason=v)


def _bind_EndedEvent(v: Value) -> EndedEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_at = v.field("at")
    f_probe = v.field("probe")
    f_kind = v.field("kind")
    f_reason = v.field("reason")
    return EndedEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        at=(f_at[0].datetime()[0] if f_at[1] else ""),
        probe=(f_probe[0].string()[0] if f_probe[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        reason=(f_reason[0].string()[0] if f_reason[1] else ""),
    )


