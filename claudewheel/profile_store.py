"""Path-injected profile enumeration and env resolution beside discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tokens import TokenStore, TokenStoreError

__all__ = ["Profile", "ProfileStore", "TokenStoreError"]


@dataclass(frozen=True)
class Profile:
    """A single discovered profile: name, on-disk path, and credential/token presence."""

    name: str
    path: Path
    has_credentials: bool
    has_token: bool

    @property
    def config_dir(self) -> Path:
        """Alias for :attr:`path` -- the CLAUDE_CONFIG_DIR of this profile."""
        return self.path


@dataclass(frozen=True)
class ProfileStore:
    """Path-injected facade that enumerates profiles and resolves launch env.

    All paths are explicit -- the store never reads module path constants and
    never calls ``Path.home()``. ``profiles_dir`` is the claudewheel profiles
    directory; ``claude_dir`` is Claude Code's built-in ``~/.claude`` (the
    "default" profile); ``token_store`` supplies token data. Every method is
    read-only: zero filesystem writes, zero terminal I/O.
    """

    profiles_dir: Path
    claude_dir: Path
    token_store: TokenStore

    def path_for(self, name: str) -> Path:
        """Map a profile name to its config dir. The single home of this convention.

        ``"default"`` maps to :attr:`claude_dir`; every other name maps to
        ``profiles_dir / name``.
        """
        if name == "default":
            return self.claude_dir
        return self.profiles_dir / name

    def enumerate(self, tokens: dict | None = None) -> list[Profile]:
        """Discover all profiles, encoding the historical discovery rules verbatim.

        *tokens* ``None`` loads token data via ``token_store.load()`` (a corrupt
        tokens.json raises :class:`TokenStoreError` -- the hard-error contract).
        An explicit dict (e.g. ``{}``) is the explicit token view for callers
        that must proceed without token data.

        Rules mirrored from ``discovery.discover_profiles``:
        1. ``claude_dir`` qualifies as "default" when it is a dir AND holds
           ``.credentials.json`` (has_credentials=True).
        2. Each subdir of ``profiles_dir`` qualifies when it holds
           ``.credentials.json`` OR ``settings.json``; has_credentials tracks
           the ``.credentials.json`` presence.
        3. Token-only: each tokens key not already found whose path_for() dir
           exists qualifies with has_credentials=False.
        4. has_token is True for any profile whose name is a tokens key.
        Result is sorted by name.
        """
        if tokens is None:
            tokens = self.token_store.load()

        # (name, path, has_credentials) records; has_token derived last.
        records: list[tuple[str, Path, bool]] = []
        found_names: set[str] = set()

        # Rule 1: bare claude_dir as "default".
        if self.claude_dir.is_dir() and (self.claude_dir / ".credentials.json").exists():
            records.append(("default", self.claude_dir, True))
            found_names.add("default")

        # Rule 2: profiles_dir subdirectories.
        if self.profiles_dir.is_dir():
            for entry in sorted(self.profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                name = entry.name
                if not name:
                    continue
                has_credentials = (entry / ".credentials.json").exists()
                has_settings = (entry / "settings.json").exists()
                if has_credentials or has_settings:
                    records.append((name, entry, has_credentials))
                    found_names.add(name)

        # Rule 3: token-only entries whose dir exists.
        for key in tokens:
            if key in found_names:
                continue
            pdir = self.path_for(key)
            if pdir.is_dir():
                records.append((key, pdir, False))
                found_names.add(key)

        # Rule 4: mark token presence (equivalent to discovery's two-pass form --
        # every token-only record's name is already a tokens key).
        profiles = [
            Profile(name, path, has_credentials, name in tokens)
            for name, path, has_credentials in records
        ]
        profiles.sort(key=lambda p: p.name)
        return profiles

    def get(self, name: str, tokens: dict | None = None) -> Profile | None:
        """Return the enumerated :class:`Profile` for *name*, or None if absent."""
        for profile in self.enumerate(tokens):
            if profile.name == name:
                return profile
        return None

    def env(self, name: str) -> dict[str, str]:
        """Resolve a profile name to launch env vars. Read-only, no terminal I/O.

        Enumerates via the TokenStore (a corrupt tokens.json raises
        :class:`TokenStoreError`). An unknown *name* raises :class:`ValueError`
        listing the available profile names. The result always carries
        ``CLAUDE_CONFIG_DIR`` and adds ``CLAUDE_CODE_OAUTH_TOKEN`` when the
        token_store yields a truthy token for *name*.
        """
        profiles = self.enumerate()
        names = {p.name for p in profiles}
        if name not in names:
            available = sorted(names)
            raise ValueError(
                f"Profile {name!r} not found. Available profiles: {available}"
            )

        env: dict[str, str] = {"CLAUDE_CONFIG_DIR": str(self.path_for(name))}
        token = self.token_store.token_for(name)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return env
