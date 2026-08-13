"""Profile auth-shadow repair and running-state detection.

Profile create/delete/rename live in :mod:`claudewheel.profile_store` now; this
module retains only the fix-auth flow and the session running-state check that
callers apply as policy before delegating deletions to the store.  That check is
a delegate: :mod:`claudewheel.session_registry` owns every read of Claude Code's
per-session registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import effects
from . import session_registry
from .effects import write_json_atomic_secret
from .tokens import parse_entry

if TYPE_CHECKING:
    from .workspace import Workspace


@dataclass
class FixAuthResult:
    """Outcome of fix_auth_shadow(): success or a reason for no-op/failure.

    ok: True when the shadow was removed, False otherwise.
    reason: None on success; "no-token" / "no-shadow" / "unreadable-creds" on failure.
    """

    ok: bool
    reason: str | None = None


def fix_auth_shadow(ws: "Workspace", name: str) -> FixAuthResult:
    """Remove session credentials (claudeAiOauth) that shadow a long-lived token.

    Reads the profile's .credentials.json and strips the claudeAiOauth key.
    Whatever plan-tier fields that block carried are discarded with it: the
    declared plan lives in the profile's own token entry and is written there by
    claudewheel's plan picker, never harvested back out of Claude Code's
    credential file. A file left holding nothing is removed rather than kept as
    an empty object, which would go on answering "this profile has credentials"
    to discovery, the inspection report and the permission check.

    Zero printing, zero sys.exit -- returns a FixAuthResult describing what
    happened.

    A corrupt token entry raises :class:`TokenStoreError` (the hard-error
    contract) -- token resolution cannot proceed and the operator must fix it.
    """
    store = ws.profiles
    config_dir = store.path_for(name)
    data = store.data_for(name)

    # 1. Check the profile holds a valid token entry (corrupt -> TokenStoreError).
    if parse_entry(data.load()) is None:
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

    # 3. Strip claudeAiOauth and write back -- or remove the file when the
    #    shadow was all it held.
    creds.pop("claudeAiOauth", None)
    if creds:
        write_json_atomic_secret(creds_path, creds)
    else:
        effects.remove(creds_path, missing_ok=True)

    return FixAuthResult(ok=True)


def _is_profile_running(ws: "Workspace", name: str) -> bool:
    """True when a human's Claude Code session is live in this profile.

    A delegate to :func:`claudewheel.session_registry.has_live_interactive` --
    the single reader of Claude Code's per-session registry, which parses the
    ``sessions/<pid>.json`` files, filters out phantoms (a stale file, or a PID
    the kernel has since handed to something else) and classifies each record by
    kind.  Background jobs, daemons and daemon workers are live processes but do
    not answer True here: they are not a person at a terminal, and the delete
    flow offers the user a choice about them rather than a veto.
    """
    return session_registry.has_live_interactive(ws.profiles.path_for(name))
