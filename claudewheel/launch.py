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
    except subprocess.TimeoutExpired, FileNotFoundError:
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

# The environment the claudewheel-tool-scope shell prefix reads.
TOOL_ENV_KEYS: tuple[str, ...] = (
    "CLAUDE_CODE_SHELL_PREFIX",
    "CLAUDEWHEEL_TOOL_SLICE",
    "CLAUDEWHEEL_SESSION_SCOPE",
    "CLAUDEWHEEL_TOOL_MEMORY_MAX",
    "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX",
)


@dataclass(frozen=True)
class ToolCap:
    """The memory cap a launched session's Bash commands share.

    Read from config.json's ``tool_memory_max`` and ``tool_memory_swap_max``.
    Claude Code itself is not capped: every Bash command the session runs, and
    everything that command starts, runs in the session's tools slice, which
    caps them together, so a runaway command is killed while the session goes
    on (see :class:`SessionUnits`). ``heavy`` jobs run in scopes of their own
    outside the slice, limited by their own caps and never counted against it.
    """

    memory_max: str
    memory_swap_max: str

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ToolCap":
        """The cap config.json declares; a missing or malformed value raises ValueError."""
        values: list[str] = []
        for key, zero_allowed in (
            ("tool_memory_max", False),
            ("tool_memory_swap_max", True),
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
                    f"T suffix (such as 6G){zero}, not {value!r}"
                )
            values.append(value)
        return cls(values[0], values[1])


# A session slice claudewheel.launch starts sessions in.
SESSION_SLICE_RE = re.compile(r"^claudewheel-(\d+)_(\d+)\.slice$")


@dataclass(frozen=True)
class SessionUnits:
    """The systemd user units one launched session runs in.

    Named from the Claude Code process id (the launcher's own, which the exec
    keeps) and the launch second::

        claudewheel.slice
          claudewheel-<pid>_<time>.slice               the session, uncapped
            claudewheel-session-<pid>-<time>.scope     Claude Code and its hooks
            claudewheel-<pid>_<time>-tools.slice       the ToolCap, shared
              claudewheel-tool-<pid>-<time>-<n>.scope  one Bash command each

    A slice's dashes name its parents, so the tools slice sits inside the
    session slice by its name alone. The launcher starts the session scope;
    the claudewheel-tool-scope shell prefix creates the tools slice with its
    cap at the session's first Bash command and starts each tool scope.
    """

    pid: int
    launched: int

    @property
    def session_slice(self) -> str:
        return f"claudewheel-{self.pid}_{self.launched}.slice"

    @property
    def session_scope(self) -> str:
        return f"claudewheel-session-{self.pid}-{self.launched}.scope"

    @property
    def tool_slice(self) -> str:
        return f"claudewheel-{self.pid}_{self.launched}-tools.slice"

    def argv(self, systemd_run: str, client_argv: list[str]) -> list[str]:
        """*client_argv* started inside the session scope, in the session slice.

        ``--scope`` makes systemd-run exec the client in place, so the session
        keeps this process's PID, terminal, and environment. ``--collect``
        unloads the scope when it ends, failed or not, so none accumulate.
        ``--expand-environment=no`` passes the client's arguments through as
        they are: systemd-run would otherwise expand ``$VAR`` and ``$$`` in
        them, rewriting a prompt that mentions a price. The scope has no
        memory cap. ``OOMPolicy=continue`` keeps the session alive when the
        kernel kills one of its processes (a hook, say) because the machine ran
        out of memory: without it systemd stops the whole scope on the first
        kill.
        """
        return [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--expand-environment=no",
            f"--slice={self.session_slice}",
            f"--unit={self.session_scope}",
            "-p",
            "OOMPolicy=continue",
            "--",
            *client_argv,
        ]

    def env(self, cap: ToolCap, prefix: Path) -> dict[str, str]:
        """What the session's environment carries for the shell prefix."""
        return {
            "CLAUDE_CODE_SHELL_PREFIX": str(prefix),
            "CLAUDEWHEEL_TOOL_SLICE": self.tool_slice,
            "CLAUDEWHEEL_SESSION_SCOPE": self.session_scope,
            "CLAUDEWHEEL_TOOL_MEMORY_MAX": cap.memory_max,
            "CLAUDEWHEEL_TOOL_MEMORY_SWAP_MAX": cap.memory_swap_max,
        }


# A session slice younger than this is never swept: its session scope may not
# have its process in it yet.
SWEEP_AFTER_SECONDS = 60

# Where the cgroup v2 hierarchy is mounted.
CGROUP_ROOT = Path("/sys/fs/cgroup")


def sweep_ended_sessions(*, now: float) -> list[str]:
    """Stop the slices of sessions that ended; return the ones stopped.

    systemd keeps an empty slice active until it is stopped, so each ended
    session would leave its session slice and tools slice behind. A session
    slice holds its Claude Code process for as long as the session lives and
    each Bash command for as long as it runs, so an empty one belongs to a
    session that ended with nothing left running; stopping it stops its tools
    slice too and ends no process. A slice launched less than
    ``SWEEP_AFTER_SECONDS`` ago is left alone. Sessions launched before
    claudewheel started them in slices run in no slice of this form and are
    never touched.
    """
    listing = effects.run(
        [
            "systemctl",
            "--user",
            "list-units",
            "--type=slice",
            "--state=active",
            "--plain",
            "--no-legend",
            "claudewheel-*.slice",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        read=True,
    )
    candidates = []
    for line in (listing.stdout or "").splitlines():
        fields = line.split()
        if not fields:
            continue
        match = SESSION_SLICE_RE.match(fields[0])
        if match and int(match.group(2)) <= now - SWEEP_AFTER_SECONDS:
            candidates.append(fields[0])
    stopped = []
    for name in candidates:
        shown = effects.run(
            ["systemctl", "--user", "show", "-P", "ControlGroup", name],
            capture_output=True,
            text=True,
            timeout=10,
            read=True,
        )
        cgroup = (shown.stdout or "").strip()
        if not cgroup.startswith("/"):
            continue
        try:
            events = (CGROUP_ROOT / cgroup.lstrip("/") / "cgroup.events").read_text()
        except OSError:
            continue
        if "populated 0" not in events.splitlines():
            continue
        effects.run(
            ["systemctl", "--user", "stop", name],
            capture_output=True,
            timeout=30,
        )
        stopped.append(name)
    return stopped


def do_launch(
    cwd: str,
    argv: list[str],
    env: dict[str, str],
    cap: ToolCap,
    scripts_dir: Path,
) -> Any:
    """Change to directory and exec the client inside its session units. Does not return.

    The client runs every shell command through the claudewheel-tool-scope
    script in *scripts_dir* (CLAUDE_CODE_SHELL_PREFIX); the launch adds it and
    the names it reads to *env*, and deploys it when it is missing, as the
    reconcile deploys a missing hook script. Every hook and Bash call of the
    session would fail without it, so one that cannot be run fails the launch,
    as does a systemd-run missing from the launch environment's PATH, the one
    the exec searches: no session starts without its units. The empty slices
    of ended sessions are stopped first (:func:`sweep_ended_sessions`). The
    layout and the cap are printed to stderr, which print mode keeps apart
    from its answer on stdout.

    Under ``--dry-run`` there is nothing to replace this process with: the exec
    is recorded and the carrier standing in for it is returned, so the dispatch
    can finish and the would-do log can render.
    """
    from .hook_scripts import TOOL_SCOPE_SCRIPT, deploy_scripts

    systemd_run = shutil.which("systemd-run", path=env.get("PATH", os.defpath))
    if systemd_run is None:
        raise OSError(
            "systemd-run is not on PATH; claudewheel starts every session in "
            "systemd user units, its Bash commands capped together at "
            "tool_memory_max from config.json"
        )
    prefix = scripts_dir / TOOL_SCOPE_SCRIPT
    if any(c.isspace() for c in str(prefix)):
        raise ValueError(
            f"the shell prefix {prefix} has whitespace in its path, which Claude "
            "Code would split; claudewheel's scripts directory needs a path "
            "without it"
        )
    if not prefix.exists():
        deploy_scripts([TOOL_SCOPE_SCRIPT], scripts_dir)
    if not effects.previewing() and not os.access(prefix, os.X_OK):
        raise OSError(
            f"{prefix} is not executable; it is the shell prefix every command "
            "of the session runs through: redeploy it with 'claudewheel "
            f"deploy-hooks {TOOL_SCOPE_SCRIPT} --force-overwrite'"
        )
    sweep_ended_sessions(now=time.time())
    units = SessionUnits(os.getpid(), int(time.time()))
    print(
        f"claudewheel: this session runs in {units.session_scope}, not memory-"
        f"capped; its Bash commands run in {units.tool_slice}, capped together "
        f"at {cap.memory_max} of memory and {cap.memory_swap_max} of swap "
        "(tool_memory_max and tool_memory_swap_max in config.json)",
        file=sys.stderr,
    )
    return effects.exec_replace(
        cwd,
        units.argv(systemd_run, argv),
        {**env, **units.env(cap, prefix)},
        grant="exec-client",
    )
