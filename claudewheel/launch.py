"""Map TUI selections to binary path, env vars, flags, and exec."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .binaries import BinaryLocator
from .clients import CLIENT_ADAPTERS, ClientContext
from .defaults import DISALLOWED_TOOLS
from .profile_store import ProfileStore


def fetch_gh_token(account: str) -> str | None:
    """Fetch GH token live via gh CLI. Returns None on failure."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--user", account],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
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
    ProfileStore -- the single source of profile identity. A profile that no
    longer exists raises :class:`ValueError` (the hard-error contract that
    replaced the old silent ~/.claude fallback); a corrupt tokens.json raises
    :class:`TokenStoreError`. No profile selected falls back to the store's
    "default" path (~/.claude) with no token.

    When *metadata* is provided (TUI path), use it for model lookups. When
    None (skip-TUI path), fall back to reading from *options_def*.
    """
    # 1. Profile -> config dir + OAuth token (via ProfileStore; no metadata).
    profile = selections.get("profile")
    profile_env: dict[str, str] = {}
    if profile:
        # Unknown/stale name -> ValueError; corrupt tokens.json -> TokenStoreError.
        profile_env = profiles.env(profile)
        config_dir = profile_env["CLAUDE_CONFIG_DIR"]
    else:
        config_dir = str(profiles.path_for("default"))

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
    env["CLAUDE_CONFIG_DIR"] = config_dir
    if gh_token:
        env["GH_TOKEN"] = gh_token
    # Long-lived OAuth token, supplied by ProfileStore.env() alongside the
    # config dir. env() adds CLAUDE_CODE_OAUTH_TOKEN only when the store yields
    # a truthy token for the profile; a missing file or absent entry yields none.
    oauth_token = profile_env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

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


def do_launch(cwd: str, argv: list[str], env: dict[str, str]) -> None:
    """Change to directory and exec Claude Code. Does not return."""
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)
