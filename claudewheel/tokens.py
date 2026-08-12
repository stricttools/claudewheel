"""The OAuth token entry format: build one, date it, and read its tier fields.

The entry itself is stored per profile by :mod:`claudewheel.profile_data`, one
file inside each profile directory.  This module owns nothing on disk -- only
the shape of an entry and what its fields mean.
"""

from __future__ import annotations

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
        ("subscriptionType", "CLAUDE_CODE_SUBSCRIPTION_TYPE", SUBSCRIPTION_TYPES),
        ("rateLimitTier", "CLAUDE_CODE_RATE_LIMIT_TIER", RATE_LIMIT_TIERS),
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
    tier: str | None = None,
    subscription: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Assemble a token entry dict in the canonical shape.

    The one description of the entry format: ``token`` plus ``created``, then
    either ``expires_at`` (a TTL disposition) or the ``expiry_unknown`` marker
    (an externally-issued token), plus the optional plan-tier fields.
    """
    created = date.today() if today is None else today
    entry: dict[str, Any] = {"token": token, "created": created.isoformat()}
    if expiry is TokenExpiryDisposition.TTL:
        entry["expires_at"] = (created + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
    elif expiry is TokenExpiryDisposition.UNKNOWN:
        entry[EXPIRY_UNKNOWN_FIELD] = True
    if tier is not None:
        entry["rateLimitTier"] = tier
    if subscription is not None:
        entry["subscriptionType"] = subscription
    return entry


class TokenStoreError(Exception):
    """Raised when a token entry cannot be read/parsed and resolution cannot proceed."""
