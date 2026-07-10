"""Map TUI selections to binary path, env vars, flags, and exec."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .binaries import BinaryLocator
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
    options_def: dict,
    default_flags: list[str],
    locator: BinaryLocator,
    profiles: ProfileStore,
    extra_flags: list[str] | None = None,
    metadata: dict[str, dict[str, dict]] | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Build (cwd, argv, env) for os.execvpe from TUI selections.

    Maps segment values to their concrete effects:
    - profile -> CLAUDE_CONFIG_DIR + OAuth token env vars via *profiles*
    - github -> GH_TOKEN env var (fetched live via gh CLI)
    - version -> binary path (under ~/.local/share/claude/versions/)
    - directory -> os.chdir target
    - mcp -> --strict-mcp-config flag (if "strict")
    - permissions -> --dangerously-skip-permissions or --permission-mode=X

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

    # 3. Version -> binary path
    version = selections.get("version")
    if version:
        binary = locator.binary_for(version)
        binary_path = str(binary)
        if not binary.is_file():
            raise OSError(
                f"Version {version} is not on disk. "
                f"Use the TUI to install it, or run: "
                f"python3 -m claudewheel --install {version}"
            )
    else:
        # Fall back to the symlink if no version selected
        binary_path = str(locator.fallback)

    # 4. Directory -> cwd
    directory = selections.get("directory")
    if directory:
        cwd = str(Path(directory).expanduser())
    else:
        cwd = os.getcwd()

    # 5. MCP flags
    mcp = selections.get("mcp")
    mcp_flags = ["--strict-mcp-config"] if mcp == "strict" else []

    # 5b. Model flag -- value is the model ID directly, or looked up from metadata
    model_name = selections.get("model")
    model_flags: list[str] = []
    if model_name:
        if metadata and "model" in metadata:
            model_meta = metadata["model"]
        else:
            model_meta = options_def.get("model", {}).get("metadata", {})
        model_id = model_meta.get(model_name, {}).get("model_id", model_name)
        model_flags = ["--model", model_id]

    # 6. Permission flags
    perm = selections.get("permissions")
    perm_flags: list[str] = []
    if perm == "bypass":
        perm_flags = ["--dangerously-skip-permissions"]
    elif perm in ("default", "plan", "auto"):
        perm_flags = [f"--permission-mode={perm}"]

    # 7. Disallowed tools -- passed as CLI flags so the model never sees them
    disallowed_flags = ["--disallowedTools"] + DISALLOWED_TOOLS if DISALLOWED_TOOLS else []

    # 8. Environment
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

    # 9. Argv
    argv = [binary_path] + default_flags + mcp_flags + perm_flags + model_flags + disallowed_flags
    if extra_flags:
        argv += extra_flags

    return (cwd, argv, env)


def do_launch(cwd: str, argv: list[str], env: dict[str, str]) -> None:
    """Change to directory and exec Claude Code. Does not return."""
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)
