"""Resolve a profile name to CLAUDE_CONFIG_DIR and OAuth token env vars.

This module is a thin facade over the workspace stores
(:class:`claudewheel.workspace.Workspace`). Its single public function,
:func:`resolve_profile`, maps a profile name to the environment variables
Claude Code needs at launch.

- **Workspace root**: the ``CLAUDEWHEEL_CONFIG_DIR`` environment variable when
  set (expanduser'd), otherwise ``~/.claudewheel``. The root is the only knob;
  everything else is derived from it.
- **Profile locations are derived from directories**, never persisted: the set
  of profiles is the ``profiles/`` directory scan plus the built-in ``~/.claude``
  default. No ``options.json`` metadata is consulted.
- **Zero filesystem writes, no terminal I/O** -- safe on read-only mounts and
  headless servers.

All resolution work lives in
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
      ``tokens.json``. Profile locations are derived from directories, never
      persisted; ``options.json`` metadata is no longer consulted (a deliberate
      contract change from earlier versions).
    - The workspace root is chosen by :meth:`Workspace.default`: the
      ``CLAUDEWHEEL_CONFIG_DIR`` environment variable when set (expanduser'd),
      otherwise ``~/.claudewheel``.
    - Zero filesystem writes, zero terminal I/O -- safe for read-only mounts
      and headless servers.
    """
    return Workspace.default().profiles.env(name)
