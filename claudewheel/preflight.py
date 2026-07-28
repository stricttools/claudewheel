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

import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

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


# ---------------------------------------------------------------------------
# Vanilla default guardrail management (opt-in).
#
# The "default" profile is Claude Code's own ~/.claude -- strictly read-only to
# cw. The unified reconcile core UNCONDITIONALLY excludes it. The ONLY way cw
# ever writes ~/.claude/settings.json is when the user explicitly opts in to
# guardrails for the default; even then the write is purely ADDITIVE (via
# merge_hooks) and touches only cw's own hook wiring.
# ---------------------------------------------------------------------------


def _canonical_hook_scripts() -> set[str]:
    """The canonical hook script basenames cw manages (from the guardrail model)."""
    from .hook_scripts import HOOK_SCRIPTS

    return set(HOOK_SCRIPTS)


def ensure_vanilla_guardrails(ws: "Workspace") -> bool:
    """Additively inject cw's canonical hook wiring into ~/.claude/settings.json.

    Deploys any missing guardrail hook scripts, then merges the canonical hook
    wiring into ``~/.claude/settings.json`` via ``merge_hooks`` -- never pruning,
    never touching non-hook keys. Idempotent: when the wiring is already present
    the file is left byte-identical (no write). Returns True iff a write happened.
    """
    import json

    from .defaults import build_canonical_shared_settings
    from .hook_scripts import HOOK_SCRIPTS, deploy_scripts
    from .patch_profiles import merge_hooks
    from .permission import load_settings, save_settings
    from .reconcile import _referenced_scripts

    canonical = build_canonical_shared_settings(ws.scripts_dir)
    canonical_hooks = canonical["hooks"]

    # Deploy any missing referenced hook scripts (wiring pointing at missing
    # scripts would not be functional).
    referenced = _referenced_scripts(canonical_hooks)
    missing = [
        n for n in referenced if n in HOOK_SCRIPTS and not (ws.scripts_dir / n).exists()
    ]
    if missing:
        deploy_scripts(missing, ws.scripts_dir)

    settings_path = ws.claude_dir / "settings.json"
    if settings_path.exists():
        try:
            settings = load_settings(settings_path)
        except (json.JSONDecodeError, OSError):
            settings = {}
    else:
        settings = {}

    before = json.dumps(settings, sort_keys=True)
    hooks = settings.setdefault("hooks", {})
    merge_hooks(hooks, canonical_hooks)
    after = json.dumps(settings, sort_keys=True)
    if before == after:
        return False

    ws.claude_dir.mkdir(parents=True, exist_ok=True)
    save_settings(settings_path, settings)
    return True


def remove_vanilla_guardrails(ws: "Workspace") -> bool:
    """Remove EXACTLY cw's known hook entries from ~/.claude/settings.json.

    Matches by the canonical hook script basenames -- user-authored hooks and
    all non-hook keys are left byte-identical. Hook entries emptied of every cw
    hook are dropped; events emptied of every entry are dropped. Idempotent: no
    cw hooks present -> no write. Returns True iff a write happened.
    """
    import json
    from pathlib import Path

    from .permission import load_settings, save_settings

    settings_path = ws.claude_dir / "settings.json"
    if not settings_path.exists():
        return False
    try:
        settings = load_settings(settings_path)
    except (json.JSONDecodeError, OSError):
        return False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False

    known = _canonical_hook_scripts()
    before = json.dumps(settings, sort_keys=True)

    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        new_entries: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                new_entries.append(entry)
                continue
            original_hooks = entry["hooks"]
            kept = [
                h
                for h in original_hooks
                if not (
                    isinstance(h, dict) and Path(h.get("command", "")).name in known
                )
            ]
            if len(kept) == len(original_hooks):
                # No cw hook in this entry -- preserve it verbatim.
                new_entries.append(entry)
            elif kept:
                entry["hooks"] = kept
                new_entries.append(entry)
            # else: entry held only cw hooks -> drop it entirely.
        if new_entries:
            hooks[event] = new_entries
        else:
            del hooks[event]

    after = json.dumps(settings, sort_keys=True)
    if before == after:
        return False
    save_settings(settings_path, settings)
    return True


def _prompt_vanilla_choice(ctx: PreflightContext) -> bool:
    """Render the one-time vanilla/guardrails choice; return True iff opt-in.

    Builds a themed Terminal the same way the other UI-rendering steps do and
    offers two keys: stay vanilla (the default, any non-``g`` key) or inject cw
    guardrails (``g``). The terminal is closed on the way out.
    """
    from .config import resolve_theme_name
    from .theme import parse_theme
    from .ui import show_page

    theme_name = resolve_theme_name(ctx.cfg.config.get("theme", "auto"))
    theme = parse_theme(ctx.cfg.load_theme(theme_name))

    title = "Launching the default profile (~/.claude)"
    lines = [
        "The 'default' profile is Claude Code's own ~/.claude.",
        "cw treats it as vanilla and strictly read-only: no guardrails,",
        "no hooks, no settings are applied unless you opt in.",
        "",
        "You can opt in to cw's guardrail hooks (block unsafe commands,",
        "worktree isolation, timestamps, advice). This ADDITIVELY writes",
        "cw's hooks into ~/.claude/settings.json and never prunes your own.",
        "",
        "You can change this later from the profile inspect page (press 'i').",
    ]
    hint = "g: enable cw guardrails   any other key: stay vanilla"

    terminal = _make_terminal()
    try:
        key = show_page(title, lines, theme, terminal, hint=hint)
    finally:
        terminal.close()
    return key in ("g", "G")


def _vanilla_choice_run(ctx: PreflightContext) -> StepResult:
    """One-time vanilla-vs-guardrails choice for the default profile.

    Acts only when the selected/effective profile is the default (explicit
    ``"default"`` or the no-profile fallback). Reads the per-project opt-in flag:

    - unset + interactive -> render the one-time choice page and persist the
      answer; if the user opts in, inject cw's guardrail hooks;
    - unset + non-interactive -> proceed vanilla WITHOUT prompting or persisting
      (the offer stays open for the next interactive launch);
    - already opted in -> (idempotently) ensure the guardrail hooks are present;
    - already opted out -> do nothing.

    Never ABORTs -- this is a setup choice, not a gate.
    """
    from .appdata import StateFile
    from .project_hooks import target_directory
    from .state import get_vanilla_guardrails_opt_in, set_vanilla_guardrails_opt_in

    profile = ctx.selections.get("profile")
    is_default = (not profile) or profile == "default"
    if not is_default:
        return StepResult.cont()

    directory = target_directory(ctx.selections)
    sf = StateFile(ctx.workspace.state_file)
    opt_in = get_vanilla_guardrails_opt_in(sf, directory)

    if opt_in is None:
        if not ctx.interactive:
            # Non-interactive: stay vanilla, do not prompt or persist.
            return StepResult.cont()
        opt_in = _prompt_vanilla_choice(ctx)
        set_vanilla_guardrails_opt_in(sf, directory, opt_in)

    if opt_in:
        ensure_vanilla_guardrails(ctx.workspace)
    return StepResult.cont()


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


def _prompt_scratchpad_cleanup(ctx: PreflightContext, stale, now_ts: float) -> bool:
    """Render the scratchpad-cleanup page and return True iff the user confirms.

    Builds a themed Terminal the same way :func:`_prompt_hook_approval` does,
    lists each stale directory (name, human-readable size, age in whole days),
    and offers a single delete-all key. Confirm is the ``y`` key; anything else
    -- ``n``, ``q``, ESC, or an interrupt -- declines. The terminal is closed on
    the way out.
    """
    from .config import resolve_theme_name
    from .profile_info import _format_size
    from .theme import parse_theme
    from .ui import show_page

    theme_name = resolve_theme_name(ctx.cfg.config.get("theme", "auto"))
    theme = parse_theme(ctx.cfg.load_theme(theme_name))

    title = "Stale Claude Code scratchpad data under /tmp"
    lines = ["These per-project scratchpad directories look stale:", ""]
    for d in stale:
        age = int(d.age_days(now_ts))
        lines.append(f"  {d.name}   {_format_size(d.size_bytes)}   {age}d old")
    lines.append("")
    lines.append("Deleting them frees /tmp space. Active sessions are never listed.")
    hint = "y: delete all listed   n/esc: skip (ask again in 7 days)"

    terminal = _make_terminal()
    try:
        key = show_page(title, lines, theme, terminal, hint=hint)
    finally:
        terminal.close()
    return key in ("y", "Y")


def _scratchpad_cleanup_run(ctx: PreflightContext) -> StepResult:
    """Offer to delete stale Claude Code scratchpad dirs under /tmp (confirmed).

    Interactive-only (skipped by the runner on the non-interactive path). Honors
    a snooze: if the stored ``scratchpad_snooze_until`` deadline is in the future,
    CONTINUE silently WITHOUT scanning (no filesystem work at all). Otherwise the
    scratchpad tree is scanned; when no directory is stale, CONTINUE silently.
    When stale dirs exist, render a confirmation page:

    - confirm -> ``shutil.rmtree`` each stale dir. Per-dir errors are collected
      and reported to stderr but NEVER abort the launch; deletion continues for
      the remaining dirs. CONTINUE.
    - decline -> set the snooze to now + :data:`SCRATCHPAD_SNOOZE_DAYS` days and
      CONTINUE.

    This step never ABORTs -- scratchpad cleanup is best-effort housekeeping.
    """
    from datetime import datetime, timedelta, timezone

    from .appdata import StateFile
    from .scratchpad import (
        SCRATCHPAD_SNOOZE_DAYS,
        scan_scratchpad_dirs,
        tmp_claude_dir,
    )
    from .state import get_scratchpad_snooze_until, set_scratchpad_snooze_until

    sf = StateFile(ctx.workspace.state_file)
    now = datetime.now(timezone.utc)

    snooze = get_scratchpad_snooze_until(sf)
    if snooze:
        try:
            until = datetime.fromisoformat(snooze)
        except (ValueError, TypeError):
            until = None
        if until is not None:
            # A naive stored value (no tzinfo) would raise TypeError when compared
            # against the offset-aware `now`. Assume UTC -- this preserves a
            # legitimate snooze intent rather than discarding it.
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if now < until:
                # Within the snooze window: no prompt, no scan side-effects.
                return StepResult.cont()

    now_ts = now.timestamp()
    stale = [d for d in scan_scratchpad_dirs(tmp_claude_dir()) if d.is_stale(now_ts)]
    if not stale:
        return StepResult.cont()

    if _prompt_scratchpad_cleanup(ctx, stale, now_ts):
        errors: list[str] = []
        for d in stale:
            try:
                shutil.rmtree(d.path)
            except OSError as e:
                errors.append(f"{d.name}: {e}")
        if errors:
            print(
                "Warning: could not delete some scratchpad dirs: "
                + "; ".join(errors),
                file=sys.stderr,
            )
        return StepResult.cont()

    until = now + timedelta(days=SCRATCHPAD_SNOOZE_DAYS)
    set_scratchpad_snooze_until(sf, until.isoformat())
    return StepResult.cont()


# Registered steps, executed in this exact (registration) order. The runner
# defaults to this list.
PREFLIGHT_STEPS: list[PreflightStep] = [
    PreflightStep(
        name="vanilla-choice",
        # Runs on the non-interactive path too so it can apply an existing
        # opt-in (ensure guardrails) without prompting; an UNSET flag stays
        # vanilla on that path.
        runs_in_non_interactive=True,
        renders_ui=True,
        run=_vanilla_choice_run,
    ),
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
    PreflightStep(
        name="scratchpad-cleanup",
        runs_in_non_interactive=False,
        renders_ui=True,
        run=_scratchpad_cleanup_run,
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
