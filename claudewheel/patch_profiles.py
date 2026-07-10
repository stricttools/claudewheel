"""Sync existing profiles and shared-settings.json toward canonical defaults.

Canonical defaults advance in defaults.py (new disallowedTools entries, new
hook scripts), but per-profile settings.json files are only written at
creation and shared-settings.json only when it is first created. This module
backs the ``claudewheel patch-profiles`` command, which additively brings
every discovered profile and shared-settings.json up to the current canonical
hook wiring and disallowedTools list without disturbing anything else.

The sync is purely additive and idempotent:
  - Canonical hook entries (matched by their "matcher" field) are merged in,
    de-duplicated by script basename, preserving any user-added hooks.
  - claudewheel.disallowedTools (top-level for shared-settings) is made a
    superset of defaults.DISALLOWED_TOOLS; user-added extras are kept.
  - The inert top-level ``disallowedTools`` key that Claude Code ignores is
    folded into the claudewheel namespace and removed.
  - Missing built-in hook scripts referenced by the canonical hooks are
    deployed via the shared deploy-hooks code path.

Permissions, credentials, tokens, and all unrelated keys are never touched.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .constants import PROFILES_DIR, SCRIPTS_DIR, SHARED_SETTINGS_FILE, TOKENS_FILE
from .defaults import DISALLOWED_TOOLS, build_canonical_shared_settings
from .fsutil import write_json_atomic
from .hook_scripts import HOOK_SCRIPTS, deploy_scripts
from .profile_store import Profile, ProfileStore
from .tokens import TokenStore, TokenStoreError


def _discovered_profiles() -> list[Profile]:
    """Enumerate profiles via ProfileStore, tolerating a corrupt tokens.json.

    Built at call time from this module's path constants (patched by tests) plus
    Claude Code's built-in ``~/.claude``. A corrupt tokens.json is swallowed to
    ``{}`` here (patch-profiles is additive maintenance, not token resolution),
    matching the historical discovery tolerance.
    """
    store = ProfileStore(PROFILES_DIR, Path.home() / ".claude", TokenStore(TOKENS_FILE))
    try:
        tokens = store.token_store.load()
    except TokenStoreError:
        tokens = {}
    return store.enumerate(tokens)


def _script_basename(command: str) -> str:
    """Return the trailing script name of a hook command path (or "")."""
    return Path(command).name if command else ""


def merge_hooks(existing: dict, canonical: dict) -> list[str]:
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
                (e for e in existing_entries
                 if isinstance(e, dict) and e.get("matcher", "") == matcher),
                None,
            )
            if target is None:
                existing_entries.append(deepcopy(c_entry))
                for h in c_hooks:
                    added.append(f"{event}[{label}] {_script_basename(h.get('command', ''))}")
                continue
            target_hooks = target.setdefault("hooks", [])
            for h in c_hooks:
                base = _script_basename(h.get("command", ""))
                if not base:
                    continue
                canonical_cmd = h.get("command", "")
                matches = [
                    th for th in target_hooks
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


def _append_missing(current: list, wanted: list) -> list[str]:
    """Append entries of *wanted* absent from *current* (mutated). Returns them."""
    added: list[str] = []
    have = set(current)
    for tool in wanted:
        if tool not in have:
            current.append(tool)
            have.add(tool)
            added.append(tool)
    return added


def sync_profile_settings(settings: dict, canonical: dict) -> list[str]:
    """Additively sync one profile's settings dict toward canonical (mutated).

    Returns descriptions of every change. Empty list means already in sync.
    """
    changes: list[str] = []

    hooks = settings.setdefault("hooks", {})
    for desc in merge_hooks(hooks, canonical.get("hooks", {})):
        changes.append(f"hook {desc}")

    cw = settings.setdefault("claudewheel", {})
    current = cw.get("disallowedTools")
    if not isinstance(current, list):
        current = []
        cw["disallowedTools"] = current

    # Fold the inert top-level disallowedTools key (which Claude Code ignores)
    # into the claudewheel namespace, then drop it.
    inert = settings.get("disallowedTools")
    if isinstance(inert, list):
        for tool in _append_missing(current, inert):
            changes.append(f"disallowedTools +{tool} (from inert top-level key)")
    for tool in _append_missing(current, DISALLOWED_TOOLS):
        changes.append(f"disallowedTools +{tool}")
    if "disallowedTools" in settings:
        del settings["disallowedTools"]
        changes.append("removed inert top-level disallowedTools key")

    return changes


def sync_shared_settings(shared: dict, canonical: dict) -> list[str]:
    """Additively sync shared-settings.json dict toward canonical (mutated).

    Returns descriptions of every change. Empty list means already in sync.
    In shared-settings.json, disallowedTools canonically lives at the top
    level (unlike profiles, where it is under the claudewheel namespace).
    """
    changes: list[str] = []

    hooks = shared.setdefault("hooks", {})
    for desc in merge_hooks(hooks, canonical.get("hooks", {})):
        changes.append(f"hook {desc}")

    current = shared.get("disallowedTools")
    if not isinstance(current, list):
        current = []
        shared["disallowedTools"] = current
    for tool in _append_missing(current, DISALLOWED_TOOLS):
        changes.append(f"disallowedTools +{tool}")

    return changes


def _referenced_scripts(hooks: dict) -> list[str]:
    """Collect the ordered, unique script basenames referenced by *hooks*."""
    names: list[str] = []
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            entry_hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
            for h in entry_hooks:
                base = _script_basename(h.get("command", "")) if isinstance(h, dict) else ""
                if base and base not in names:
                    names.append(base)
    return names


def run_patch_profiles(dry_run: bool = False) -> int:
    """Sync every discovered profile and shared-settings.json toward canonical.

    Deploys any missing built-in hook scripts, then additively patches
    shared-settings.json and each profile's settings.json. With *dry_run*,
    reports what would change and writes nothing.
    """
    canonical = build_canonical_shared_settings(SCRIPTS_DIR)
    changed_any = False

    # 1. Deploy any missing built-in hook scripts referenced by canonical hooks.
    referenced = _referenced_scripts(canonical.get("hooks", {}))
    missing_scripts = [
        n for n in referenced
        if n in HOOK_SCRIPTS and not (SCRIPTS_DIR / n).exists()
    ]
    if missing_scripts:
        changed_any = True
        if dry_run:
            for n in missing_scripts:
                print(f"hook script: would deploy {SCRIPTS_DIR / n}")
        else:
            for n, action in deploy_scripts(missing_scripts, SCRIPTS_DIR):
                print(f"hook script: {action} {SCRIPTS_DIR / n}")
    else:
        print("hook scripts: all present")

    # 2. shared-settings.json
    shared = None
    if SHARED_SETTINGS_FILE.exists():
        try:
            shared = json.loads(SHARED_SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"shared-settings.json: unreadable ({e}), skipping")
    else:
        print("shared-settings.json: not found, skipping")
    if shared is not None:
        sh_changes = sync_shared_settings(shared, canonical)
        if sh_changes:
            changed_any = True
            print(f"shared-settings.json: {'would update' if dry_run else 'updated'}")
            for c in sh_changes:
                print(f"    + {c}")
            if not dry_run:
                write_json_atomic(SHARED_SETTINGS_FILE, shared)
        else:
            print("shared-settings.json: already up to date")

    # 3. Each discovered profile's settings.json
    profiles = _discovered_profiles()
    if not profiles:
        print("no profiles found")
    for info in profiles:
        settings_file = info.path / "settings.json"
        if not settings_file.exists():
            print(f"{info.name}: no settings.json, skipping")
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"{info.name}: unreadable settings.json ({e}), skipping")
            continue
        changes = sync_profile_settings(settings, canonical)
        if changes:
            changed_any = True
            print(f"{info.name}: {'would update' if dry_run else 'updated'}")
            for c in changes:
                print(f"    + {c}")
            if not dry_run:
                write_json_atomic(settings_file, settings)
        else:
            print(f"{info.name}: already up to date")

    if not changed_any:
        print("\nEverything already up to date.")
    elif dry_run:
        print("\nDry run: no files were written.")

    return 0
