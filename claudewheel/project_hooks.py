"""Read and fingerprint a target project's Claude Code hooks.

A project can contribute its own Claude Code hooks via ``.claude/settings.json``
and ``.claude/settings.local.json`` (each file's top-level ``hooks`` section).
Those hooks run arbitrary commands, so claudewheel must show them for explicit
approval before a launch trusts them -- and re-prompt whenever they change.

This module is the reader half: it loads both config files, extracts their
hooks sections, computes a canonical fingerprint over the combined content (so
a launch can detect first-sighting and change), and produces a flattened,
human-readable listing for the approval page. Malformed JSON is a hard error
carrying the offending filename (it aborts the launch -- never a silent skip).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The two per-project Claude Code settings files that may carry a hooks section,
# in the order they are read and combined.
_PROJECT_SETTINGS_FILES = ("settings.json", "settings.local.json")


class MalformedProjectHooksError(Exception):
    """A project settings file could not be parsed as JSON.

    Carries the bare :attr:`filename` (e.g. ``settings.local.json``) so callers
    can name the broken file in an actionable abort message.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(f"Malformed project hooks config: {filename}")


@dataclass(frozen=True)
class ProjectHooks:
    """The combined hooks a project contributes, keyed by source filename.

    ``sources`` maps each contributing file's basename (e.g. ``settings.json``)
    to that file's ``hooks`` section. Files that are absent, that lack a
    ``hooks`` key, or whose ``hooks`` is empty do NOT appear -- so ``has_hooks``
    is a clean "does this project contribute anything" signal.
    """

    sources: dict[str, Any]

    @property
    def has_hooks(self) -> bool:
        """True when at least one settings file contributes a hooks section."""
        return bool(self.sources)

    @property
    def fingerprint(self) -> str:
        """Stable sha256 hex over the canonical JSON of the combined hooks.

        Canonical serialization (sorted keys, compact separators) makes the
        fingerprint independent of key ordering and whitespace: identical hooks
        content always yields the same fingerprint, and any change -- including
        moving a hook between the two settings files -- yields a different one.
        """
        canonical = json.dumps(self.sources, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def listing_lines(self) -> list[str]:
        """Flatten the hooks into human-readable lines for the approval page.

        One line per command: the event name, the matcher (when present), and
        the command string. The Claude Code hooks schema is tolerated loosely --
        missing or extra fields never raise; only what exists is shown. An entry
        with a matcher but no commands still contributes a line so the reviewer
        sees every matcher. Ordering is deterministic (sorted by filename, then
        event) so the listing is stable across launches.
        """
        lines: list[str] = []
        for filename in sorted(self.sources):
            hooks = self.sources[filename]
            if not isinstance(hooks, dict):
                continue
            for event in sorted(hooks, key=str):
                entries = hooks[event]
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    lines.extend(_entry_lines(event, entry))
        return lines


def _entry_lines(event: str, entry: Any) -> list[str]:
    """Render one hooks entry (``{matcher?, hooks: [...]}``) to display lines."""
    matcher: Any = None
    inner: Any = []
    if isinstance(entry, dict):
        matcher = entry.get("matcher")
        inner = entry.get("hooks", [])
    prefix = f"{event}"
    if matcher is not None and matcher != "":
        prefix += f"  [matcher: {matcher}]"

    commands: list[str] = []
    if isinstance(inner, list):
        for h in inner:
            if isinstance(h, dict) and "command" in h:
                commands.append(str(h["command"]))

    if not commands:
        return [prefix]
    return [f"{prefix}  ->  {cmd}" for cmd in commands]


def _load_hooks_section(path: Path) -> Any:
    """Return the ``hooks`` section of *path*, or None if absent/empty.

    An absent file or a file without a (non-empty) ``hooks`` key contributes
    nothing. Unparseable JSON raises :class:`MalformedProjectHooksError` naming
    the file -- this is a hard error, never a silent skip.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise MalformedProjectHooksError(path.name) from None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks")
    if not hooks:
        return None
    return hooks


def read_project_hooks(directory: str) -> ProjectHooks:
    """Read the combined Claude Code hooks a project under *directory* declares.

    Reads ``<directory>/.claude/settings.json`` and ``settings.local.json``,
    extracting each file's ``hooks`` section. Absent files and absent/empty
    hooks keys are simply skipped. Malformed JSON in either file raises
    :class:`MalformedProjectHooksError` carrying the offending filename.
    """
    claude_dir = Path(directory).expanduser() / ".claude"
    sources: dict[str, Any] = {}
    for name in _PROJECT_SETTINGS_FILES:
        hooks = _load_hooks_section(claude_dir / name)
        if hooks is not None:
            sources[name] = hooks
    return ProjectHooks(sources=sources)


def target_directory(selections: dict[str, str | None]) -> str:
    """Resolve the launch target directory from *selections* (mirrors launch).

    Uses the ``directory`` selection (``~`` expanded) when set, else the current
    working directory -- the same rule the launch config resolver applies.
    """
    directory = selections.get("directory")
    if directory:
        return str(Path(directory).expanduser())
    return os.getcwd()
