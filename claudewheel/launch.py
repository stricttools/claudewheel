"""Map TUI selections to binary path, env vars, flags, and exec."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import effects
from .binaries import BinaryLocator
from .clients import CLIENT_ADAPTERS, ClientContext
from .defaults import DISALLOWED_TOOLS
from .profile_store import ProfileStore


def fetch_gh_token(account: str) -> str | None:
    """Fetch GH token live via gh CLI. Returns None on failure."""
    try:
        result = effects.run(
            ["gh", "auth", "token", "--user", account],
            capture_output=True,
            text=True,
            timeout=5,
            read=True,
        )
        if result.returncode == 0:
            out: str = result.stdout
            return out.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def resolve_launch_config(
    selections: dict[str, str | None],
    options_def: dict[str, Any],
    default_flags: list[str],
    locator: BinaryLocator,
    profiles: ProfileStore,
    extra_flags: list[str] | None = None,
    metadata: dict[str, dict[str, dict[str, Any]]] | None = None,
    client: str = "claude",
    clients_config: dict[str, Any] | None = None,
    passthrough: list[str] | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Build (cwd, argv, env) for os.execvpe from TUI selections.

    Maps segment values to their concrete effects. The env and cwd (the
    target-agnostic pieces) are assembled here; the argv is delegated to the
    selected *client* adapter in :mod:`claudewheel.clients`:

    - profile -> CLAUDE_CONFIG_DIR + OAuth token env vars via *profiles* (shared)
    - github -> GH_TOKEN env var, fetched live via gh CLI (shared)
    - directory -> os.chdir target (shared)
    - model -> resolved model id (shared), then formatted per client
    - version / mcp / permissions / session flags -> client-specific argv

    The *client* names an entry in :data:`claudewheel.clients.CLIENT_ADAPTERS`
    ("claude" preserves the historical behavior exactly; "miniclaude" targets
    the miniclaude REPL). *clients_config* is the ``clients`` section of
    config.json (used by the miniclaude adapter to locate its binary).
    *passthrough* is the tail of *extra_flags* that came from args after ``--``;
    the claude adapter appends it verbatim, the miniclaude adapter rejects it.

    The selected profile is resolved through the injected *profiles*
    ProfileStore -- the single source of profile identity. A named profile that
    no longer exists raises :class:`ValueError` (the hard-error contract that
    replaced the old silent ~/.claude fallback); a corrupt tokens.json raises
    :class:`TokenStoreError`.

    The ``"default"`` profile -- selected explicitly OR reached via the
    no-profile fallback -- is the VANILLA path: ``~/.claude`` is Claude Code's
    own config dir, managed by Claude Code and strictly read-only to cw. The
    built env therefore carries NEITHER ``CLAUDE_CONFIG_DIR`` (any ambient one
    inherited from ``os.environ`` is explicitly removed) NOR
    ``CLAUDE_CODE_OAUTH_TOKEN`` (no token injection, even if a "default" tokens
    key exists).

    When *metadata* is provided (TUI path), use it for model lookups. When
    None (skip-TUI path), fall back to reading from *options_def*.
    """
    # 1. Profile -> config dir + OAuth token (via ProfileStore; no metadata).
    #    The default (explicit or fallback) is vanilla: no config dir, no token.
    profile = selections.get("profile")
    profile_env: dict[str, str] = {}
    is_default = (not profile) or profile == "default"
    if not is_default:
        # Unknown/stale name -> ValueError; corrupt tokens.json -> TokenStoreError.
        profile_env = profiles.env(profile)  # type: ignore[arg-type]

    # 2. GH token
    gh_account = selections.get("github")
    gh_token = fetch_gh_token(gh_account) if gh_account else None

    # 3. Directory -> cwd
    directory = selections.get("directory")
    if directory:
        cwd = str(Path(directory).expanduser())
    else:
        cwd = os.getcwd()

    # 4. Model id -- value is the model ID directly, or looked up from metadata.
    #    Resolution is client-agnostic; each adapter formats the id its own way.
    model_name = selections.get("model")
    model_id: str | None = None
    if model_name:
        if metadata and "model" in metadata:
            model_meta = metadata["model"]
        else:
            model_meta = options_def.get("model", {}).get("metadata", {})
        model_id = model_meta.get(model_name, {}).get("model_id", model_name)

    # 5. Environment (target-agnostic)
    env = dict(os.environ)
    if is_default:
        # Vanilla default: strip any ambient CLAUDE_CONFIG_DIR / OAuth token so
        # Claude Code manages ~/.claude entirely on its own. cw injects nothing.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = profile_env["CLAUDE_CONFIG_DIR"]
        # Long-lived OAuth token, supplied by ProfileStore.env() alongside the
        # config dir. env() adds CLAUDE_CODE_OAUTH_TOKEN only when the store
        # yields a truthy token for the profile; a missing file or absent entry
        # yields none.
        oauth_token = profile_env.get("CLAUDE_CODE_OAUTH_TOKEN")
        if oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    if gh_token:
        env["GH_TOKEN"] = gh_token

    # 6. Argv -- delegated to the selected client adapter.
    adapter = CLIENT_ADAPTERS.get(client)
    if adapter is None:
        raise ValueError(
            f"unknown client {client!r}; available: {', '.join(CLIENT_ADAPTERS)}"
        )
    ctx = ClientContext(
        selections=selections,
        model_id=model_id,
        default_flags=default_flags,
        disallowed_tools=DISALLOWED_TOOLS,
        extra_flags=extra_flags or [],
        passthrough=passthrough or [],
        locator=locator,
        clients_config=clients_config or {},
    )
    argv = adapter(ctx)

    return (cwd, argv, env)


def do_launch(cwd: str, argv: list[str], env: dict[str, str]) -> Any:
    """Change to directory and exec Claude Code. Does not return.

    Under ``--dry-run`` there is nothing to replace this process with: the exec
    is recorded and the carrier standing in for it is returned, so the dispatch
    can finish and the would-do log can render.
    """
    return effects.exec_replace(cwd, argv, env, grant="exec-client")
