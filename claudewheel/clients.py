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
  mapped permission mode and mapped session flags. By-definition claude-only
  inputs that simply do not apply are ignored without error: the ``version``
  selection (it names a claudewheel-managed *claude* binary, which a non-claude
  client never execs), the ``mcp`` selection ("strict" maps to claude's
  ``--strict-mcp-config``; miniclaude has no equivalent),
  ``config.default_flags`` (raw claude CLI flags), and the ``DISALLOWED_TOOLS``
  constant. Ignoring ``version``/``mcp`` here is what lets a plain ``--client
  miniclaude`` succeed even when a claude-only value is remembered in
  last_config or set as a config default; a *contradictory, same-invocation*
  explicit override (``-s version=...`` / ``-s mcp=strict`` together with a
  non-claude ``--client``) is rejected upstream in the CLI, not here.

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
    present. A missing profile, an unsupported session flag, and passthrough
    args are all HARD ERRORS -- never silent drops.

    ``version`` and ``mcp`` selections are IGNORED, not rejected: a version
    names a claudewheel-managed *claude* binary and mcp "strict" maps to
    claude's ``--strict-mcp-config``, so for a non-claude client they are
    by-definition-inapplicable inputs on the same footing as ``default_flags``
    and ``DISALLOWED_TOOLS``. This is what lets ``--client miniclaude`` succeed
    when such a value is merely remembered/configured. A contradictory
    *explicit* override passed in the same invocation is rejected upstream in
    the CLI (``claudewheel.cli``), where the selection's provenance is known.
    """
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

# Segment selections whose only effect is a claude-specific launch input,
# mapped to a predicate over the selected value: True means that value is
# claude-only. Single source of truth for the three enforcement sites:
# the TUI skips these segments for non-claude clients, the CLI drops their
# ambient (remembered/default) values, and the CLI rejects a contradictory
# explicit same-invocation override.
#
# version: any value (it names a claudewheel-managed claude binary).
# mcp: only "strict" is claude-only (it maps to --strict-mcp-config; "default"
#      is a no-op) -- but the segment exists solely for that flag, so the TUI
#      skips the whole segment for non-claude clients.
CLAUDE_ONLY_SELECTIONS: dict[str, object] = {
    "version": lambda v: True,
    "mcp": lambda v: v == "strict",
}


# ---------------------------------------------------------------------------
# Client-selection step (TUI + non-interactive resolution)
# ---------------------------------------------------------------------------


def resolve_default_client(config: dict) -> str:
    """Return the configured ``default_client``, validated against the registry.

    Reads ``config["default_client"]`` (falling back to :data:`DEFAULT_CLIENT`
    when the key is absent). An unknown value is a HARD ERROR
    (:class:`ValueError`) -- never a silent fallback to "claude" -- so a typo in
    config.json fails loudly instead of quietly launching the wrong client.
    """
    name = config.get("default_client", DEFAULT_CLIENT)
    if name not in CLIENT_ADAPTERS:
        raise ValueError(
            f"unknown client {name!r}; known: {', '.join(CLIENT_ADAPTERS)}"
        )
    return name


def client_available(
    name: str, locator: BinaryLocator, clients_config: dict
) -> bool:
    """Report whether *name*'s launch binary is resolvable right now.

    Mirrors each adapter's own binary resolution so the picker's availability
    marking matches what an actual launch would find:

    - ``claude``: the :class:`~claudewheel.binaries.BinaryLocator` fallback
      symlink (what ``build_claude_argv`` execs when no version is selected).
    - ``miniclaude``: the configured ``clients.miniclaude.binary`` or a PATH
      ``miniclaude`` (what ``build_miniclaude_argv`` resolves).

    A client with no known probe is reported available (True): we cannot prove
    it missing, so we never mislabel a freshly added adapter as "not installed".
    """
    if name == "claude":
        return locator.fallback.exists()
    if name == "miniclaude":
        configured = clients_config.get("miniclaude", {}).get("binary")
        return bool(configured or shutil.which("miniclaude"))
    return True


def build_client_choices(
    locator: BinaryLocator, clients_config: dict, default_client: str
) -> tuple[list[tuple[str, str]], str]:
    """Build the ``(options, initial_key)`` pair for the client-selection step.

    Options are the :data:`CLIENT_ADAPTERS` registry entries in registry order
    ("claude" first), as ``(key, label)`` pairs for
    :func:`claudewheel.ui.run_selection`. The key is always the bare client
    name; unavailable clients get a ``" (not installed)"`` label suffix rather
    than being hidden -- selecting one still launches and fails with the
    adapter's own hard-error message. *initial_key* is *default_client*, so the
    cursor starts on the configured default.
    """
    options: list[tuple[str, str]] = []
    for name in CLIENT_ADAPTERS:
        if client_available(name, locator, clients_config):
            label = name
        else:
            label = f"{name} (not installed)"
        options.append((name, label))
    return options, default_client


def resolve_client(explicit_client: str | None, prompt) -> str | None:
    """Resolve the launch client: explicit CLI flag wins, else prompt.

    *explicit_client* is the ``--client`` value when the user passed it, or
    ``None`` when they did not. When it is set, it is returned verbatim and
    *prompt* is NOT called (explicit wins, the TUI step is skipped). Otherwise
    *prompt* (a zero-arg callable that runs the interactive picker) is invoked
    and its result returned -- a client name, or ``None`` if the user cancelled.
    """
    if explicit_client is not None:
        return explicit_client
    return prompt()
