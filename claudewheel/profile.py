"""Resolve a profile name to CLAUDE_CONFIG_DIR and OAuth token env vars.

This module is a thin facade over :class:`claudewheel.workspace.Workspace`. Its
single public function, :func:`resolve_profile`, maps a profile name to the
environment variables Claude Code needs at launch. It performs **zero
filesystem writes and no terminal I/O**, so it is safe on read-only mounts and
headless servers. All resolution work lives in
:meth:`claudewheel.profile_store.ProfileStore.env`; this module only picks the
default workspace and delegates.
"""

from __future__ import annotations

from .workspace import Workspace


def resolve_profile(name: str) -> dict[str, str]:
    """Resolve a profile *name* to its launch environment variables.

    Returns a dict that always carries ``CLAUDE_CONFIG_DIR`` and additionally
    carries ``CLAUDE_CODE_OAUTH_TOKEN`` when a token exists for *name*.

    Contract:

    - **Unknown profile** -> :class:`ValueError` whose message lists the
      available profile names.
    - **Corrupt or unreadable ``tokens.json``** ->
      :class:`~claudewheel.tokens.TokenStoreError` (a hard error). A *missing*
      tokens.json file or a *missing* entry for *name* is NOT an error --
      resolution succeeds, simply without a token.
    - Profiles are resolved purely from the on-disk workspace layout: the
      ``profiles/`` directory scan, the built-in ``~/.claude`` default, and
      ``tokens.json``. Persisted ``options.json`` metadata is no longer
      consulted (a deliberate contract change from earlier versions).
    - The workspace root is chosen by :meth:`Workspace.default`, which honors an
      environment-variable override and otherwise defaults to ``~/.claudewheel``.
    - Zero filesystem writes, zero terminal I/O -- safe for read-only mounts
      and headless servers.
    """
    return Workspace.default().profiles.env(name)
