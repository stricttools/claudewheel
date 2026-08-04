"""The profile store: enumerate, resolve, create, delete, and rename profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .appdata import OptionsFile, StateFile
from . import effects
from .effects import write_json_atomic
from .shared_store import SharedStore
from .tokens import TokenStore, TokenStoreError

__all__ = [
    "Profile",
    "ProfileStore",
    "DeletionResult",
    "AuditFinding",
]

# Segment key under which profiles are registered in options.json.
_PROFILE_SEGMENT = "profile"

# Minimal options.json fallback used by every write op. Only consulted when the
# real file is missing/corrupt (the normal case reads the on-disk file). Shape
# mirrors DEFAULT_OPTIONS["profile"] minus the discovery block, which write ops
# never touch -- an empty values/pinned segment is enough for add_pinned to
# register a value and for rename/remove to no-op cleanly on a fresh store.
_OPTIONS_DEFAULT: dict[str, Any] = {_PROFILE_SEGMENT: {"values": [], "pinned": []}}

# Breadcrumb file written into a profile dir mid-rename. Same name as
# profile_ops.RENAME_PENDING_FILE so both engines recognize the same crumbs.
_RENAME_PENDING_FILE = ".rename_pending"


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
class AuditFinding:
    """One structured integrity finding from :meth:`ProfileStore.audit`.

    - ``kind``: a stable finding category. Currently the only kind is
      ``"orphan-token-entry"`` -- a tokens.json key with no directory on disk.
    - ``name``: the profile name the finding is about.
    - ``detail``: a human-readable explanation.
    """

    kind: str
    name: str
    detail: str


@dataclass(frozen=True)
class DeletionResult:
    """Success record from :meth:`ProfileStore.delete` (refusals raise instead).

    Mirrors the success-path fields of ``profile_ops.DeleteResult``: symlink and
    real-entry removal counts plus which stores were touched.
    """

    removed_symlinks: int
    removed_real: int
    removed_from_options: bool
    removed_from_tokens: bool
    last_config_purged: bool


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
    # Write-path stores. None keeps the read APIs working with zero write deps;
    # every write op guards on their presence (explicit config, not silent skip).
    shared: SharedStore | None = None
    options: OptionsFile | None = None
    state: StateFile | None = None

    def path_for(self, name: str) -> Path:
        """Map a profile name to its config dir. The single home of this convention.

        ``"default"`` maps to :attr:`claude_dir`; every other name maps to
        ``profiles_dir / name``.
        """
        if name == "default":
            return self.claude_dir
        return self.profiles_dir / name

    def enumerate(self, tokens: dict[str, Any] | None = None) -> list[Profile]:
        """Discover all profiles, encoding the historical discovery rules verbatim.

        *tokens* ``None`` loads token data via ``token_store.load()`` (a corrupt
        tokens.json raises :class:`TokenStoreError` -- the hard-error contract).
        An explicit dict (e.g. ``{}``) is the explicit token view for callers
        that must proceed without token data.

        Rules encoding the profile-discovery behavior:
        1. ``claude_dir`` qualifies as "default" whenever it IS A DIRECTORY.
           ``~/.claude`` is Claude Code's own config dir -- managed by Claude
           Code, not cw -- so cw cannot verify its auth (``.credentials.json``
           may live elsewhere, e.g. macOS Keychain). ``has_credentials`` tracks
           the ``.credentials.json`` presence but is NOT required for discovery.
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

        # Rule 1: claude_dir as "default" whenever it is a directory (lenient --
        # ~/.claude is managed by Claude Code, so cw cannot require credentials
        # it may not be able to see). has_credentials reflects on-disk reality.
        if self.claude_dir.is_dir():
            has_default_credentials = (self.claude_dir / ".credentials.json").exists()
            records.append(("default", self.claude_dir, has_default_credentials))
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

    def discover(
        self,
        *,
        on_corrupt_tokens: Literal["raise", "swallow"],
        tokens: dict[str, Any] | None = None,
    ) -> list[Profile]:
        """Enumerate profiles with an EXPLICIT corrupt-tokens policy.

        The single shared home of the "enumerate profiles, deciding what to do
        about a corrupt tokens.json" convention. Every consumer (health,
        reconcile, patch-profiles) routes through here so the swallow
        ``try/except`` lives in exactly one place.

        *on_corrupt_tokens* is mandatory and has no default -- the caller must
        choose:

        - ``"raise"``: a corrupt tokens.json raises :class:`TokenStoreError`
          (the hard-error contract; health records the error once elsewhere).
        - ``"swallow"``: a corrupt tokens.json is swallowed to ``{}`` (additive
          maintenance that touches permissions/hooks, not tokens).

        *tokens* is an explicit preloaded token view. When provided it is used
        verbatim and never re-loaded, so *on_corrupt_tokens* is moot -- this is
        how health passes the single view it loaded once. When ``None``, this
        loads via ``token_store.load()`` and applies *on_corrupt_tokens*.
        """
        if on_corrupt_tokens not in ("raise", "swallow"):
            raise ValueError(
                "on_corrupt_tokens must be 'raise' or 'swallow', got "
                f"{on_corrupt_tokens!r}"
            )
        if tokens is None:
            try:
                tokens = self.token_store.load()
            except TokenStoreError:
                if on_corrupt_tokens == "raise":
                    raise
                tokens = {}
        return self.enumerate(tokens)

    def get(self, name: str, tokens: dict[str, Any] | None = None) -> Profile | None:
        """Return the enumerated :class:`Profile` for *name*, or None if absent."""
        for profile in self.enumerate(tokens):
            if profile.name == name:
                return profile
        return None

    def audit(self, tokens: dict[str, Any] | None = None) -> list[AuditFinding]:
        """Return structured integrity findings about the profile store.

        Read-only: zero filesystem writes. *tokens* ``None`` loads token data via
        ``token_store.load()`` (a corrupt tokens.json raises
        :class:`TokenStoreError`); an explicit dict is used verbatim.

        Currently one finding kind:

        - ``"orphan-token-entry"``: a tokens.json key whose ``path_for()`` dir
          does not exist on disk (a token entry with no profile behind it).

        Findings are returned in sorted-name order for deterministic output.
        """
        if tokens is None:
            tokens = self.token_store.load()
        findings: list[AuditFinding] = []
        for key in sorted(tokens):
            pdir = self.path_for(key)
            if not pdir.is_dir():
                findings.append(
                    AuditFinding(
                        kind="orphan-token-entry",
                        name=key,
                        detail=(
                            f"tokens.json has an entry for {key!r} but no profile "
                            f"directory exists at {pdir}"
                        ),
                    )
                )
        return findings

    def env(self, name: str) -> dict[str, str]:
        """Resolve a profile name to launch env vars. Read-only, no terminal I/O.

        Enumerates via the TokenStore (a corrupt tokens.json raises
        :class:`TokenStoreError`). An unknown *name* raises :class:`ValueError`
        listing the available profile names.

        For every named profile the result carries ``CLAUDE_CONFIG_DIR`` and
        adds ``CLAUDE_CODE_OAUTH_TOKEN`` when the token_store yields a truthy
        token for *name*. The ``"default"`` profile is the EXCEPTION: it is
        Claude Code's own ``~/.claude``, managed by Claude Code and strictly
        read-only to cw, so it resolves to an EMPTY env -- no
        ``CLAUDE_CONFIG_DIR`` and no token injection (the vanilla launch path).
        """
        profiles = self.enumerate()
        names = {p.name for p in profiles}
        if name not in names:
            available = sorted(names)
            raise ValueError(
                f"Profile {name!r} not found. Available profiles: {available}"
            )

        if name == "default":
            # Vanilla default: Claude Code manages ~/.claude itself. cw injects
            # neither a config dir nor a token.
            return {}

        env: dict[str, str] = {"CLAUDE_CONFIG_DIR": str(self.path_for(name))}
        token = self.token_store.token_for(name)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        return env

    # --- Write operations ------------------------------------------------
    #
    # These build NEW code beside the live wizard/profile_ops paths. Every op
    # begins with a guard requiring the write-path stores -- a missing store is
    # a hard RuntimeError (explicit configuration, never a silent skip).

    def _require_write_stores(self) -> None:
        """Guard: every write op needs shared/options/state wired."""
        if self.shared is None or self.options is None or self.state is None:
            raise RuntimeError(
                "ProfileStore write operations require shared/options/state stores"
            )

    def _require_shared(self) -> None:
        """Guard for shared-store-only helpers (classify_shared_dirs)."""
        if self.shared is None:
            raise RuntimeError(
                "ProfileStore.classify_shared_dirs requires the shared store"
            )

    def _set_onboarding_flag(self, config_dir: Path) -> None:
        """Merge ``hasCompletedOnboarding: true`` into ``<config_dir>/.claude.json``.

        Replicates ``wizard._set_onboarding_flag`` exactly: no-op if the dir is
        absent, read-merge-write preserving other keys, tolerating a corrupt or
        missing file, atomic write.
        """
        if not config_dir.is_dir():
            return
        path = config_dir / ".claude.json"
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        data["hasCompletedOnboarding"] = True
        write_json_atomic(path, data)

    def create(
        self,
        name: str,
        settings: dict[str, Any],
        *,
        set_onboarding: bool = True,
        symlink_shared: bool = True,
    ) -> Profile:
        """Create a profile from FINAL *settings* content. Returns the Profile.

        Settings assembly (clone/defaults/checkbox overrides/hook merging) stays
        in the wizard -- the store takes the finished dict and lands it durably:
        atomic settings.json write, onboarding flag, all six shared-store
        symlinks plus skills, and options.json registration. No metadata is
        written (config_dir is never persisted -- a deliberate core decision).

        *symlink_shared* mirrors the wizard's "Symlink to shared store" checkbox:
        when False, neither the six shared-store subdir links nor the skills link
        are created and the profile gets a plain dir (settings + registration
        still land). When True (default), all seven links are created.
        """
        self._require_write_stores()
        assert self.shared is not None and self.options is not None  # for type-checkers
        if name == "default":
            raise ValueError(f"'{name}' is a reserved name")
        target = self.path_for(name)
        if target.exists():
            raise FileExistsError(f"Profile directory already exists: {target}")

        effects.mkdir(target, parents=True)

        # Everything below was created by THIS call (the pre-mkdir FileExistsError
        # guard above guarantees the dir did not pre-exist), so any failure lets
        # us safely remove the whole target dir and re-raise, leaving no debris to
        # block a retry. Removal is symlink-safe (shared session data survives) --
        # it reuses the same helper delete() uses.
        try:
            # settings.json -- ATOMIC (the fix for the wizard's truncating write_text)
            write_json_atomic(target / "settings.json", settings)

            # Onboarding flag so CC skips the login screen under an injected token.
            if set_onboarding:
                self._set_onboarding_flag(target)

            # Symlink the shared-store subdirs (+ skills), skipping existing links.
            # Skipped entirely when the caller opted out of shared symlinking.
            if symlink_shared:
                for sub in SharedStore.SHARED_SUBDIRS:
                    link = target / sub
                    if link.exists() or link.is_symlink():
                        continue
                    sub_target = self.shared.subdir(sub)
                    effects.mkdir(sub_target, parents=True, exist_ok=True)
                    effects.symlink(link, sub_target)
                skills_link = target / "skills"
                if (
                    self.shared.skills_dir.is_dir()
                    and not skills_link.exists()
                    and not skills_link.is_symlink()
                ):
                    effects.symlink(skills_link, self.shared.skills_dir)

            # Register in options.json (pinned). No metadata -- config_dir dropped.
            self.options.add_pinned(_PROFILE_SEGMENT, name, _OPTIONS_DEFAULT)

            has_credentials = (target / ".credentials.json").exists()
            has_token = name in self.token_store.load()
            return Profile(
                name=name,
                path=target,
                has_credentials=has_credentials,
                has_token=has_token,
            )
        except BaseException:
            self._remove_profile_dir(name)
            raise

    def classify_shared_dirs(self, name: str) -> dict[str, str]:
        """Classify each shared-store entry in *name*'s dir into one of four states.

        Four states (intact, wrong-target, real-dir, missing) over
        SHARED_SUBDIRS + skills, resolved against this store's shared paths
        rather than module constants.
        """
        self._require_shared()
        assert self.shared is not None
        profile_path = self.path_for(name)
        states: dict[str, str] = {}
        entries = [(d, self.shared.subdir(d)) for d in SharedStore.SHARED_SUBDIRS]
        entries.append(("skills", self.shared.skills_dir))
        for entry_name, entry_target in entries:
            link = profile_path / entry_name
            if link.is_symlink():
                if link.resolve() == entry_target.resolve():
                    states[entry_name] = "intact"
                else:
                    states[entry_name] = "wrong-target"
            elif link.exists():
                states[entry_name] = "real-dir"
            else:
                states[entry_name] = "missing"
        return states

    def _remove_profile_dir(self, name: str) -> tuple[int, int]:
        """Remove *name*'s dir, unlinking symlinks WITHOUT following real data.

        Replicates ``profile_ops._remove_profile_dir``. Returns
        (removed_symlinks, removed_real).
        """
        profile_dir = self.path_for(name)
        if not profile_dir.is_dir():
            return 0, 0
        removed_symlinks = 0
        removed_real = 0
        for child in list(profile_dir.iterdir()):
            if child.is_symlink():
                effects.remove(child)
                removed_symlinks += 1
            elif child.is_dir():
                effects.rmtree(child)
                removed_real += 1
            else:
                effects.remove(child)
                removed_real += 1
        effects.rmdir(profile_dir)
        return removed_symlinks, removed_real

    def _purge_last_config(self, name: str) -> bool:
        """Drop ``last_config['profile']`` from state.json when it names *name*.

        Replicates ``profile_ops._purge_last_config_profile``.
        """
        assert self.state is not None
        last_config = self.state.get_value("last_config")
        if not isinstance(last_config, dict) or last_config.get("profile") != name:
            return False
        del last_config["profile"]
        self.state.set_value("last_config", last_config)
        return True

    def delete(
        self, name: str, *, allow_data_destruction: bool = False
    ) -> DeletionResult:
        """Delete a profile and clean up its stores. Refusals raise; success returns.

        Mirrors ``profile_ops.delete_profile_core``'s decision flow MINUS the
        running check (that is CLI policy, applied by callers at cutover).
        Refusal mapping (exceptions instead of a DeleteResult.refusal_reason):

        - reserved "default" -> ``ValueError``
        - neither registered nor present on disk -> ``ValueError`` (known
          profiles listed), mirroring the old "not-found" refusal
        - real data at a shared-dir name without *allow_data_destruction* ->
          ``ValueError`` naming the offending entries (old "data-destruction")
        """
        self._require_write_stores()
        assert self.options is not None
        if name == "default":
            raise ValueError(
                f"'{name}' is Claude Code's built-in ~/.claude, not a "
                "claudewheel profile; refusing to delete it."
            )

        options = self.options.load(_OPTIONS_DEFAULT)
        profile_sec = options.get(_PROFILE_SEGMENT, {})
        values = profile_sec.get("values", [])
        pinned = profile_sec.get("pinned", [])
        metadata = profile_sec.get("metadata", {})
        registered = name in values or name in pinned
        profile_dir = self.path_for(name)
        if not registered and not profile_dir.is_dir():
            known = sorted(set(values) | set(pinned))
            raise ValueError(
                f"Profile '{name}' is not registered in options.json and has no "
                f"directory on disk. Known profiles: {known or '<none>'}"
            )
        # removed_from_options reflects any presence across values/pinned/metadata.
        removed_from_options = registered or (name in metadata)

        # Data-destruction guard: refuse if any shared name holds REAL data.
        if profile_dir.is_dir():
            states = self.classify_shared_dirs(name)
            at_risk = sorted(d for d, s in states.items() if s == "real-dir")
            if at_risk and not allow_data_destruction:
                raise ValueError(
                    f"Profile '{name}' holds REAL data (not symlinks) at: "
                    f"{', '.join(at_risk)}. Deleting it would destroy that data; "
                    "pass allow_data_destruction=True to proceed."
                )

        sym, real = self._remove_profile_dir(name)
        self.options.remove_value(_PROFILE_SEGMENT, name, _OPTIONS_DEFAULT)
        removed_from_tokens = self.token_store.remove(name)
        purged = self._purge_last_config(name)

        return DeletionResult(
            removed_symlinks=sym,
            removed_real=real,
            removed_from_options=removed_from_options,
            removed_from_tokens=removed_from_tokens,
            last_config_purged=purged,
        )

    def _update_state_rename(self, old: str, new: str) -> None:
        """Swap ``last_config['profile']`` old->new. Replicates _update_state_rename."""
        assert self.state is not None
        last_config = self.state.get_value("last_config")
        if not isinstance(last_config, dict) or last_config.get("profile") != old:
            return
        last_config["profile"] = new
        self.state.set_value("last_config", last_config)

    def rename(self, old: str, new: str) -> None:
        """Rename a profile dir and swap all stores, crash-safe via a breadcrumb.

        Redesigned transaction: atomic breadcrumb write into the old dir,
        ``os.rename`` of the dir, token key move, options values+pinned swap
        (plus a verbatim metadata-key move -- NO config_dir rewrite), state swap,
        breadcrumb removal. Refuses "default" in either position.
        """
        self._require_write_stores()
        assert self.options is not None
        if old == "default" or new == "default":
            raise ValueError("'default' cannot be renamed to or from")
        old_dir = self.path_for(old)
        new_dir = self.path_for(new)
        if not old_dir.is_dir():
            raise ValueError(f"Profile directory does not exist: {old_dir}")
        if new_dir.exists():
            raise ValueError(f"Target directory already exists: {new_dir}")

        pending_path = old_dir / _RENAME_PENDING_FILE
        write_json_atomic(pending_path, {"from": old, "to": new})
        effects.rename(old_dir, new_dir)
        self.token_store.rename(old, new)  # effects: exempt -- TokenStore method, not Path.rename
        self.options.rename_value(_PROFILE_SEGMENT, old, new, _OPTIONS_DEFAULT)
        self._update_state_rename(old, new)
        breadcrumb = new_dir / _RENAME_PENDING_FILE
        if breadcrumb.exists():
            effects.remove(breadcrumb)

    def recover_incomplete_renames(self) -> list[dict[str, Any]]:
        """Finish or unwind interrupted renames from breadcrumbs. Returns a summary.

        Scans ``profiles_dir/*/.rename_pending``. Two crash windows:

        - dir already at ``to`` -> POST-rename crash: re-run the three idempotent
          store updates and drop the breadcrumb (the old code's behavior).
        - dir still at ``from`` -> PRE-rename crash: remove the stale breadcrumb.
          This fixes today's leak, where a pre-rename crash left the crumb
          forever (the old recovery only handled the post-rename window).

        Malformed breadcrumbs (unparseable or missing from/to) are reported and
        skipped, mirroring the old code's tolerant ``except`` behavior. Returns a
        list of ``{"action", ...}`` dicts for callers to log.
        """
        self._require_write_stores()
        assert self.options is not None
        actions: list[dict[str, Any]] = []
        if not self.profiles_dir.is_dir():
            return actions

        for profile_dir in self.profiles_dir.iterdir():
            if not profile_dir.is_dir():
                continue
            pending = profile_dir / _RENAME_PENDING_FILE
            if not pending.exists():
                continue
            try:
                data = json.loads(pending.read_text())
            except (json.JSONDecodeError, OSError):
                actions.append(
                    {
                        "action": "skipped",
                        "reason": "unparseable",
                        "profile": profile_dir.name,
                    }
                )
                continue
            old = data.get("from") if isinstance(data, dict) else None
            new = data.get("to") if isinstance(data, dict) else None
            if not old or not new:
                actions.append(
                    {
                        "action": "skipped",
                        "reason": "missing-fields",
                        "profile": profile_dir.name,
                    }
                )
                continue

            if profile_dir.name == new:
                # Post-rename window: finish the idempotent store updates.
                self.token_store.rename(old, new)  # effects: exempt -- TokenStore method, not Path.rename
                self.options.rename_value(_PROFILE_SEGMENT, old, new, _OPTIONS_DEFAULT)
                self._update_state_rename(old, new)
                effects.remove(pending)
                actions.append({"action": "completed", "from": old, "to": new})
            elif profile_dir.name == old:
                # Pre-rename window: the dir never moved -- drop the stale crumb.
                effects.remove(pending)
                actions.append({"action": "reverted", "from": old, "to": new})
            else:
                actions.append(
                    {
                        "action": "skipped",
                        "reason": "name-mismatch",
                        "profile": profile_dir.name,
                    }
                )

        return actions
