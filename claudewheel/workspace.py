"""Workspace: the single root object owning all claudewheel filesystem paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .tokens import TokenStore


@dataclass(frozen=True)
class Workspace:
    """Immutable owner of every claudewheel filesystem path, rooted at *root*.

    Path properties are derived purely from *root*; store accessors hand back
    path-injected facades. Construction performs zero filesystem or terminal
    I/O -- it is pure value assembly. The only env var read in the entire
    package (CLAUDEWHEEL_CONFIG_DIR) lives in :meth:`default`.
    """

    root: Path
    claude_dir: Path

    @classmethod
    def open(cls, root: Path, claude_dir: Path | None = None) -> Workspace:
        """Build a Workspace at *root*. Pure value construction -- no I/O.

        *claude_dir* defaults to ``Path.home()/".claude"`` evaluated at call
        time (never at import), so a poisoned/sandboxed home is honored.
        """
        if claude_dir is None:
            claude_dir = Path.home() / ".claude"
        return cls(root=root, claude_dir=claude_dir)

    @classmethod
    def default(cls) -> Workspace:
        """Build the default Workspace, honoring CLAUDEWHEEL_CONFIG_DIR.

        This is the ONLY place in the package that reads the
        CLAUDEWHEEL_CONFIG_DIR env var. When set, its expanduser'd value is the
        root; otherwise the root is ``Path.home()/".claudewheel"``.
        """
        override = os.environ.get("CLAUDEWHEEL_CONFIG_DIR")
        if override:
            root = Path(override).expanduser()
        else:
            root = Path.home() / ".claudewheel"
        return cls.open(root)

    # --- Path properties (all derived from root) -------------------------

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"

    @property
    def tokens_file(self) -> Path:
        return self.root / "tokens.json"

    @property
    def options_file(self) -> Path:
        return self.root / "options.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def segments_file(self) -> Path:
        return self.root / "segments.json"

    @property
    def themes_dir(self) -> Path:
        return self.root / "themes"

    @property
    def hooks_dir(self) -> Path:
        return self.root / "hooks"

    @property
    def scripts_dir(self) -> Path:
        return self.root / "scripts"

    @property
    def shared_dir(self) -> Path:
        return self.root / "shared"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def shared_settings_file(self) -> Path:
        return self.root / "shared-settings.json"

    @property
    def inodes_file(self) -> Path:
        return self.shared_dir / "inodes.json"

    # --- Store accessors -------------------------------------------------

    @property
    def tokens(self) -> TokenStore:
        """The path-injected TokenStore over this workspace's tokens.json."""
        return TokenStore(self.tokens_file)

    # NOTE: profiles / shared-store / appconfig accessors are added by a later
    # phase; they will live here, each returning a path-injected facade built
    # from the properties above.
