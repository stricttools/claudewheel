"""Client adapters: map resolved launch inputs to a client-specific argv.

claudewheel can launch different Claude-compatible clients. Each client is an
"adapter" -- a function that turns the shared launch context (resolved binary
inputs, model id, selections, session flags) into the concrete argv handed to
``os.execvpe``. The seam lets claudewheel target the official ``claude`` binary
or an alternative client like ``miniclaude`` without special-casing launch.py.

Adapters:

- ``claude``: the official Claude Code CLI. Preserves claudewheel's historical
  argv exactly (``default_flags`` + strict-mcp + permission + model +
  ``--disallowedTools`` + session/passthrough flags), with the binary chosen by
  the :class:`~claudewheel.binaries.BinaryLocator`.
- ``miniclaude``: the miniclaude REPL client. Builds ``miniclaude repl`` with a
  mapped permission mode and mapped session flags. Selections that only make
  sense for the claude client (an explicit version, strict MCP) are HARD
  ERRORS, never silent drops. By-definition claude-only inputs that simply do
  not apply -- ``config.default_flags`` (raw claude CLI flags) and the
  ``DISALLOWED_TOOLS`` constant -- are ignored without error.

Every hard error raised here is a :class:`ValueError` so the CLI launch
sequence catches it and prints a clean, actionable message instead of a
traceback.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .binaries import BinaryLocator


@dataclass
class ClientContext:
    """Everything an adapter needs to build a client argv.

    :func:`claudewheel.launch.resolve_launch_config` assembles the shared pieces
    (profile env, cwd, gh token, resolved model id) and hands the rest to the
    selected adapter.

    ``extra_flags`` is the claude-form session flags followed by the passthrough
    tail (``session flags + passthrough``); ``passthrough`` is that same tail on
    its own. The claude adapter appends ``extra_flags`` verbatim; the miniclaude
    adapter uses :meth:`session_flags` (the prefix with the passthrough tail
    removed) and rejects any passthrough outright.
    """

    selections: dict[str, str | None]
    model_id: str | None
    default_flags: list[str]
    disallowed_tools: list[str]
    extra_flags: list[str]
    passthrough: list[str]
    locator: BinaryLocator
    clients_config: dict

    def session_flags(self) -> list[str]:
        """The session-flag prefix of ``extra_flags`` with the passthrough tail removed."""
        if not self.passthrough:
            return list(self.extra_flags)
        return self.extra_flags[: len(self.extra_flags) - len(self.passthrough)]


def build_claude_argv(ctx: ClientContext) -> list[str]:
    """Build the argv for the official ``claude`` CLI.

    Byte-for-byte identical to claudewheel's historical argv assembly:
    ``[binary] + default_flags + strict-mcp + permission + model +
    --disallowedTools + extra_flags``. The binary is the selected version's
    on-disk path (a missing version is an :class:`OSError`) or the locator's
    fallback symlink when no version is selected.
    """
    version = ctx.selections.get("version")
    if version:
        binary = ctx.locator.binary_for(version)
        if not binary.is_file():
            raise OSError(
                f"Version {version} is not on disk. "
                f"Use the TUI to install it, or run: "
                f"python3 -m claudewheel --install {version}"
            )
        binary_path = str(binary)
    else:
        binary_path = str(ctx.locator.fallback)

    mcp = ctx.selections.get("mcp")
    mcp_flags = ["--strict-mcp-config"] if mcp == "strict" else []

    model_flags = ["--model", ctx.model_id] if ctx.model_id else []

    perm = ctx.selections.get("permissions")
    perm_flags: list[str] = []
    if perm == "bypass":
        perm_flags = ["--dangerously-skip-permissions"]
    elif perm in ("default", "plan", "auto"):
        perm_flags = [f"--permission-mode={perm}"]

    disallowed_flags = (
        ["--disallowedTools"] + ctx.disallowed_tools if ctx.disallowed_tools else []
    )

    argv = (
        [binary_path]
        + ctx.default_flags
        + mcp_flags
        + perm_flags
        + model_flags
        + disallowed_flags
    )
    if ctx.extra_flags:
        argv += ctx.extra_flags
    return argv


# claudewheel permission value -> miniclaude ``repl --permission-mode`` choice.
# miniclaude's choices are: default, acceptEdits, plan, bypassPermissions,
# dontAsk, auto (confirmed via ``miniclaude repl --help``).
_MINICLAUDE_PERMISSION_MAP = {
    "bypass": "bypassPermissions",
    "default": "default",
    "plan": "plan",
    "auto": "auto",
}


def build_miniclaude_argv(ctx: ClientContext) -> list[str]:
    """Build the argv for the ``miniclaude`` REPL client.

    Shape: ``[binary, "repl", "--profile", <profile>, "--model", <model id>,
    "--permission-mode", <mapped>] + <session flags>``. ``--model`` and
    ``--permission-mode`` are included only when the corresponding selection is
    present. Claude-only selections (an explicit version, strict MCP), a missing
    profile, an unsupported session flag, and passthrough args are all HARD
    ERRORS -- never silent drops.
    """
    # Reject claude-only selections loudly rather than dropping them silently.
    version = ctx.selections.get("version")
    if version:
        raise ValueError(
            f"version selection {version!r} is claude-client-only: the miniclaude "
            f"client does not use claudewheel-managed claude binaries"
        )
    if ctx.selections.get("mcp") == "strict":
        raise ValueError(
            "mcp selection 'strict' is claude-client-only: the miniclaude client "
            "has no --strict-mcp-config equivalent"
        )

    profile = ctx.selections.get("profile")
    if not profile:
        raise ValueError("the miniclaude client requires a claudewheel profile")

    # Binary: configured path wins, else PATH lookup, else hard error.
    configured = ctx.clients_config.get("miniclaude", {}).get("binary")
    binary_path = configured or shutil.which("miniclaude")
    if not binary_path:
        raise ValueError(
            "miniclaude binary not found; install it or set clients.miniclaude.binary"
        )

    argv = [binary_path, "repl", "--profile", profile]

    if ctx.model_id:
        argv += ["--model", ctx.model_id]

    perm = ctx.selections.get("permissions")
    if perm:
        mapped = _MINICLAUDE_PERMISSION_MAP.get(perm)
        if mapped is None:
            raise ValueError(
                f"permission mode {perm!r} is not supported by the miniclaude client "
                f"(supported: {', '.join(_MINICLAUDE_PERMISSION_MAP)})"
            )
        argv += ["--permission-mode", mapped]

    argv += _miniclaude_session_flags(ctx)
    return argv


def _miniclaude_session_flags(ctx: ClientContext) -> list[str]:
    """Translate claude-form session flags into miniclaude equivalents.

    ``--continue`` -> ``--continue-session``; ``--resume <id>`` -> ``--resume
    <id>``. A bare ``--resume`` (claude's session picker), ``--print``/``-p``,
    and any passthrough args are HARD ERRORS: miniclaude has no session picker,
    no print mode, and no generic passthrough.
    """
    if ctx.passthrough:
        raise ValueError(
            "the miniclaude client does not support passthrough arguments after "
            f"'--'; got: {' '.join(ctx.passthrough)}"
        )

    session = ctx.session_flags()
    if not session:
        return []

    head = session[0]
    if head == "--continue":
        return ["--continue-session"]
    if head == "--resume":
        if len(session) < 2 or not session[1]:
            raise ValueError(
                "the miniclaude client has no session picker: --resume requires an "
                "explicit session id (a bare --resume/--picker is claude-client-only)"
            )
        return ["--resume", session[1]]
    if head == "--print":
        raise ValueError(
            "print mode (--print/-p) is not supported by the miniclaude client"
        )
    raise ValueError(f"unsupported session flag for the miniclaude client: {head!r}")


CLIENT_ADAPTERS = {
    "claude": build_claude_argv,
    "miniclaude": build_miniclaude_argv,
}

# Ordered client names for CLI --client choices; "claude" first keeps it the
# natural default.
CLIENT_NAMES = tuple(CLIENT_ADAPTERS)

DEFAULT_CLIENT = "claude"
