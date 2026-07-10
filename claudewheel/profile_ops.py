"""Profile auth-shadow repair and running-state detection.

Profile create/delete/rename live in :mod:`claudewheel.profile_store` now; this
module retains only the fix-auth flow and the session running-state check that
callers apply as policy before delegating deletions to the store.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .constants import PROFILES_DIR, TOKENS_FILE
from .fsutil import write_json_atomic_secret
from .profile_store import ProfileStore
from .tokens import TokenStore, parse_entry


@dataclass
class FixAuthResult:
    """Outcome of fix_auth_shadow(): success or a reason for no-op/failure.

    ok: True when the shadow was removed, False otherwise.
    reason: None on success; "no-token" / "no-shadow" / "unreadable-creds" on failure.
    tier_saved: rateLimitTier value preserved into tokens.json, or None.
    subscription_saved: subscriptionType value preserved into tokens.json, or None.
    """

    ok: bool
    reason: str | None = None
    tier_saved: str | None = None
    subscription_saved: str | None = None


def fix_auth_shadow(name: str) -> FixAuthResult:
    """Remove session credentials (claudeAiOauth) that shadow a long-lived token.

    Reads the profile's .credentials.json, strips the claudeAiOauth key, and
    preserves any tier/subscription metadata into tokens.json. Zero printing,
    zero sys.exit -- returns a FixAuthResult describing what happened.

    A corrupt tokens.json raises :class:`TokenStoreError` (the hard-error
    contract) -- token resolution cannot proceed and the operator must fix it.
    """
    store = ProfileStore(
        PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE)
    )
    config_dir = store.path_for(name)

    # 1. Check tokens.json has a valid entry (corrupt -> TokenStoreError).
    tokens = store.token_store.load()
    if parse_entry(tokens.get(name)) is None:
        return FixAuthResult(ok=False, reason="no-token")

    # 2. Read .credentials.json
    creds_path = config_dir / ".credentials.json"
    if not creds_path.exists():
        return FixAuthResult(ok=False, reason="no-shadow")
    try:
        creds = json.loads(creds_path.read_text())
    except (json.JSONDecodeError, OSError):
        return FixAuthResult(ok=False, reason="unreadable-creds")

    if "claudeAiOauth" not in creds:
        return FixAuthResult(ok=False, reason="no-shadow")

    # 3. Extract tier fields before stripping
    oauth_block = creds["claudeAiOauth"]
    tier = oauth_block.get("rateLimitTier") if isinstance(oauth_block, dict) else None
    sub_type = oauth_block.get("subscriptionType") if isinstance(oauth_block, dict) else None

    if tier or sub_type:
        # Merge tier data into the tokens.json entry. TokenStore.set_tier applies
        # the identical merge (bare-string -> dict upgrade, field-wise set) the
        # inline code used; `or None` preserves the old truthy guard so empty
        # strings are never written.
        store.token_store.set_tier(
            name, tier=tier or None, subscription=sub_type or None
        )

    # 4. Strip claudeAiOauth and write back
    creds.pop("claudeAiOauth", None)
    write_json_atomic_secret(creds_path, creds)

    return FixAuthResult(
        ok=True,
        tier_saved=tier,
        subscription_saved=sub_type,
    )


def _is_profile_running(name: str) -> bool:
    """Check if a profile has active sessions by scanning its sessions/ dir for PID files."""
    profile_dir = PROFILES_DIR / name
    sessions_dir = profile_dir / "sessions"
    if not sessions_dir.is_dir():
        return False
    for entry in sessions_dir.iterdir():
        if entry.suffix == ".pid" and entry.is_file():
            try:
                pid = int(entry.read_text().strip())
                # Check if process is alive (signal 0 = existence check)
                os.kill(pid, 0)
                return True
            except (ValueError, OSError):
                # Stale PID file or process gone -- not running
                continue
    return False
