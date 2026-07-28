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


# Registered steps, executed in this exact (registration) order. The runner
# defaults to this list.
PREFLIGHT_STEPS: list[PreflightStep] = [
    PreflightStep(
        name="reconcile-guardrails",
        runs_in_non_interactive=True,
        renders_ui=False,
        run=_reconcile_guardrails_run,
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
