"""Unified reconcile core: make every managed target EXACTLY canonical.

This module owns the single reconciliation core for the whole guardrail
surface. It merges what used to be two separate, differently-shaped sync
paths -- ``patch_profiles``' additive hooks/disallowedTools sync and this
module's permissions reconciliation -- into one compare-then-write core that
brings each target file's guardrail sections into EXACT agreement with the
canonical model:

  - ``hooks``: the ENTIRE hooks structure is replaced with the canonical
    wiring (``defaults.build_canonical_shared_settings``). User-added hook
    entries are pruned -- extras belong in ``defaults.py``, not in per-profile
    drift.
  - ``disallowedTools``: made exactly equal to ``defaults.DISALLOWED_TOOLS``.
    In profile settings it lives under the ``claudewheel`` namespace; the inert
    top-level ``disallowedTools`` key (which Claude Code ignores) is dropped.
    In ``shared-settings.json`` it lives at the top level.
  - ``permissions.deny`` / ``permissions.ask``: made exactly equal to
    ``guardrail.canonical_deny_rules()`` / ``canonical_ask_rules()`` -- missing
    canonical entries added, non-canonical entries pruned.
  - ``permissions.allow``: only ``guardrail.ALLOW_CONFLICTS`` entries removed;
    all other allow entries are left alone and nothing is ever added to allow.

This DELIBERATELY replaces the old additive, user-extras-preserving semantics
of ``patch_profiles`` (``merge_hooks`` etc.): extras are pruned.

Hook SCRIPT deployment is part of canonical: the core deploys any missing
guardrail hook scripts to the scripts dir, because wiring that references
missing scripts is not canonical.

The ``"default"`` profile (Claude Code's built-in ``~/.claude``) is
UNCONDITIONALLY excluded: the core never reads from or writes to it, even when
profile discovery enumerates it.

All writes go through the mode-preserving atomic ``save_settings`` path, and
every target is compared before writing -- a file already canonical is left
byte-identical (no write happens).
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .guardrail import ALLOW_CONFLICTS, canonical_ask_rules, canonical_deny_rules
from . import effects
from .hook_scripts import HOOK_SCRIPTS, deploy_scripts
from .permission import add_rule, load_settings, remove_rule, save_settings

if TYPE_CHECKING:
    from .workspace import Workspace


# ---------------------------------------------------------------------------
# Permissions diff (deny/ask made exact; allow conflicts pruned).
# ---------------------------------------------------------------------------


@dataclass
class PermissionDiff:
    """The additions and removals needed to reconcile one permissions block."""

    deny_add: list[str] = field(default_factory=list)
    deny_remove: list[str] = field(default_factory=list)
    ask_add: list[str] = field(default_factory=list)
    ask_remove: list[str] = field(default_factory=list)
    allow_remove: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when no additions or removals are needed (already canonical)."""
        return not (
            self.deny_add
            or self.deny_remove
            or self.ask_add
            or self.ask_remove
            or self.allow_remove
        )

    def change_count(self) -> int:
        """Total number of individual add/remove operations in this diff."""
        return (
            len(self.deny_add)
            + len(self.deny_remove)
            + len(self.ask_add)
            + len(self.ask_remove)
            + len(self.allow_remove)
        )


def _reconcile_list(
    current: list[str], canonical: list[str]
) -> tuple[list[str], list[str]]:
    """Compute (to_add, to_remove) so *current* becomes exactly *canonical*.

    ``to_add`` preserves canonical order (missing canonical entries in the order
    they appear in the model). ``to_remove`` preserves *current* order (entries
    present now but absent from the canonical set).
    """
    canonical_set = set(canonical)
    current_set = set(current)
    to_add = [r for r in canonical if r not in current_set]
    to_remove = [r for r in current if r not in canonical_set]
    return to_add, to_remove


def compute_settings_diff(container: dict[str, Any]) -> PermissionDiff:
    """Compute the reconciliation diff for a dict holding a ``permissions`` block.

    *container* is either a profile ``settings.json`` dict or a
    ``profileDefaults`` dict -- both nest their arrays under ``permissions``.
    A missing ``permissions`` block (or missing arrays) is treated as empty.
    The ``allow`` array is only inspected when present; nothing is ever added to
    allow.
    """
    perms = container.get("permissions")
    if not isinstance(perms, dict):
        perms = {}

    deny_raw = perms.get("deny")
    deny_current: list[str] = deny_raw if isinstance(deny_raw, list) else []
    ask_raw = perms.get("ask")
    ask_current: list[str] = ask_raw if isinstance(ask_raw, list) else []
    allow_raw = perms.get("allow")
    allow_current: list[str] = allow_raw if isinstance(allow_raw, list) else []

    deny_add, deny_remove = _reconcile_list(deny_current, canonical_deny_rules())
    ask_add, ask_remove = _reconcile_list(ask_current, canonical_ask_rules())
    allow_remove = [r for r in allow_current if r in ALLOW_CONFLICTS]

    return PermissionDiff(
        deny_add=deny_add,
        deny_remove=deny_remove,
        ask_add=ask_add,
        ask_remove=ask_remove,
        allow_remove=allow_remove,
    )


def apply_settings_diff(container: dict[str, Any], diff: PermissionDiff) -> None:
    """Mutate *container* in place to enact *diff* via the permission primitives.

    Removals run before additions. Uses ``permission.add_rule`` (append-only) and
    ``permission.remove_rule`` so JSON IO and the permissions-block shape stay
    consistent with the rest of the codebase.
    """
    for rule in diff.deny_remove:
        remove_rule(container, "deny", rule)
    for rule in diff.ask_remove:
        remove_rule(container, "ask", rule)
    for rule in diff.allow_remove:
        remove_rule(container, "allow", rule)
    for rule in diff.deny_add:
        add_rule(container, "deny", rule)
    for rule in diff.ask_add:
        add_rule(container, "ask", rule)


def _reconcile_permissions(container: dict[str, Any]) -> list[str]:
    """Make *container*'s permissions deny/ask exact and prune allow conflicts.

    Returns human-readable change descriptions (empty when already canonical).
    """
    diff = compute_settings_diff(container)
    changes: list[str] = []
    for rule in diff.deny_remove:
        changes.append(f"deny -{rule}")
    for rule in diff.deny_add:
        changes.append(f"deny +{rule}")
    for rule in diff.ask_remove:
        changes.append(f"ask -{rule}")
    for rule in diff.ask_add:
        changes.append(f"ask +{rule}")
    for rule in diff.allow_remove:
        changes.append(f"allow -{rule}")
    apply_settings_diff(container, diff)
    return changes


# ---------------------------------------------------------------------------
# Hooks + disallowedTools reconciliation (made exactly canonical).
# ---------------------------------------------------------------------------


def _reconcile_hooks(container: dict[str, Any], canonical_hooks: dict[str, Any]) -> list[str]:
    """Set ``container['hooks']`` to EXACTLY *canonical_hooks*.

    Replaces the entire hooks structure -- user-added hook entries are pruned.
    No-op (no mutation, empty return) when the hooks are already canonical.
    """
    if container.get("hooks") == canonical_hooks:
        return []
    container["hooks"] = deepcopy(canonical_hooks)
    return ["hooks -> canonical"]


def _reconcile_profile_disallowed(settings: dict[str, Any]) -> list[str]:
    """Make a profile's ``claudewheel.disallowedTools`` exactly canonical.

    Also drops the inert top-level ``disallowedTools`` key (Claude Code ignores
    it -- profiles carry the list under the ``claudewheel`` namespace).
    """
    changes: list[str] = []
    cw = settings.setdefault("claudewheel", {})
    if cw.get("disallowedTools") != list(DISALLOWED_TOOLS):
        cw["disallowedTools"] = list(DISALLOWED_TOOLS)
        changes.append("disallowedTools -> canonical")
    if "disallowedTools" in settings:
        del settings["disallowedTools"]
        changes.append("removed inert top-level disallowedTools")
    return changes


def _reconcile_shared_disallowed(shared: dict[str, Any]) -> list[str]:
    """Make ``shared-settings.json``'s top-level ``disallowedTools`` exactly canonical."""
    if shared.get("disallowedTools") != list(DISALLOWED_TOOLS):
        shared["disallowedTools"] = list(DISALLOWED_TOOLS)
        return ["disallowedTools -> canonical"]
    return []


def reconcile_profile_dict(
    settings: dict[str, Any], canonical: dict[str, Any]
) -> list[str]:
    """Reconcile one profile ``settings.json`` dict IN PLACE to exact canonical.

    Reconciles hooks, the ``claudewheel.disallowedTools`` list, and
    ``permissions`` deny/ask/allow. Non-guardrail keys are left untouched.
    Returns human-readable change descriptions (empty when already canonical).
    """
    changes: list[str] = []
    changes += _reconcile_hooks(settings, canonical["hooks"])
    changes += _reconcile_profile_disallowed(settings)
    changes += _reconcile_permissions(settings)
    return changes


def reconcile_shared_dict(
    shared: dict[str, Any], canonical: dict[str, Any]
) -> list[str]:
    """Reconcile the ``shared-settings.json`` dict IN PLACE to exact canonical.

    Reconciles the top-level hooks and disallowedTools plus the
    ``profileDefaults.permissions`` deny/ask/allow. Non-guardrail keys are left
    untouched. Returns human-readable change descriptions.
    """
    changes: list[str] = []
    changes += _reconcile_hooks(shared, canonical["hooks"])
    changes += _reconcile_shared_disallowed(shared)
    pd = shared.setdefault("profileDefaults", {})
    changes += [f"profileDefaults {c}" for c in _reconcile_permissions(pd)]
    return changes


# ---------------------------------------------------------------------------
# Hook-script deployment (part of canonical).
# ---------------------------------------------------------------------------


def _referenced_scripts(hooks: dict[str, Any]) -> list[str]:
    """Collect the ordered, unique script basenames referenced by *hooks*."""
    names: list[str] = []
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            entry_hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
            for h in entry_hooks:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                base = Path(cmd).name if cmd else ""
                if base and base not in names:
                    names.append(base)
    return names


# ---------------------------------------------------------------------------
# Report structures.
# ---------------------------------------------------------------------------


@dataclass
class TargetReport:
    """The outcome of reconciling one target file."""

    label: str
    changed: bool
    written: bool
    changes: list[str] = field(default_factory=list)
    skip_reason: str = ""


@dataclass
class ReconcileReport:
    """The aggregate outcome of a workspace reconciliation pass."""

    scripts_deployed: list[str] = field(default_factory=list)
    scripts_would_deploy: list[str] = field(default_factory=list)
    targets: list[TargetReport] = field(default_factory=list)
    error: str | None = None

    def changed_any(self) -> bool:
        """True when anything was (or would be) written."""
        return bool(
            self.scripts_deployed
            or self.scripts_would_deploy
            or any(t.changed for t in self.targets)
        )


# ---------------------------------------------------------------------------
# The core: reconcile the whole workspace.
# ---------------------------------------------------------------------------


def _process_settings_file(
    path: Path,
    reconcile_fn: Callable[[dict[str, Any], dict[str, Any]], list[str]],
    canonical: dict[str, Any],
    label: str,
    dry_run: bool,
) -> TargetReport:
    """Load, reconcile, compare, and (unless dry-run) write one settings file.

    Compare-then-write: the file is written only when its guardrail sections
    actually differed from canonical, so an already-canonical file is left
    byte-identical. A missing/unreadable file is reported and skipped; a write
    error is captured (never raised) so a launch-time reconcile never aborts.
    """
    if not path.exists():
        return TargetReport(label, changed=False, written=False, skip_reason="no settings.json")
    try:
        data = load_settings(path)
    except (json.JSONDecodeError, OSError) as e:
        return TargetReport(
            label, changed=False, written=False, skip_reason=f"unreadable ({e})"
        )
    original = deepcopy(data)
    changes = reconcile_fn(data, canonical)
    changed = data != original
    report = TargetReport(label, changed=changed, written=False, changes=changes)
    if changed and effects.issue(dry_run):
        try:
            save_settings(path, data)
            report.written = not dry_run
        except OSError as e:
            report.skip_reason = f"write-error ({e})"
    return report


def reconcile_workspace(
    ws: "Workspace",
    *,
    dry_run: bool,
    profile: str | None = None,
    deploy_hook_scripts: bool = True,
) -> ReconcileReport:
    """Reconcile every managed target to exact canonical. The single core.

    Deploys any missing guardrail hook scripts (unless *dry_run*), then
    reconciles each discovered profile's ``settings.json`` and -- when not
    scoped to a single *profile* -- ``shared-settings.json``. The ``"default"``
    profile is unconditionally excluded. When *profile* names a single profile,
    only that profile is touched and shared-settings is left alone.
    """
    report = ReconcileReport()
    canonical = build_canonical_shared_settings(ws.scripts_dir)

    # 1. Deploy any missing built-in hook scripts referenced by canonical hooks.
    if deploy_hook_scripts:
        referenced = _referenced_scripts(canonical.get("hooks", {}))
        missing = [
            n
            for n in referenced
            if n in HOOK_SCRIPTS and not (ws.scripts_dir / n).exists()
        ]
        if missing:
            if not effects.issue(dry_run):
                report.scripts_would_deploy = missing
            else:
                for name, _action in deploy_scripts(missing, ws.scripts_dir):
                    if dry_run:
                        report.scripts_would_deploy.append(name)
                    else:
                        report.scripts_deployed.append(name)

    # 2. Profiles (default UNCONDITIONALLY excluded; never read/written).
    profiles = [
        p
        for p in ws.profiles.discover(on_corrupt_tokens="swallow")
        if p.name != "default"
    ]
    only_one = profile is not None
    if only_one:
        profiles = [p for p in profiles if p.name == profile]
        if not profiles:
            report.error = f"profile {profile!r} not found"
            return report

    for info in profiles:
        report.targets.append(
            _process_settings_file(
                info.path / "settings.json",
                reconcile_profile_dict,
                canonical,
                info.name,
                dry_run,
            )
        )

    # 3. shared-settings.json (fleet-wide; skipped when scoped to one profile).
    if not only_one:
        report.targets.append(
            _process_settings_file(
                ws.shared_settings_file,
                reconcile_shared_dict,
                canonical,
                "shared-settings.json",
                dry_run,
            )
        )

    return report


# ---------------------------------------------------------------------------
# Printing wrapper (shared by the reconcile-permissions and patch-profiles CLI).
# ---------------------------------------------------------------------------


def _print_report(report: ReconcileReport, dry_run: bool) -> None:
    """Print a human-readable summary of a reconciliation pass."""
    if report.scripts_would_deploy:
        for name in report.scripts_would_deploy:
            print(f"hook script: would deploy {name}")
    elif report.scripts_deployed:
        for name in report.scripts_deployed:
            print(f"hook script: deployed {name}")
    else:
        print("hook scripts: all present")

    if report.error:
        print(f"Error: {report.error}", file=sys.stderr)
        return

    for t in report.targets:
        if t.skip_reason:
            print(f"{t.label}: {t.skip_reason}")
        elif not t.changed:
            print(f"{t.label}: already canonical, no changes")
        else:
            verb = "would reconcile" if dry_run else "reconciled"
            print(f"{t.label}: {verb}")
            for c in t.changes:
                print(f"    {c}")

    if not report.changed_any():
        print("\nEverything already canonical.")
    elif dry_run:
        print("\nDry run: no files were written.")
    else:
        print("\nReconciled to canonical.")


def run_reconcile(ws: "Workspace", dry_run: bool, profile: str | None = None) -> int:
    """Reconcile the workspace to exact canonical and print a report.

    Backs both the ``reconcile-permissions`` and ``patch-profiles`` CLI commands
    (they are now the same operation). Returns 0 on success, 1 when a scoped
    *profile* is not found.
    """
    report = reconcile_workspace(ws, dry_run=dry_run, profile=profile)
    _print_report(report, dry_run)
    return 1 if report.error else 0
