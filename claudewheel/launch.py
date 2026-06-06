"""Map TUI selections to binary path, env vars, flags, and exec."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .constants import VERSIONS_DIR, CLAUDE_SYMLINK, TOKENS_FILE
from .defaults import DISALLOWED_TOOLS


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
    extra_flags: list[str] | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """Build (cwd, argv, env) for os.execvpe from TUI selections.

    Maps segment values to their concrete effects:
    - profile -> CLAUDE_CONFIG_DIR env var (from options.json metadata)
    - github -> GH_TOKEN env var (fetched live via gh CLI)
    - version -> binary path (under ~/.local/share/claude/versions/)
    - directory -> os.chdir target
    - mcp -> --strict-mcp-config flag (if "strict")
    - permissions -> --dangerously-skip-permissions or --permission-mode=X
    """
    # 1. Profile -> config dir
    profile = selections.get("profile")
    config_dir = str(Path("~/.claude").expanduser())
    if profile:
        meta = options_def.get("profile", {}).get("metadata", {})
        profile_meta = meta.get(profile, {})
        if "config_dir" in profile_meta:
            config_dir = str(Path(profile_meta["config_dir"]).expanduser())

    # 2. GH token
    gh_account = selections.get("github")
    gh_token = fetch_gh_token(gh_account) if gh_account else None

    # 3. Version -> binary path
    version = selections.get("version")
    if version:
        binary_path = str(VERSIONS_DIR / version)
        if not (VERSIONS_DIR / version).is_file():
            raise OSError(
                f"Version {version} is not on disk. "
                f"Use the TUI to install it, or run: "
                f"python3 -m claudewheel --install {version}"
            )
    else:
        # Fall back to the symlink if no version selected
        binary_path = str(CLAUDE_SYMLINK)

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
    # Long-lived OAuth token (from tokens.json, keyed by profile name)
    # Supports both {name: "token"} and {name: {token, created}} formats.
    if profile and TOKENS_FILE.is_file():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            entry = tokens.get(profile)
            if isinstance(entry, str):
                env["CLAUDE_CODE_OAUTH_TOKEN"] = entry
            elif isinstance(entry, dict) and entry.get("token"):
                env["CLAUDE_CODE_OAUTH_TOKEN"] = entry["token"]
        except (json.JSONDecodeError, OSError):
            pass

    # 9. Argv
    argv = [binary_path] + default_flags + mcp_flags + perm_flags + model_flags + disallowed_flags
    if extra_flags:
        argv += extra_flags

    return (cwd, argv, env)


def do_launch(cwd: str, argv: list[str], env: dict[str, str]) -> None:
    """Change to directory and exec Claude Code. Does not return."""
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)
