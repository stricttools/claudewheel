"""Parse, expire, and write OAuth token entries in ~/.claudewheel/tokens.json."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import NamedTuple

from .constants import TOKENS_FILE

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


def add_token(name: str, token: str) -> None:
    """Add or update a profile's OAuth token in tokens.json.

    Writes token, created (today), and expires_at (created + TOKEN_TTL_DAYS).
    Creates the file with 0600 permissions if it does not exist.
    Writes atomically via tmp-file rename.
    """
    try:
        tokens = json.loads(TOKENS_FILE.read_text())
    except FileNotFoundError:
        tokens = {}

    created = date.today()
    tokens[name] = {
        "token": token,
        "created": created.isoformat(),
        "expires_at": (created + timedelta(days=TOKEN_TTL_DAYS)).isoformat(),
    }

    creating = not TOKENS_FILE.exists()
    tmp = TOKENS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
        f.write("\n")
    tmp.rename(TOKENS_FILE)
    if creating:
        TOKENS_FILE.chmod(0o600)
