"""Wizard hook-merge helper plus a thin delegate to the unified reconcile core.

The old additive, user-extras-preserving profile sync (``sync_profile_settings``
/ ``sync_shared_settings`` / ``run_patch_profiles``) has been REPLACED by the
unified reconcile core in ``reconcile.py``, which prunes each target's guardrail
sections to EXACTLY canonical. ``run_patch_profiles`` here is now a thin wrapper
around that core -- the ``claudewheel patch-profiles`` command and
``claudewheel reconcile-permissions`` command do the same thing.

``merge_hooks`` (and its ``_script_basename`` helper) remain because the wizard
uses them to assemble a NEW profile's hooks from a canonical base plus any
clone-source hooks at creation time. That is profile *construction*, distinct
from the reconcile core's *pruning* of existing profiles.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import Workspace


def _script_basename(command: str) -> str:
    """Return the trailing script name of a hook command path (or "")."""
    return Path(command).name if command else ""


def merge_hooks(existing: dict[str, Any], canonical: dict[str, Any]) -> list[str]:
    """Merge canonical hooks into *existing* (mutated in place).

    Canonical entries are matched to existing ones by their "matcher" field.
    Individual canonical hooks are matched to existing ones by script basename:

    - a canonical hook whose basename is absent is APPENDED;
    - a canonical hook whose basename is present but whose command points at a
      DIFFERENT (stale) absolute path is REPATHED in place to the canonical
      command -- this is how a workspace relocation is healed, so a profile whose
      hook commands reference an old scripts directory is brought to the current
      one without duplicating the entry.

    Only claudewheel-managed wirings (those in *canonical*) are ever touched;
    user-custom, non-canonical hooks are matched by neither basename nor matcher
    and so are preserved exactly. Returns human-readable descriptions of every
    hook added or repathed.

    Used by the wizard to assemble a new profile's hooks. Existing profiles are
    reconciled to exact canonical by the reconcile core, not by this merge.
    """
    added: list[str] = []
    for event, canonical_entries in canonical.items():
        existing_entries = existing.setdefault(event, [])
        if not isinstance(existing_entries, list):
            continue
        for c_entry in canonical_entries:
            matcher = c_entry.get("matcher", "")
            c_hooks = c_entry.get("hooks", [])
            label = matcher or "*"
            target = next(
                (
                    e
                    for e in existing_entries
                    if isinstance(e, dict) and e.get("matcher", "") == matcher
                ),
                None,
            )
            if target is None:
                existing_entries.append(deepcopy(c_entry))
                for h in c_hooks:
                    added.append(
                        f"{event}[{label}] {_script_basename(h.get('command', ''))}"
                    )
                continue
            target_hooks = target.setdefault("hooks", [])
            for h in c_hooks:
                base = _script_basename(h.get("command", ""))
                if not base:
                    continue
                canonical_cmd = h.get("command", "")
                matches = [
                    th
                    for th in target_hooks
                    if isinstance(th, dict)
                    and _script_basename(th.get("command", "")) == base
                ]
                if not matches:
                    target_hooks.append(deepcopy(h))
                    added.append(f"{event}[{label}] {base}")
                    continue
                # Same script already wired; repath any stale absolute path so a
                # relocated workspace points back at the current scripts dir.
                for th in matches:
                    if th.get("command", "") != canonical_cmd:
                        old_cmd = th.get("command", "")
                        th["command"] = canonical_cmd
                        added.append(
                            f"{event}[{label}] {base} repath {old_cmd} -> {canonical_cmd}"
                        )
    return added


def run_patch_profiles(ws: "Workspace", dry_run: bool = False) -> int:
    """Reconcile every managed profile and shared-settings.json to exact canonical.

    Thin delegate to the unified reconcile core. This PRUNES each target's
    guardrail sections (hooks, disallowedTools, permissions deny/ask) to exactly
    canonical -- the old additive, user-extras-preserving behavior is gone.
    """
    from .reconcile import run_reconcile

    return run_reconcile(ws, dry_run=dry_run, profile=None)
