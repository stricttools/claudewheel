"""Resolve a profile name to its launch environment (see ProfileStore.env).

This module is a thin facade over the workspace stores
(:class:`claudewheel.workspace.Workspace`). Its single public function,
:func:`resolve_profile`, maps a profile name to the environment variables
Claude Code needs at launch.

- **Workspace root**: the ``CLAUDEWHEEL_CONFIG_DIR`` environment variable when
  set (expanduser'd), otherwise ``~/.claudewheel``. The root is the only knob;
  everything else is derived from it. A caller that owns its own workspace
  passes it as the ``workspace`` keyword argument instead, and no environment
  variable is consulted.
- **Profile locations are derived from directories**, never persisted: the set
  of profiles is the ``profiles/`` directory scan plus the built-in ``~/.claude``
  default. No ``options.json`` metadata is consulted.
- **Zero filesystem writes, no terminal I/O** -- safe on read-only mounts and
  headless servers.

All resolution work lives in
:meth:`claudewheel.profile_store.ProfileStore.env`; this module only picks the
workspace -- the default one, or the one the caller injected -- and delegates.
"""

from __future__ import annotations

from .workspace import Workspace


def resolve_profile(name: str, *, workspace: Workspace | None = None) -> dict[str, str]:
    """Resolve a profile *name* to its launch environment variables.

    For a named profile the result is the profile-owned launch environment
    that :meth:`claudewheel.profile_store.ProfileStore.env` defines (the
    config dir, the stored token when one exists, and the quieting switches;
    that method's docstring is the one list). The ``"default"`` profile is
    the exception: it is Claude Code's own ``~/.claude`` (managed by Claude
    Code, read-only to cw), so it resolves to an EMPTY dict (the vanilla path).

    Contract:

    - **Unknown profile** -> :class:`ValueError` whose message lists the
      available profile names.
    - **Corrupt or unreadable token entry** ->
      :class:`~claudewheel.tokens.TokenStoreError` (a hard error). A profile
      with no stored token entry at all is NOT an error -- resolution succeeds,
      simply without a token.
    - Profiles are resolved purely from the on-disk workspace layout: the
      ``profiles/`` directory scan plus the built-in ``~/.claude`` default, and
      each profile's token comes from its own claudewheel data directory.
      Profile locations are derived from directories, never persisted;
      ``options.json`` metadata is no longer consulted (a deliberate contract
      change from earlier versions).
    - The workspace root is chosen by the caller through the *workspace*
      parameter, or -- when it is omitted or ``None`` -- by
      :meth:`Workspace.default`: the ``CLAUDEWHEEL_CONFIG_DIR`` environment
      variable when set (expanduser'd), otherwise ``~/.claudewheel``.
    - Zero filesystem writes, zero terminal I/O -- safe for read-only mounts
      and headless servers.

    *workspace* is the injection seam for library consumers and their test
    isolation: pass a :class:`~claudewheel.workspace.Workspace` (built with
    :meth:`Workspace.open`) and resolution happens against that root, reading
    no environment variable at all. Passing ``None`` -- or omitting the
    argument -- keeps the default behavior described above.
    """
    ws = Workspace.default() if workspace is None else workspace
    return ws.profiles.env(name)
