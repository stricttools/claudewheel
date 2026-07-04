"""Parse, expire, and write OAuth token entries in ~/.claudewheel/tokens.json."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import NamedTuple

from .constants import TOKENS_FILE
from .fsutil import write_json_atomic_secret

# Claude Code setup-token TTL. Single source of truth for token lifetime.
TOKEN_TTL_DAYS = 365


def parse_entry(entry: object) -> str | None:
    """Extract the token string from a tokens.json entry.

    Supports both formats: a bare string, or a dict like
    {"token": ..., "created": ..., "expires_at": ...}.
    Returns None if the entry is empty, absent, or unrecognized.
    """
    if isinstance(entry, str) and entry:
        return entry
    if isinstance(entry, dict) and entry.get("token"):
        return entry["token"]
    return None


class TokenExpiry(NamedTuple):
    """Computed token lifetime: creation date, expiry date, days remaining."""

    created: date | None
    expires: date | None
    remaining_days: float


def compute_expiry(entry: object, tokens_mtime: float,
                   today: date | None = None) -> TokenExpiry:
    """Compute a token entry's creation date, expiry date, and remaining days.

    Precedence: explicit "expires_at" ISO date; else "created" + TOKEN_TTL_DAYS;
    else (legacy bare-string entry) the tokens.json file mtime + TOKEN_TTL_DAYS.
    Unparseable or absent dict fields yield (None, None, TOKEN_TTL_DAYS),
    matching the historical health-check behavior of assuming a fresh token.
    """
    if today is None:
        today = date.today()

    if isinstance(entry, dict):
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
            return TokenExpiry(created, expires,
                               TOKEN_TTL_DAYS - (today - created).days)
        return TokenExpiry(None, None, TOKEN_TTL_DAYS)

    # Legacy bare-string entry: only the file mtime dates it.
    created = date.fromtimestamp(tokens_mtime)
    expires = created + timedelta(days=TOKEN_TTL_DAYS)
    remaining = TOKEN_TTL_DAYS - (time.time() - tokens_mtime) / 86400
    return TokenExpiry(created, expires, remaining)


def add_token(
    name: str, token: str, *,
    tier: str | None = None,
    subscription: str | None = None,
) -> None:
    """Add or update a profile's OAuth token in tokens.json.

    Writes token, created (today), and expires_at (created + TOKEN_TTL_DAYS).
    Optionally stores rateLimitTier and subscriptionType when provided.
    The file always ends up with 0600 permissions (it holds secrets).
    Writes atomically via tmp-file rename.

    Raises OSError if an existing tokens.json cannot be parsed -- a corrupt
    file is never silently overwritten (callers handle OSError).
    """
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except FileNotFoundError:
        tokens = {}
    except json.JSONDecodeError as e:
        raise OSError(
            f"{TOKENS_FILE} is corrupt ({e}); refusing to overwrite it. "
            "Fix or remove the file, then retry."
        ) from e

    created = date.today()
    entry: dict = {
        "token": token,
        "created": created.isoformat(),
        "expires_at": (created + timedelta(days=TOKEN_TTL_DAYS)).isoformat(),
    }
    if tier is not None:
        entry["rateLimitTier"] = tier
    if subscription is not None:
        entry["subscriptionType"] = subscription
    tokens[name] = entry

    write_json_atomic_secret(TOKENS_FILE, tokens)


def store_tier(name: str, *, tier: str | None = None,
               subscription: str | None = None) -> None:
    """Store rate-limit tier metadata in tokens.json for a profile.

    Creates or updates the entry. If the profile already has a token entry,
    the tier fields are merged into it. If not, a tier-only entry (no token)
    is created -- parse_entry returns None for such entries, which is fine.

    Raises OSError if tokens.json is corrupt (same contract as add_token).
    """
    if tier is None and subscription is None:
        return

    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except FileNotFoundError:
        tokens = {}
    except json.JSONDecodeError as e:
        raise OSError(
            f"{TOKENS_FILE} is corrupt ({e}); refusing to overwrite it. "
            "Fix or remove the file, then retry."
        ) from e

    existing = tokens.get(name)
    if isinstance(existing, str):
        # Legacy bare-string entry: upgrade to dict
        existing = {"token": existing}
    elif not isinstance(existing, dict):
        existing = {}

    if tier is not None:
        existing["rateLimitTier"] = tier
    if subscription is not None:
        existing["subscriptionType"] = subscription
    tokens[name] = existing

    write_json_atomic_secret(TOKENS_FILE, tokens)
