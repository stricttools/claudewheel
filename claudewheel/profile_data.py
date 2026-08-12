"""claudewheel's own data, stored inside each profile directory.

A profile directory IS a Claude Code config dir: Claude Code owns everything in
it, and claudewheel passes the path to it as ``CLAUDE_CONFIG_DIR``.  What
claudewheel knows about a profile -- today the OAuth token and the plan-tier
fields -- lives in one dot-prefixed subdirectory inside that same directory,
:data:`PROFILE_DATA_DIRNAME`, so the profile and everything the launcher knows
about it travel together: a rename moves the directory and the data with it, a
deletion removes both, and nothing outside can be left pointing at a profile
that no longer exists.

Layout, for a profile whose config dir is ``<profile_dir>``::

    <profile_dir>/.claudewheel/            0700, owner only
    <profile_dir>/.claudewheel/token.json  0600, the token entry

The token entry is a single JSON object (never a name-keyed map -- the file
already belongs to exactly one profile) in the shape :func:`build_entry`
assembles:

===================  ==========================================================
``token``            the OAuth token string
``created``          ISO date the entry was written
``expires_at``       ISO expiry date; present for a ``TTL`` disposition
``expiry_unknown``   ``true`` for an externally-issued token instead
``rateLimitTier``    declared plan tier, validated against Claude Code's set
``subscriptionType`` declared subscription type, validated the same way
===================  ==========================================================

Modes are set explicitly rather than inherited from the umask.  The token file
is written 0600 from creation by the secret writer, and the subdirectory is
chmod'd 0700 every time it is ensured: a world-readable parent would expose the
file's existence, its size and its mtime -- and on a directory whose mode was
never stated, that is what the default gives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import effects
from .effects import write_json_atomic_secret
from .tokens import (
    TokenExpiry,
    TokenExpiryDisposition,
    TokenStoreError,
    build_entry,
    entry_expiry,
    entry_plan_env,
    parse_entry,
)

__all__ = [
    "PROFILE_DATA_DIRNAME",
    "PROFILE_DATA_DIR_MODE",
    "TOKEN_FILE_MODE",
    "TOKEN_FILE_NAME",
    "ProfileDataStore",
]

# The dot-prefixed subdirectory inside a profile directory that holds
# claudewheel's own per-profile data. Everything else in a profile directory
# belongs to Claude Code.
PROFILE_DATA_DIRNAME = ".claudewheel"

# Owner-only. Nothing in the codebase set a directory mode before this store, so
# the mode has to be stated: the default would be world-readable and would leak
# the token file's name, size and mtime through the directory listing.
PROFILE_DATA_DIR_MODE = 0o700

# The token entry file, and the mode the secret writer gives it.
TOKEN_FILE_NAME = "token.json"
TOKEN_FILE_MODE = 0o600


@dataclass(frozen=True)
class ProfileDataStore:
    """Path-injected read/write facade over ONE profile's claudewheel data.

    *profile_dir* is the profile's config dir (its ``CLAUDE_CONFIG_DIR``); every
    path is derived from it and nothing here reads a module path constant or
    calls ``Path.home()``.  Construction is pure value assembly -- no directory
    is created until a write happens.

    Read APIs raise :class:`~claudewheel.tokens.TokenStoreError` on a corrupt or
    unreadable token file; a *missing* file is not an error and reads as "this
    profile has no claudewheel data".
    """

    profile_dir: Path

    # --- Paths -----------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """The dot-prefixed subdirectory holding this profile's data."""
        return self.profile_dir / PROFILE_DATA_DIRNAME

    @property
    def token_file(self) -> Path:
        """The token entry file inside :attr:`data_dir`."""
        return self.data_dir / TOKEN_FILE_NAME

    def exists(self) -> bool:
        """True when this profile carries a claudewheel data directory."""
        return self.data_dir.is_dir()

    # --- Reads -----------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """Parse the token entry. Missing -> ``{}``; corrupt -> TokenStoreError."""
        try:
            raw = self.token_file.read_text()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise TokenStoreError(self._corrupt_message(e)) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TokenStoreError(self._corrupt_message(e)) from e
        if not isinstance(data, dict):
            raise TokenStoreError(
                self._corrupt_message("top-level JSON is not an object")
            )
        return data

    def _corrupt_message(self, reason: object) -> str:
        """The one wording for an unusable token file."""
        return (
            f"{self.token_file} is corrupt or unreadable ({reason}); "
            "token resolution cannot proceed. Fix or remove the file, then retry."
        )

    def token(self) -> str | None:
        """The token string, or None when there is none stored."""
        return parse_entry(self.load())

    def has_token(self) -> bool:
        """True when a token string is stored for this profile."""
        return self.token() is not None

    def expiry(self) -> TokenExpiry | None:
        """Computed expiry of the stored entry, or None when there is no entry."""
        entry = self.load()
        if not entry:
            return None
        return entry_expiry(entry)

    def tier(self) -> tuple[str | None, str | None]:
        """The declared ``(rateLimitTier, subscriptionType)``, unvalidated.

        The raw pair as stored, for reporting surfaces that show what is on disk
        rather than resolving it into launch environment variables (which
        validates -- see :meth:`plan_env`).
        """
        entry = self.load()
        tier = entry.get("rateLimitTier")
        subscription = entry.get("subscriptionType")
        return (
            tier if isinstance(tier, str) and tier else None,
            subscription if isinstance(subscription, str) and subscription else None,
        )

    def plan_env(self) -> dict[str, str]:
        """The declared plan tier as Claude Code env vars, validated.

        An unrecognized value is a :class:`ValueError` naming the field, the
        file and the accepted values -- never a silently ignored field, because
        Claude Code treats a value it does not know exactly like no value.
        """
        return entry_plan_env(self.load(), source=str(self.token_file))

    # --- Writes ----------------------------------------------------------

    def ensure_dir(self) -> None:
        """Create :attr:`data_dir` if absent and set its mode explicitly."""
        effects.mkdir(self.data_dir, parents=True, exist_ok=True)
        effects.chmod(self.data_dir, PROFILE_DATA_DIR_MODE)

    def write_token(
        self,
        token: str,
        *,
        expiry: TokenExpiryDisposition,
        tier: str | None = None,
        subscription: str | None = None,
        today: date | None = None,
    ) -> None:
        """Write the token entry, replacing whatever was there.

        *expiry* is required: the caller must choose how the token's lifetime is
        recorded (see :class:`~claudewheel.tokens.TokenExpiryDisposition`), so a
        lifetime is never silently fabricated.  The directory is created at
        0700 and the file written 0600 from creation.
        """
        self.ensure_dir()
        write_json_atomic_secret(
            self.token_file,
            build_entry(
                token, expiry=expiry, tier=tier, subscription=subscription, today=today
            ),
        )

    def set_tier(
        self, *, tier: str | None = None, subscription: str | None = None
    ) -> None:
        """Merge plan-tier fields into the entry, creating it if absent.

        Passing neither field is a no-op.  A corrupt entry file raises
        :class:`~claudewheel.tokens.TokenStoreError` rather than being
        overwritten.
        """
        if tier is None and subscription is None:
            return
        entry = self.load()
        if tier is not None:
            entry["rateLimitTier"] = tier
        if subscription is not None:
            entry["subscriptionType"] = subscription
        self.ensure_dir()
        write_json_atomic_secret(self.token_file, entry)

    def remove_token(self) -> bool:
        """Delete the token entry file. True when it existed."""
        if not self.token_file.exists():
            return False
        effects.remove(self.token_file, missing_ok=True)
        return True
