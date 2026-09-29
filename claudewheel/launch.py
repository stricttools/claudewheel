"""Map TUI selections to binary path, env vars, flags, and exec."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import effects
from .binaries import BinaryLocator
from .clients import CLIENT_ADAPTERS, ClientContext
from .defaults import DISALLOWED_TOOLS
from .profile_store import PROFILE_ENV_KEYS, ProfileStore


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
    *,
    lifecycle_dir: Path,
) -> tuple[str, list[str], dict[str, str]]:
    """Build (cwd, argv, env) for do_launch from TUI selections.

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
    replaced the old silent ~/.claude fallback); a corrupt token entry raises
    :class:`TokenStoreError`.

    The ``"default"`` profile -- selected explicitly OR reached via the
    no-profile fallback -- is the VANILLA path: ``~/.claude`` is Claude Code's
    own config dir, managed by Claude Code and strictly read-only to cw. The
    built env therefore carries NEITHER ``CLAUDE_CONFIG_DIR`` (any ambient one
    inherited from ``os.environ`` is explicitly removed) NOR
    ``CLAUDE_CODE_OAUTH_TOKEN`` (no token injection, even if a token entry
    exists under ``~/.claude``).

    When *metadata* is provided (TUI path), use it for model lookups. When
    None (skip-TUI path), fall back to reading from *options_def*.

    The env also carries the LAUNCH FACTS: ``CLAUDEWHEEL_LIFECYCLE_DIR``
    (*lifecycle_dir*, where the session lifecycle store is) plus one
    ``CLAUDEWHEEL_LAUNCH_*`` variable per selection claudewheel actually made.
    The SessionStart/SessionEnd hook scripts read them, because a session cannot
    otherwise tell which profile, version, model or permissions mode it was
    launched with. A selection claudewheel did not make sets no variable -- and
    clears an inherited one, so a launcher run from inside a launched session
    never passes its parent's facts off as the child's.
    """
    # 1. Profile -> config dir + OAuth token (via ProfileStore; no metadata).
    #    The default (explicit or fallback) is vanilla: no config dir, no token.
    profile = selections.get("profile")
    profile_env: dict[str, str] = {}
    # The non-default test is written inline rather than through a precomputed
    # flag so the type checker narrows `profile` to str for the env() call.
    if profile and profile != "default":
        is_default = False
        # Unknown/stale name -> ValueError; corrupt token entry -> TokenStoreError.
        profile_env = profiles.env(profile)
    else:
        is_default = True

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
        # Vanilla default: strip every profile-owned variable so Claude Code
        # manages ~/.claude entirely on its own. cw injects nothing, and an
        # ambient value inherited from the shell must not stand in for the
        # injection cw is declining to make.
        for key in PROFILE_ENV_KEYS:
            env.pop(key, None)
    else:
        # Everything ProfileStore.env() yields is injected verbatim: the config
        # dir, the OAuth token when one exists, and the declared plan tier. The
        # store decides which keys apply; enumerating them here would mean a new
        # key silently failing to reach the client.
        env.update(profile_env)
    if gh_token:
        env["GH_TOKEN"] = gh_token

    # 5b. The launch facts the lifecycle hooks read. These are claudewheel's own
    #     statements about the launch, not profile-owned values, so the vanilla
    #     default path carries them too. A fact claudewheel did not choose is
    #     REMOVED rather than left inherited: a launcher started from inside a
    #     launched session would otherwise hand the child its parent's profile.
    launch_facts = {
        "CLAUDEWHEEL_LAUNCH_PROFILE": profile,
        "CLAUDEWHEEL_LAUNCH_VERSION": selections.get("version"),
        "CLAUDEWHEEL_LAUNCH_MODEL": model_id,
        "CLAUDEWHEEL_LAUNCH_PERMISSIONS": selections.get("permissions"),
        "CLAUDEWHEEL_LIFECYCLE_DIR": str(lifecycle_dir),
    }
    for key, value in launch_facts.items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)

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


# A size as systemd reads it for MemoryMax and MemorySwapMax, spelled the way
# heavy's --mem is: a whole number with a K, M, G, or T suffix.
_SIZE = re.compile(r"[1-9][0-9]*[KMGT]")


@dataclass(frozen=True)
class SessionScope:
    """The memory ceiling of the systemd user scope a launched session runs in.

    Read from config.json's ``session_memory_max`` and
    ``session_memory_swap_max``. The scope keeps a command that runs away
    inside the session (one no guardrail routed through ``heavy``) from taking
    more than the session's ceiling; ``heavy`` scopes started from inside the
    session land beside it in the user manager's slice, so they are limited by
    their own caps and never counted against this one.
    """

    memory_max: str
    memory_swap_max: str

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SessionScope":
        """The ceiling config.json declares; a missing or malformed value raises ValueError."""
        values: list[str] = []
        for key, zero_allowed in (
            ("session_memory_max", False),
            ("session_memory_swap_max", True),
        ):
            if key not in config:
                raise ValueError(
                    f"config.json has no {key}; claudewheel adds it with its "
                    "default when it starts, so restart claudewheel"
                )
            value = config[key]
            if not isinstance(value, str) or not (
                _SIZE.fullmatch(value) or (zero_allowed and value == "0")
            ):
                zero = ", or 0 for none" if zero_allowed else ""
                raise ValueError(
                    f"config.json {key} takes a whole number with a K, M, G, or "
                    f"T suffix (such as 4G){zero}, not {value!r}"
                )
            values.append(value)
        return cls(values[0], values[1])

    def argv(self, systemd_run: str, unit: str, client_argv: list[str]) -> list[str]:
        """*client_argv* started inside the scope named *unit*.

        ``--scope`` makes systemd-run exec the client in place, so the session
        keeps this process's PID, terminal, and environment. ``--collect``
        unloads the scope when it ends, failed or not, so none accumulate.
        ``--expand-environment=no`` passes the client's arguments through as
        they are: systemd-run would otherwise expand ``$VAR`` and ``$$`` in
        them, rewriting a prompt that mentions a price.
        ``OOMPolicy=continue`` keeps the session alive when the kernel kills
        the largest process at the ceiling (the runaway, not Claude Code):
        without it systemd stops the whole scope on the first kill.
        """
        return [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--expand-environment=no",
            f"--unit={unit}",
            "-p",
            f"MemoryMax={self.memory_max}",
            "-p",
            f"MemorySwapMax={self.memory_swap_max}",
            "-p",
            "OOMPolicy=continue",
            "--",
            *client_argv,
        ]


def do_launch(
    cwd: str, argv: list[str], env: dict[str, str], scope: SessionScope
) -> Any:
    """Change to directory and exec the client inside its session scope. Does not return.

    systemd-run is looked up on the launch environment's PATH, the one the
    exec searches; without it the launch fails with an OSError rather than
    starting a session with no ceiling. The ceiling is printed to stderr, which
    print mode keeps apart from its answer on stdout.

    Under ``--dry-run`` there is nothing to replace this process with: the exec
    is recorded and the carrier standing in for it is returned, so the dispatch
    can finish and the would-do log can render.
    """
    systemd_run = shutil.which("systemd-run", path=env.get("PATH", os.defpath))
    if systemd_run is None:
        raise OSError(
            "systemd-run is not on PATH; claudewheel starts every session in its "
            "own systemd user scope, capped at session_memory_max from config.json"
        )
    unit = f"claudewheel-session-{os.getpid()}-{int(time.time())}.scope"
    print(
        f"claudewheel: this session runs in {unit}, capped at "
        f"{scope.memory_max} of memory and {scope.memory_swap_max} of swap "
        "(session_memory_max and session_memory_swap_max in config.json)",
        file=sys.stderr,
    )
    return effects.exec_replace(
        cwd, scope.argv(systemd_run, unit, argv), env, grant="exec-client"
    )
