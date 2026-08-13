"""The OAuth token entry format: build one, date it, and read its tier fields.

The entry itself is stored per profile by :mod:`claudewheel.profile_data`, one
file inside each profile directory.  This module owns nothing on disk -- only
the shape of an entry and what its fields mean.

It also owns the plan tier: :class:`PlanTier` and the closed
:data:`PLAN_TIERS` list every writer resolves against, so the two stored fields
are only ever written as one coherent pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, NamedTuple

# Claude Code setup-token TTL. Single source of truth for token lifetime.
TOKEN_TTL_DAYS = 365

# Plan-tier values Claude Code compares against. Anything outside these sets is
# inert there -- it resolves the tier to null exactly as an absent value would --
# so declaring one is rejected rather than passed through.
#
# SUBSCRIPTION_TYPES are the tiers its entitlement checks test for.
# RATE_LIMIT_TIERS are the only rate-limit strings present in the client;
# notably there is none for Pro, and Team accounts use the max_5x value.
SUBSCRIPTION_TYPES = frozenset({"max", "pro", "team", "enterprise"})
RATE_LIMIT_TIERS = frozenset(
    {"default_claude_max_20x", "default_claude_max_5x", "default_claude_zero"}
)

# Marker field written on entries whose expiry is genuinely unknown (a token
# supplied from an external source, not a claude setup-token). Its presence
# means "do not assume a 365-day lifetime -- we simply do not know".
EXPIRY_UNKNOWN_FIELD = "expiry_unknown"

# The two entry fields carrying the declared plan, named exactly as Claude Code
# names them (and one-to-one with the CLAUDE_CODE_* variables they become).
SUBSCRIPTION_FIELD = "subscriptionType"
RATE_LIMIT_FIELD = "rateLimitTier"


@dataclass(frozen=True)
class PlanTier:
    """One declarable plan: a label, and the two fields it stores.

    The pair is the unit. Claude Code reads a subscription type and a
    rate-limit tier and combines them (a Max account on the 20x tier is
    ``max`` + ``default_claude_max_20x``; Team is ``team`` +
    ``default_claude_max_5x``; Pro has no rate-limit string at all), so
    declaring one field without the other describes no real account. Every
    writer therefore passes a whole :class:`PlanTier`.

    Both fields are validated at construction against the sets Claude Code
    actually compares against: a value it does not recognize is inert there --
    the tier resolves to null exactly as an absent value would -- so an
    unrecognized value is a hard error naming the accepted ones, never a
    silently stored string.
    """

    key: str
    label: str
    subscription_type: str
    rate_limit_tier: str | None = None

    def __post_init__(self) -> None:
        if self.subscription_type not in SUBSCRIPTION_TYPES:
            raise ValueError(
                f"{SUBSCRIPTION_FIELD}={self.subscription_type!r} is not a value "
                "Claude Code recognizes. Valid values: "
                f"{', '.join(sorted(SUBSCRIPTION_TYPES))}."
            )
        if self.rate_limit_tier is not None and (
            self.rate_limit_tier not in RATE_LIMIT_TIERS
        ):
            raise ValueError(
                f"{RATE_LIMIT_FIELD}={self.rate_limit_tier!r} is not a value "
                "Claude Code recognizes. Valid values: "
                f"{', '.join(sorted(RATE_LIMIT_TIERS))}."
            )

    def fields(self) -> dict[str, str]:
        """The entry fields this plan writes, omitting a field it does not carry."""
        fields = {SUBSCRIPTION_FIELD: self.subscription_type}
        if self.rate_limit_tier is not None:
            fields[RATE_LIMIT_FIELD] = self.rate_limit_tier
        return fields


# The declarable plans, in picker order. This is the closed list every writer
# resolves against -- the creation flow, the pre-launch prompt and the scripted
# command all pick from here, so the three cannot drift apart.
#
# The pairings are measured from the Claude Code binary: it tests
# ``max`` + ``default_claude_max_20x`` for the top Max tier and
# ``team`` + ``default_claude_max_5x`` for Team, and carries no rate-limit
# string for Pro or Enterprise.
PLAN_TIERS: tuple[PlanTier, ...] = (
    PlanTier("max-20x", "Max 20x", "max", "default_claude_max_20x"),
    PlanTier("max-5x", "Max 5x", "max", "default_claude_max_5x"),
    PlanTier("pro", "Pro", "pro"),
    PlanTier("team", "Team", "team", "default_claude_max_5x"),
    PlanTier("enterprise", "Enterprise", "enterprise"),
)


def apply_plan(entry: dict[str, Any], plan: PlanTier) -> None:
    """Write *plan*'s fields into *entry*, in place.

    Both plan fields are cleared first, so a plan that carries no rate-limit
    tier (Pro, Enterprise) leaves none behind. Otherwise declaring Pro over Max
    20x would store ``pro`` beside ``default_claude_max_20x`` -- a pair no
    account has, injected into the launch environment as if it did.
    """
    for field in (SUBSCRIPTION_FIELD, RATE_LIMIT_FIELD):
        entry.pop(field, None)
    entry.update(plan.fields())


def plan_keys() -> list[str]:
    """The declarable plan keys, in picker order."""
    return [plan.key for plan in PLAN_TIERS]


def plan_by_key(key: str) -> PlanTier:
    """Resolve a plan key to its :class:`PlanTier`.

    The single door every writer goes through. An unknown key is a
    :class:`ValueError` naming the valid ones.
    """
    for plan in PLAN_TIERS:
        if plan.key == key:
            return plan
    raise ValueError(f"Unknown plan {key!r}. Valid plans: {', '.join(plan_keys())}.")


def entry_declares_plan(entry: dict[str, Any]) -> bool:
    """True when *entry* declares a plan.

    Keyed on the subscription type: it is the field every plan carries, and the
    one Claude Code's entitlement checks read. An entry holding only a
    rate-limit tier declares no plan -- that is the shape a hand-edit or an
    older claudewheel could leave behind, and it is exactly as unusable as an
    empty entry.
    """
    value = entry.get(SUBSCRIPTION_FIELD)
    return isinstance(value, str) and bool(value)


class TokenExpiryDisposition(Enum):
    """How a token's expiry is recorded when the entry is written.

    The caller MUST choose one explicitly -- there is no default -- so token
    lifetime is never silently fabricated.

    - ``TTL``: a claude setup-token, genuinely valid for ``TOKEN_TTL_DAYS``.
      The entry gets ``created`` (today) and ``expires_at`` (created + TTL).
    - ``UNKNOWN``: an externally-issued token whose expiry we cannot know.
      The entry gets ``created`` (today) and the ``expiry_unknown`` marker,
      and NO ``expires_at`` -- expiry is reported as unknown, never assumed.
    """

    TTL = "ttl"
    UNKNOWN = "unknown"


def parse_entry(entry: object) -> str | None:
    """Extract the token string from a token entry.

    An entry is a dict like ``{"token": ..., "created": ..., "expires_at": ...}``.
    Returns None if the entry is empty, absent, or unrecognized.
    """
    if isinstance(entry, dict):
        tok = entry.get("token")
        if isinstance(tok, str) and tok:
            return tok
    return None


class TokenExpiry(NamedTuple):
    """Computed token lifetime: creation date, expiry date, days remaining.

    ``remaining_days`` is ``None`` only for entries marked with an unknown
    expiry disposition -- a distinct, honest "we don't know" that consumers
    must handle separately from the "assume fresh" fallback (which reports a
    concrete ``TOKEN_TTL_DAYS``).
    """

    created: date | None
    expires: date | None
    remaining_days: float | None


def entry_expiry(entry: dict[str, Any], today: date | None = None) -> TokenExpiry:
    """Compute a dict token entry's creation date, expiry date, and days left.

    Precedence: an explicit unknown-expiry marker yields (None, None, None);
    else explicit "expires_at" ISO date; else "created" + TOKEN_TTL_DAYS.
    Unparseable or absent fields yield (None, None, TOKEN_TTL_DAYS), matching
    the historical health-check behavior of assuming a fresh token.

    The entry shape is the single per-profile record: the token string, its
    creation/expiry dates, and the plan-tier fields. Every store that holds one
    computes expiry through here.
    """
    if today is None:
        today = date.today()

    if entry.get(EXPIRY_UNKNOWN_FIELD):
        # Externally-issued token: expiry is genuinely unknown. Distinct from
        # the "assume fresh" branch below (which returns a concrete
        # TOKEN_TTL_DAYS) -- here remaining_days is None.
        return TokenExpiry(None, None, None)
    if entry.get("expires_at"):
        try:
            expires = date.fromisoformat(entry["expires_at"])
        except (ValueError, TypeError):
            return TokenExpiry(None, None, TOKEN_TTL_DAYS)
        created: date | None = None
        if entry.get("created"):
            try:
                created = date.fromisoformat(entry["created"])
            except (ValueError, TypeError):
                created = None
        return TokenExpiry(created, expires, (expires - today).days)
    if entry.get("created"):
        try:
            created = date.fromisoformat(entry["created"])
        except (ValueError, TypeError):
            return TokenExpiry(None, None, TOKEN_TTL_DAYS)
        expires = created + timedelta(days=TOKEN_TTL_DAYS)
        return TokenExpiry(created, expires, TOKEN_TTL_DAYS - (today - created).days)
    return TokenExpiry(None, None, TOKEN_TTL_DAYS)


def entry_plan_env(entry: dict[str, Any], *, source: object) -> dict[str, str]:
    """Map a token entry's plan-tier fields to Claude Code's env vars.

    Reads the ``subscriptionType`` and ``rateLimitTier`` fields, mapping each
    present one to its ``CLAUDE_CODE_*`` variable. An entry declaring neither
    yields an empty dict.

    Declared values are validated against the sets Claude Code actually compares
    against, because a value it does not recognize behaves exactly like no value
    at all -- the tier resolves to null and the failure looks identical to not
    having configured anything. Raises :class:`ValueError` naming the offending
    field, *source* (where the value was read from) and the accepted values.
    """
    env: dict[str, str] = {}
    for field, var, allowed in (
        (SUBSCRIPTION_FIELD, "CLAUDE_CODE_SUBSCRIPTION_TYPE", SUBSCRIPTION_TYPES),
        (RATE_LIMIT_FIELD, "CLAUDE_CODE_RATE_LIMIT_TIER", RATE_LIMIT_TIERS),
    ):
        value = entry.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(
                f"{source} declares {field}={value!r}, which Claude Code does "
                f"not recognize. Valid values: {', '.join(sorted(allowed))}."
            )
        env[var] = value
    return env


def build_entry(
    token: str,
    *,
    expiry: TokenExpiryDisposition,
    plan: PlanTier,
    today: date | None = None,
) -> dict[str, Any]:
    """Assemble a token entry dict in the canonical shape.

    The one description of the entry format: ``token`` plus ``created``, then
    either ``expires_at`` (a TTL disposition) or the ``expiry_unknown`` marker
    (an externally-issued token), plus the plan fields.

    *plan* is required and has no default: a token written without a stated
    plan would launch Claude Code with the tier resolved to null, and there is
    no value to guess from. Writing a token also RE-STATES the plan -- the
    entry is rebuilt, so a replaced token can never inherit the plan declared
    for the one before it.
    """
    created = date.today() if today is None else today
    entry: dict[str, Any] = {"token": token, "created": created.isoformat()}
    if expiry is TokenExpiryDisposition.TTL:
        entry["expires_at"] = (created + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
    elif expiry is TokenExpiryDisposition.UNKNOWN:
        entry[EXPIRY_UNKNOWN_FIELD] = True
    apply_plan(entry, plan)
    return entry


class TokenStoreError(Exception):
    """Raised when a token entry cannot be read/parsed and resolution cannot proceed."""
