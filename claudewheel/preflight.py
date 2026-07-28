"""Pre-launch step framework: a deterministic sequence of gate steps.

A "preflight" is a fixed, registration-ordered list of steps that run after
state has been saved but before the launch config is resolved and the child
process is exec'd. Each step inspects a shared :class:`PreflightContext` and
returns a :class:`StepResult` that either lets the sequence CONTINUE or ABORTs
it with an actionable message. A step that decides it has nothing to do simply
CONTINUEs (there is no separate "skip" verdict -- skipping is CONTINUE without
acting).

The framework is intentionally content-free: :data:`PREFLIGHT_STEPS` starts
empty and later phases register concrete steps. The runner is fully testable
with synthetic steps.

UI-rendering steps (``renders_ui=True``) are responsible for constructing and
tearing down their own raw-mode terminal; the call site runs in cooked mode.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .binaries import BinaryLocator
    from .config import AppConfigStore
    from .workspace import Workspace


class Decision(Enum):
    """The two verdicts a preflight step can return."""

    CONTINUE = "continue"
    ABORT = "abort"


@dataclass(frozen=True)
class StepResult:
    """The outcome of running a single preflight step.

    ``ABORT`` carries an actionable ``message`` explaining why the launch was
    stopped; ``CONTINUE`` carries no message.
    """

    decision: Decision
    message: str = ""

    @classmethod
    def cont(cls) -> "StepResult":
        """A CONTINUE result (the sequence proceeds to the next step)."""
        return cls(Decision.CONTINUE)

    @classmethod
    def abort(cls, message: str) -> "StepResult":
        """An ABORT result carrying an actionable *message*."""
        return cls(Decision.ABORT, message)

    @property
    def is_abort(self) -> bool:
        """True when this result stops the sequence."""
        return self.decision is Decision.ABORT


@dataclass(frozen=True)
class PreflightContext:
    """Shared, read-only-ish state handed to every preflight step.

    ``interactive`` is False on the skip-TUI/print path; steps that render UI or
    otherwise require a human are gated on it via
    :attr:`PreflightStep.runs_in_non_interactive`.
    """

    selections: dict[str, str | None]
    workspace: "Workspace"
    locator: "BinaryLocator"
    cfg: "AppConfigStore"
    interactive: bool


@dataclass(frozen=True)
class PreflightStep:
    """A single registered step in the preflight sequence.

    - ``name``: stable identifier, used in diagnostics.
    - ``runs_in_non_interactive``: when False, the step is skipped entirely on
      the non-interactive (print/skip-TUI) path.
    - ``renders_ui``: when True, the step manages its own raw-mode terminal; the
      call site guarantees the terminal is in cooked mode on entry.
    - ``run``: the callable that inspects the context and returns a StepResult.
    """

    name: str
    runs_in_non_interactive: bool
    renders_ui: bool
    run: Callable[[PreflightContext], StepResult]


def _reconcile_guardrails_run(ctx: PreflightContext) -> StepResult:
    """Heal the guardrail surface to canonical before every launch.

    Runs the unified reconcile core over shared-settings and all managed
    profiles (the ``"default"`` profile is excluded by the core). This is a
    best-effort self-heal: it deploys missing hook scripts and prunes drift to
    canonical, and it NEVER aborts a launch. Per-target load/write errors are
    already captured inside the core; any residual, unexpected failure is
    swallowed here so a reconcile problem can never block launching. Concurrent
    launches racing on the same files are fine -- the output is idempotent.
    """
    try:
        from .reconcile import reconcile_workspace

        reconcile_workspace(ctx.workspace, dry_run=False, profile=None)
    except Exception:
        # Reconcile is a non-fatal heal: a failure here must not stop the launch.
        pass
    return StepResult.cont()


def _model_version_guard_run(ctx: PreflightContext) -> StepResult:
    """Block launching a model on a Claude Code binary that is too old.

    Reads the selected model (stripping a trailing ``[1m]`` context-window
    suffix) and looks up its minimum CLI version in
    :data:`MODEL_MIN_CLI_VERSION`. Models absent from the table pass unguarded.
    The effective binary version is the selected version if set, else the
    resolved symlink target's version name. If no version can be determined the
    guard passes (it only acts on a positive too-old determination). A binary
    older than the model's minimum aborts with an actionable message.
    """
    from .defaults import MODEL_MIN_CLI_VERSION
    from .segment import version_sort_key

    model = ctx.selections.get("model")
    if not model:
        return StepResult.cont()
    # Strip the "[1m]" context-window suffix before table lookup.
    if model.endswith("[1m]"):
        model = model[: -len("[1m]")]
    min_version = MODEL_MIN_CLI_VERSION.get(model)
    if min_version is None:
        return StepResult.cont()

    # Effective binary version: explicit selection wins; else the symlink target.
    version = ctx.selections.get("version")
    if not version:
        target = ctx.locator.symlink_target()
        version = target.name if target is not None else None
    if not version:
        return StepResult.cont()

    if version_sort_key(version) >= version_sort_key(min_version):
        return StepResult.cont()

    return StepResult.abort(
        f"Model {model} requires Claude Code {min_version} or newer, "
        f"but the effective binary version is {version}. "
        f"Run `claudewheel install {min_version}` (or a newer version) "
        f"and select it before launching."
    )


def _make_terminal():  # type: ignore[no-untyped-def]
    """Construct the raw-capable Terminal for an approval page.

    Isolated so tests can substitute a FakeTerminal. Requires a real TTY; a
    headless environment fails here, loudly.
    """
    from .terminal import Terminal

    return Terminal()


def _prompt_hook_approval(
    ctx: PreflightContext, listing: list[str], changed: bool
) -> bool:
    """Render the approval page and return True iff the user approves.

    Constructs a themed Terminal the way cli.py's non-TUI interactive flows do,
    lists every hook (event, matcher, command), and offers approve/decline keys.
    Approve is the ``y`` key; anything else -- including ``n``, ``q``, ESC, or an
    interrupt -- declines. The terminal is closed on the way out.
    """
    from .config import resolve_theme_name
    from .theme import parse_theme
    from .ui import show_page

    theme_name = resolve_theme_name(ctx.cfg.config.get("theme", "auto"))
    theme = parse_theme(ctx.cfg.load_theme(theme_name))

    verb = "changed its" if changed else "contributes"
    title = f"This project {verb} Claude Code hooks"
    lines = ["The target project would run these hooks:", ""]
    lines.extend(listing)
    lines.append("")
    lines.append(
        "Approve only if you trust them -- they run arbitrary commands."
    )
    hint = "y: approve and continue   n/esc: decline and abort"

    terminal = _make_terminal()
    try:
        key = show_page(title, lines, theme, terminal, hint=hint)
    finally:
        terminal.close()
    return key in ("y", "Y")


def _approved_hooks_run(ctx: PreflightContext) -> StepResult:
    """Gate the launch on the target project's Claude Code hooks being approved.

    Reads the target project's hooks (``.claude/settings.json`` +
    ``settings.local.json``). Malformed config aborts, naming the broken file.
    No hooks -> CONTINUE (nothing stored). Otherwise the combined fingerprint is
    compared against the stored approval for this project (keyed by the
    realpath-canonical directory):

    - matching fingerprint -> CONTINUE, no prompt;
    - missing or changed fingerprint, interactive -> render the approval page;
      approve persists the fingerprint and CONTINUEs, decline (or ESC/quit)
      ABORTs;
    - missing or changed fingerprint, non-interactive -> ABORT with an
      actionable message (never silent trust).
    """
    from .appdata import StateFile
    from .project_hooks import (
        MalformedProjectHooksError,
        read_project_hooks,
        target_directory,
    )
    from .state import get_project_hook_approvals, set_project_hook_approvals

    directory = target_directory(ctx.selections)
    try:
        hooks = read_project_hooks(directory)
    except MalformedProjectHooksError as e:
        return StepResult.abort(
            f"The target project's Claude Code hooks config is malformed: "
            f".claude/{e.filename} could not be parsed as JSON. "
            f"Fix or remove it before launching."
        )

    if not hooks.has_hooks:
        return StepResult.cont()

    sf = StateFile(ctx.workspace.state_file)
    stored = get_project_hook_approvals(sf, directory)
    fingerprint = hooks.fingerprint
    if stored == fingerprint:
        return StepResult.cont()

    changed = stored is not None

    if not ctx.interactive:
        what = "changed its" if changed else "contributes"
        return StepResult.abort(
            f"The target project {what} Claude Code hooks that have not been "
            f"approved. These hooks run arbitrary commands. Run an interactive "
            f"launch (the TUI) to review and approve them before launching."
        )

    if _prompt_hook_approval(ctx, hooks.listing_lines(), changed):
        set_project_hook_approvals(sf, directory, fingerprint)
        return StepResult.cont()

    return StepResult.abort(
        "Declined the target project's Claude Code hooks. Launch aborted."
    )


# Registered steps, executed in this exact (registration) order. The runner
# defaults to this list.
PREFLIGHT_STEPS: list[PreflightStep] = [
    PreflightStep(
        name="reconcile-guardrails",
        runs_in_non_interactive=True,
        renders_ui=False,
        run=_reconcile_guardrails_run,
    ),
    PreflightStep(
        name="model-version-guard",
        runs_in_non_interactive=True,
        renders_ui=False,
        run=_model_version_guard_run,
    ),
    PreflightStep(
        name="approved-hooks",
        runs_in_non_interactive=True,
        renders_ui=True,
        run=_approved_hooks_run,
    ),
]


def run_preflight(
    ctx: PreflightContext,
    steps: Sequence[PreflightStep] | None = None,
) -> StepResult | None:
    """Run *steps* in order against *ctx*, honoring the non-interactive gate.

    When ``ctx.interactive`` is False, steps whose ``runs_in_non_interactive`` is
    False are skipped. The first ABORT halts the sequence and is returned to the
    caller. Returns None when every applicable step CONTINUEs.

    *steps* defaults to the module-level :data:`PREFLIGHT_STEPS`; tests pass a
    synthetic list.
    """
    if steps is None:
        steps = PREFLIGHT_STEPS
    for step in steps:
        if not ctx.interactive and not step.runs_in_non_interactive:
            continue
        result = step.run(ctx)
        if result.is_abort:
            return result
    return None
