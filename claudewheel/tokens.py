"""Parse, expire, and write OAuth token entries in ~/.claudewheel/tokens.json."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, NamedTuple

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
    if isinstance(entry, dict):
        tok = entry.get("token")
        if isinstance(tok, str) and tok:
            return tok
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


def _read_tokens_for_write(path: Path) -> dict[str, Any]:
    """Read a tokens.json for a write operation. Missing -> {}; corrupt -> OSError.

    Preserves the historical write-path contract: a corrupt file is a hard
    OSError so callers never silently clobber it. Shared by TokenStore.add and
    TokenStore.set_tier so the message and behavior stay identical regardless
    of which path is targeted.
    """
    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return data
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise OSError(
            f"{path} is corrupt ({e}); refusing to overwrite it. "
            "Fix or remove the file, then retry."
        ) from e


def _write_token(path: Path, name: str, token: str, *,
                 tier: str | None = None,
                 subscription: str | None = None) -> None:
    """Add/update a token entry in the tokens.json at *path* (0600, atomic)."""
    tokens = _read_tokens_for_write(path)

    created = date.today()
    entry: dict[str, str] = {
        "token": token,
        "created": created.isoformat(),
        "expires_at": (created + timedelta(days=TOKEN_TTL_DAYS)).isoformat(),
    }
    if tier is not None:
        entry["rateLimitTier"] = tier
    if subscription is not None:
        entry["subscriptionType"] = subscription
    tokens[name] = entry

    write_json_atomic_secret(path, tokens)


def _write_tier(path: Path, name: str, *, tier: str | None = None,
                subscription: str | None = None) -> None:
    """Merge/create a tier metadata entry in the tokens.json at *path*."""
    if tier is None and subscription is None:
        return

    tokens = _read_tokens_for_write(path)

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

    write_json_atomic_secret(path, tokens)


class TokenStoreError(Exception):
    """Raised when a tokens.json cannot be read/parsed and resolution cannot proceed."""


@dataclass
class TokenStore:
    """Path-injected read/write facade over a single tokens.json file.

    All paths are explicit -- TokenStore never reads module path constants.
    Read APIs (load/token_for/names/expiry_for) raise TokenStoreError on a
    corrupt or unreadable file. Write APIs (add/set_tier) preserve the
    historical OSError contract; rename/remove swallow read errors and return
    False, mirroring the profile_ops helpers they replace.
    """

    path: Path

    def load(self) -> dict[str, Any]:
        """Parse the tokens.json. Missing -> {}; corrupt/unreadable -> TokenStoreError."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise TokenStoreError(
                f"{self.path} is corrupt or unreadable ({e}); "
                "token resolution cannot proceed. Fix or remove the file, then retry."
            ) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TokenStoreError(
                f"{self.path} is corrupt or unreadable ({e}); "
                "token resolution cannot proceed. Fix or remove the file, then retry."
            ) from e
        if not isinstance(data, dict):
            raise TokenStoreError(
                f"{self.path} is corrupt or unreadable (top-level JSON is not an object); "
                "token resolution cannot proceed. Fix or remove the file, then retry."
            )
        return data

    def token_for(self, name: str) -> str | None:
        """Return the token string for *name*, or None if absent/tier-only."""
        return parse_entry(self.load().get(name))

    def names(self) -> set[str]:
        """Return the set of profile names present in the file."""
        return set(self.load().keys())

    def expiry_for(self, name: str) -> TokenExpiry | None:
        """Compute *name*'s expiry, using the file mtime for legacy entries.

        Returns None when the entry is absent. Raises TokenStoreError if the
        file is corrupt (via load()).
        """
        data = self.load()
        if name not in data:
            return None
        mtime = self.path.stat().st_mtime
        return compute_expiry(data[name], mtime)

    def add(self, name: str, token: str, *, tier: str | None = None,
            subscription: str | None = None) -> None:
        """Add/update a token entry (0600, atomic). OSError on corrupt file."""
        _write_token(self.path, name, token, tier=tier, subscription=subscription)

    def set_tier(self, name: str, *, tier: str | None = None,
                 subscription: str | None = None) -> None:
        """Merge/create tier metadata (0600, atomic). OSError on corrupt file."""
        _write_tier(self.path, name, tier=tier, subscription=subscription)

    def rename(self, old: str, new: str) -> bool:
        """Move the *old* key to *new*. Returns True if the entry existed."""
        try:
            tokens = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        if old not in tokens:
            return False
        tokens[new] = tokens.pop(old)
        write_json_atomic_secret(self.path, tokens)
        return True

    def remove(self, name: str) -> bool:
        """Remove *name*'s entry. Returns True if it existed."""
        try:
            tokens = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        if name not in tokens:
            return False
        del tokens[name]
        write_json_atomic_secret(self.path, tokens)
        return True
