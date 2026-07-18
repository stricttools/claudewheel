"""Reconcile profile and shared-settings permissions toward the canonical model.

Where ``patch_profiles`` additively syncs hooks and disallowedTools, this module
owns the *permissions* arrays. It brings every discovered profile's
``settings.json`` -- and ``shared-settings.json``'s ``profileDefaults`` -- into
exact agreement with the canonical guardrail model in ``guardrail.py``:

  - ``permissions.deny`` is made to contain exactly ``canonical_deny_rules()``:
    missing canonical entries are added, and any deny entry NOT in the canonical
    set is removed.
  - ``permissions.ask`` is reconciled identically against ``canonical_ask_rules()``.
  - ``permissions.allow`` has every entry listed in ``ALLOW_CONFLICTS`` removed
    (dead or conflicting allow rules); all other allow entries are left alone and
    nothing is ever added to allow.

Unlike patch-profiles, reconciliation is NOT purely additive for deny/ask: it
prunes drift. It never touches hooks, disallowedTools, or any non-permission
key. All writes go through ``permission.py``'s atomic ``save_settings``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .guardrail import ALLOW_CONFLICTS, canonical_ask_rules, canonical_deny_rules
from .permission import add_rule, load_settings, remove_rule, save_settings
from .profile_store import Profile
from .tokens import TokenStoreError

if TYPE_CHECKING:
    from .workspace import Workspace


def _discovered_profiles(ws: "Workspace") -> list[Profile]:
    """Enumerate profiles via the workspace's ProfileStore, tolerating a corrupt
    tokens.json.

    A corrupt tokens.json is swallowed to ``{}`` -- reconciliation touches
    permissions, not tokens.
    """
    store = ws.profiles
    try:
        tokens = store.token_store.load()
    except TokenStoreError:
        tokens = {}
    return store.enumerate(tokens)


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


def _print_diff(label: str, diff: PermissionDiff, dry_run: bool) -> None:
    """Print a per-target diff header and one line per add/remove operation."""
    verb = "would reconcile" if dry_run else "reconciled"
    print(f"{label}: {verb}")
    for rule in diff.deny_remove:
        print(f"    deny  -{rule}")
    for rule in diff.deny_add:
        print(f"    deny  +{rule}")
    for rule in diff.ask_remove:
        print(f"    ask   -{rule}")
    for rule in diff.ask_add:
        print(f"    ask   +{rule}")
    for rule in diff.allow_remove:
        print(f"    allow -{rule}")


def run_reconcile(ws: "Workspace", dry_run: bool, profile: str | None) -> int:
    """Reconcile permissions across profiles and shared-settings profileDefaults.

    When *profile* is ``None``, every discovered profile is reconciled AND
    ``shared-settings.json``'s ``profileDefaults`` is reconciled (so future
    profiles seed from a canonical baseline). When *profile* names a single
    profile, ONLY that profile is touched -- shared-settings is left alone, on
    the principle that scoping to one profile is a targeted operation and the
    shared baseline is a fleet-wide concern.

    With *dry_run*, prints the diff for each target and writes nothing. Returns 0
    on success, 1 if a named profile is not found.
    """
    only_one = profile is not None
    changed_any = False
    total_changes = 0
    targets_changed = 0

    # 1. Profiles.
    profiles = _discovered_profiles(ws)
    if only_one:
        profiles = [p for p in profiles if p.name == profile]
        if not profiles:
            print(f"Error: profile {profile!r} not found", file=sys.stderr)
            return 1
    elif not profiles:
        print("no profiles found")

    for info in profiles:
        settings_file = info.path / "settings.json"
        if not settings_file.exists():
            print(f"{info.name}: no settings.json, skipping")
            continue
        try:
            settings = load_settings(settings_file)
        except (json.JSONDecodeError, OSError) as e:
            print(f"{info.name}: unreadable settings.json ({e}), skipping")
            continue
        diff = compute_settings_diff(settings)
        if diff.is_empty():
            print(f"{info.name}: already canonical, no changes")
            continue
        changed_any = True
        targets_changed += 1
        total_changes += diff.change_count()
        _print_diff(info.name, diff, dry_run)
        if not dry_run:
            apply_settings_diff(settings, diff)
            save_settings(settings_file, settings)

    # 2. shared-settings.json profileDefaults (fleet-wide; skipped when scoped).
    if not only_one:
        shared_settings_file = ws.shared_settings_file
        if not shared_settings_file.exists():
            print("shared-settings.json: not found, skipping")
        else:
            shared: dict[str, Any] | None = None
            try:
                shared = load_settings(shared_settings_file)
            except (json.JSONDecodeError, OSError) as e:
                print(f"shared-settings.json: unreadable ({e}), skipping")
            if shared is not None:
                pd = shared.get("profileDefaults")
                if not isinstance(pd, dict) or not isinstance(
                    pd.get("permissions"), dict
                ):
                    print(
                        "shared-settings.json: no profileDefaults.permissions, skipping"
                    )
                else:
                    diff = compute_settings_diff(pd)
                    label = "shared-settings.json profileDefaults"
                    if diff.is_empty():
                        print(f"{label}: already canonical, no changes")
                    else:
                        changed_any = True
                        targets_changed += 1
                        total_changes += diff.change_count()
                        _print_diff(label, diff, dry_run)
                        if not dry_run:
                            apply_settings_diff(pd, diff)
                            save_settings(shared_settings_file, shared)

    # 3. Summary.
    if not changed_any:
        print("\nEverything already canonical.")
    else:
        noun = "target" if targets_changed == 1 else "targets"
        change_noun = "change" if total_changes == 1 else "changes"
        if dry_run:
            print(
                f"\nDry run: {total_changes} {change_noun} across "
                f"{targets_changed} {noun}; no files were written."
            )
        else:
            print(
                f"\nReconciled {total_changes} {change_noun} across "
                f"{targets_changed} {noun}."
            )

    return 0
