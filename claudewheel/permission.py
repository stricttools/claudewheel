"""Core logic for managing profile permission rules."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .fsutil import write_json_atomic

if TYPE_CHECKING:
    from .workspace import Workspace

_VALID_CATEGORIES = ("allow", "deny", "ask")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def validate_rule(rule: str) -> None:
    """Raise ValueError if *rule* is not a valid permission rule string.

    Rules are either bare tool names (``Bash``) or tool-with-pattern
    (``Bash(git diff:*)``).  Empty, whitespace-only, and malformed
    strings are rejected.
    """
    if not rule or not rule.strip():
        raise ValueError("rule must not be empty or whitespace-only")

    if "(" in rule or ")" in rule:
        # Must contain '(' and end with ')'
        if "(" not in rule:
            raise ValueError("rule contains ')' but no '('")
        if not rule.endswith(")"):
            raise ValueError("rule contains '(' but does not end with ')'")
        idx = rule.index("(")
        tool_name = rule[:idx]
        if not tool_name:
            raise ValueError("tool name before '(' must not be empty")
        if not _TOOL_NAME_RE.match(tool_name):
            raise ValueError(
                f"tool name {tool_name!r} must match [A-Za-z][A-Za-z0-9_-]*"
            )
        inner = rule[idx + 1 : -1]
        if not inner:
            raise ValueError("content inside parentheses must not be empty")
    else:
        if not _TOOL_NAME_RE.match(rule):
            raise ValueError(
                f"rule {rule!r} must match [A-Za-z][A-Za-z0-9_-]*"
            )


def load_settings(settings_path: Path) -> dict[str, Any]:
    """Read and parse a profile's settings.json.

    Raises FileNotFoundError if the file does not exist and
    json.JSONDecodeError if the content is not valid JSON.
    """
    data: dict[str, Any] = json.loads(settings_path.read_text())
    return data


def save_settings(settings_path: Path, data: dict[str, Any]) -> None:
    """Atomic-write *data* as JSON to *settings_path*.

    Writes to a temporary sibling file first, then renames over the
    original to avoid partial writes, preserving the file's mode.
    """
    write_json_atomic(settings_path, data)


def add_rule(data: dict[str, Any], category: str, rule: str) -> str:
    """Append *rule* to ``data["permissions"][category]``.

    Returns ``"added"`` on success or ``"already present"`` if the rule
    already exists in the list.  The list is never sorted -- append only.
    """
    if category not in _VALID_CATEGORIES:
        raise ValueError(
            f"category must be one of {', '.join(_VALID_CATEGORIES)}, got {category!r}"
        )
    perms = data.setdefault("permissions", {})
    rules_list = perms.setdefault(category, [])
    if rule in rules_list:
        return "already present"
    rules_list.append(rule)
    return "added"


def remove_rule(data: dict[str, Any], category: str, rule: str) -> str:
    """Remove *rule* from ``data["permissions"][category]``.

    Returns ``"removed"`` on success or ``"not found"`` if the rule
    is not in the list.
    """
    if category not in _VALID_CATEGORIES:
        raise ValueError(
            f"category must be one of {', '.join(_VALID_CATEGORIES)}, got {category!r}"
        )
    perms = data.get("permissions", {})
    rules_list = perms.get(category, [])
    if rule not in rules_list:
        return "not found"
    rules_list.remove(rule)
    return "removed"


def resolve_profiles(
    ws: "Workspace", profile: str | None, all_profiles: bool
) -> list[tuple[str, Path]]:
    """Map the mutex flag values to a list of ``(name, settings_path)`` pairs.

    Exactly one of *profile* or *all_profiles* must be truthy (enforced
    by the caller's MutexGroup).  Prints to stderr and exits on error.
    Enumeration uses the workspace's ProfileStore, so a corrupt tokens.json
    raises ``TokenStoreError`` -- the uniform hard-error contract; permission
    commands are settings.json operations, but a corrupt tokens.json is a
    workspace-integrity problem the operator must fix.
    """
    if profile is not None:
        discovered = ws.profiles.enumerate()
        for p in discovered:
            if p.name == profile:
                return [(p.name, p.path / "settings.json")]
        print(f"Error: profile {profile!r} not found", file=sys.stderr)
        sys.exit(1)

    if all_profiles:
        discovered = ws.profiles.enumerate()
        if not discovered:
            print("Error: no profiles found", file=sys.stderr)
            sys.exit(1)
        return [(p.name, p.path / "settings.json") for p in discovered]

    # Defensive: MutexGroup should prevent reaching here
    print("Error: one of --profile or --all-profiles is required", file=sys.stderr)
    sys.exit(1)
