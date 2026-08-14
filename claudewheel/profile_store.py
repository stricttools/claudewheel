"""The profile store: enumerate, resolve, create, delete, and rename profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .appdata import OptionsFile, StateFile
from . import effects
from .archiver import ArchiveHandle, ProfileArchiver
from .effects import write_json_atomic
from .profile_data import PROFILE_DATA_DIRNAME, ProfileDataStore
from .shared_store import SharedStore
from .tokens import TokenStoreError

__all__ = [
    "Profile",
    "ProfileStore",
    "DeletionBookkeepingError",
    "DeletionResult",
    "DirSurvey",
]


class DeletionBookkeepingError(RuntimeError):
    """The archival succeeded; updating claudewheel's own stores did not.

    The window this names is small and real: :meth:`ProfileStore.delete`
    archives the directory first and writes options.json and state.json
    afterwards, so a full disk, a read-only home or a vanished store between
    the two leaves a profile that is archived, removed, and still registered.

    It exists so the handle survives that failure.  A deletion is recoverable
    only for as long as somebody knows the uuid, and an ``OSError`` raised out
    of a store write carries none -- the profile would be gone with its restore
    handle never printed.  Raising this instead keeps one invariant: **a
    successful archival always tells the caller the uuid**, whatever happens
    after it.

    ``archive`` is that handle (None only when there was nothing to archive),
    and the message names it, the command that restores it, the write that
    failed, and the deletion to re-run to finish the de-registration.
    ``reason`` is the underlying failure on its own, for a caller that composes
    its own report around the handle rather than printing the message whole.
    """

    def __init__(
        self, message: str, *, archive: "ArchiveHandle | None", reason: str = ""
    ) -> None:
        super().__init__(message)
        self.archive = archive
        self.reason = reason or message


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

# Names that are not claudewheel profiles and can never be created, renamed or
# deleted through it. One home for the rule: every caller asks
# ProfileStore.reserved_reason() rather than testing the string itself.
RESERVED_PROFILE_NAMES: tuple[str, ...] = ("default",)

# Every environment variable ProfileStore.env() can yield -- the profile's
# identity as Claude Code sees it. The launch path injects these for a named
# profile and removes them for the vanilla default, so both directions stay in
# step from one list: a variable added to env() without being added here would
# leak across a vanilla launch it was never meant to reach.
PROFILE_ENV_KEYS: tuple[str, ...] = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_SUBSCRIPTION_TYPE",
    "CLAUDE_CODE_RATE_LIMIT_TIER",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
)

# The variable that stops Claude Code installing the official plugin
# marketplace into a profile on first launch. There is no settings key for it:
# the marketplace settings keys are managed-policy-only, so the environment is
# the only lever, and it is undocumented client surface that could change in
# any release. Claude Code parses it as a boolean accepting 1/true/yes/on,
# case-insensitively; "1" is the plainest of those.
MARKETPLACE_AUTOINSTALL_OFF = "1"


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
class DeletionResult:
    """Success record from :meth:`ProfileStore.delete` (refusals raise instead).

    Mirrors the success-path fields of ``profile_ops.DeleteResult``: symlink and
    real-entry counts, taken by a read-only pass over the directory before it
    is removed, plus which stores were touched.  The profile's
    claudewheel data (its token entry) needs no field of its own: it lives
    inside the profile directory, so removing that directory removes it and it
    is counted among ``removed_real``.

    ``archive`` is the handle the archiving tool handed back -- the one thing
    that turns this record into something reversible.  It is carried out to the
    caller to be *reported*, never written anywhere: the archive is the
    authority on what was deleted and holds its own audit trail, so a
    launcher-side copy of the handle would be duplicate state with nobody
    owning its lifetime.  It is None under a preview, where the archival was
    recorded rather than performed.
    """

    removed_symlinks: int
    removed_real: int
    removed_from_options: bool
    last_config_purged: bool
    archive: "ArchiveHandle | None" = None


@dataclass(frozen=True)
class DirSurvey:
    """What a profile directory holds, read before anything is removed.

    ``symlinks`` are the shared-store links (unlinked, never followed);
    ``real_children`` is everything else at the top level, files and
    directories alike. ``names`` is what was seen, so a caller can say which
    entries the counts came from.
    """

    symlinks: int
    real_children: int
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileStore:
    """Path-injected facade that enumerates profiles and resolves launch env.

    All paths are explicit -- the store never reads module path constants and
    never calls ``Path.home()``. ``profiles_dir`` is the claudewheel profiles
    directory; ``claude_dir`` is Claude Code's built-in ``~/.claude`` (the
    "default" profile). Token data comes from each profile's own
    :class:`~claudewheel.profile_data.ProfileDataStore`, reached through
    :meth:`data_for`.
    """

    profiles_dir: Path
    claude_dir: Path
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

    def reserved_reason(self, name: str) -> str | None:
        """Why *name* cannot be destroyed here, or None when it can.

        The query every deletion path asks BEFORE it renders anything. It
        exists because the answer has to arrive earlier than the confirmation:
        the vanilla ``default`` profile used to reach a data-destruction page
        advertising a command the store would then refuse, which is a
        destructive-looking dialogue about an operation that could never
        happen. The message deliberately names no command -- there is no
        invocation, forced or otherwise, that deletes ``~/.claude``.
        """
        if name in RESERVED_PROFILE_NAMES:
            return (
                f"'{name}' is Claude Code's built-in ~/.claude, not a "
                "claudewheel profile. Claude Code manages it; claudewheel "
                "neither creates, renames nor deletes it."
            )
        return None

    def data_for(self, name: str) -> ProfileDataStore:
        """The claudewheel data store inside *name*'s profile directory.

        The single door to a profile's token entry and plan-tier fields: one
        file per profile, inside the profile directory itself.
        """
        return ProfileDataStore(self.path_for(name))

    def enumerate(self) -> list[Profile]:
        """Discover all profiles, encoding the historical discovery rules verbatim.

        ``has_token`` is read from each profile's own data store, so a corrupt
        token file raises :class:`TokenStoreError` -- the hard-error contract.
        :meth:`discover` is the variant that takes an explicit policy for that.

        Rules encoding the profile-discovery behavior:
        1. ``claude_dir`` qualifies as "default" whenever it IS A DIRECTORY.
           ``~/.claude`` is Claude Code's own config dir -- managed by Claude
           Code, not cw -- so cw cannot verify its auth (``.credentials.json``
           may live elsewhere, e.g. macOS Keychain). ``has_credentials`` tracks
           the ``.credentials.json`` presence but is NOT required for discovery.
        2. Each subdir of ``profiles_dir`` qualifies when it holds
           ``.credentials.json``, ``settings.json``, or claudewheel's own
           per-profile data directory (:data:`PROFILE_DATA_DIRNAME`);
           has_credentials tracks the ``.credentials.json`` presence.
        3. has_token is True when the profile's own data store holds a token.
        Result is sorted by name.
        """
        return self._enumerate(on_corrupt_tokens="raise")

    def _records(self) -> list[tuple[str, Path, bool]]:
        """Apply the discovery rules WITHOUT opening any token file.

        Returns ``(name, path, has_credentials)`` for every profile the
        directory layout reveals -- rules 1 and 2 of :meth:`enumerate`, which
        need nothing but directory presence. Token reads (rule 3) happen in the
        callers, so a caller resolving ONE profile opens ONE profile's secret
        file and a corrupt file in an unrelated profile cannot decide its fate.
        """
        records: list[tuple[str, Path, bool]] = []

        # Rule 1: claude_dir as "default" whenever it is a directory (lenient --
        # ~/.claude is managed by Claude Code, so cw cannot require credentials
        # it may not be able to see). has_credentials reflects on-disk reality.
        if self.claude_dir.is_dir():
            has_default_credentials = (self.claude_dir / ".credentials.json").exists()
            records.append(("default", self.claude_dir, has_default_credentials))

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
                has_data = (entry / PROFILE_DATA_DIRNAME).is_dir()
                if has_credentials or has_settings or has_data:
                    records.append((name, entry, has_credentials))

        return records

    def _record_for(self, name: str) -> tuple[str, Path, bool] | None:
        """The discovery record for *name*, or None when no profile answers to it.

        Presence only -- no token file is opened, by this profile or any other.
        """
        for record in self._records():
            if record[0] == name:
                return record
        return None

    def _enumerate(
        self, *, on_corrupt_tokens: Literal["raise", "swallow"]
    ) -> list[Profile]:
        """Enumeration proper; the per-profile corrupt-token policy is applied here."""
        # Rule 3 over the presence records: mark token presence, one read per
        # profile.
        profiles: list[Profile] = []
        for name, path, has_credentials in self._records():
            try:
                has_token = ProfileDataStore(path).has_token()
            except TokenStoreError:
                if on_corrupt_tokens == "raise":
                    raise
                has_token = False
            profiles.append(Profile(name, path, has_credentials, has_token))
        profiles.sort(key=lambda p: p.name)
        return profiles

    def discover(
        self, *, on_corrupt_tokens: Literal["raise", "swallow"]
    ) -> list[Profile]:
        """Enumerate profiles with an EXPLICIT corrupt-token policy.

        The single shared home of the "enumerate profiles, deciding what to do
        about a corrupt token file" convention. Every consumer (health,
        reconcile, patch-profiles) routes through here so the swallow
        ``try/except`` lives in exactly one place.

        *on_corrupt_tokens* is mandatory and has no default -- the caller must
        choose:

        - ``"raise"``: a corrupt token file raises :class:`TokenStoreError`
          (the hard-error contract; health reports the failing profile).
        - ``"swallow"``: a corrupt token file leaves that profile's
          ``has_token`` False (additive maintenance that touches
          permissions/hooks, not tokens).

        The policy is applied per profile now that each profile carries its own
        token file: one unreadable file no longer decides the whole run.
        """
        if on_corrupt_tokens not in ("raise", "swallow"):
            raise ValueError(
                "on_corrupt_tokens must be 'raise' or 'swallow', got "
                f"{on_corrupt_tokens!r}"
            )
        return self._enumerate(on_corrupt_tokens=on_corrupt_tokens)

    def get(self, name: str) -> Profile | None:
        """Return the :class:`Profile` for *name*, or None if absent.

        Single-profile resolution: the name is answered from directory presence
        and only *name*'s own token file is read, so a corrupt token file in an
        unrelated profile cannot break this lookup. A corrupt file in *name*
        itself still raises :class:`TokenStoreError`, naming that file.
        """
        record = self._record_for(name)
        if record is None:
            return None
        found, path, has_credentials = record
        return Profile(found, path, has_credentials, ProfileDataStore(path).has_token())

    def env(self, name: str) -> dict[str, str]:
        """Resolve a profile name to launch env vars. Read-only, no terminal I/O.

        Resolving *name* reads *name*'s data and nothing else: the name is
        answered from directory presence (the discovery rules), then that one
        profile's token file is opened. A corrupt token file in some unrelated
        profile therefore cannot break this launch, while a corrupt file in
        *name* itself raises :class:`TokenStoreError` naming that file. An
        unknown *name* raises :class:`ValueError` listing the available profile
        names -- itself derived from directory presence, so producing the list
        opens no token file either.

        For every named profile the result carries ``CLAUDE_CONFIG_DIR`` and
        adds ``CLAUDE_CODE_OAUTH_TOKEN`` when the profile's own data store
        yields a truthy token. The ``"default"`` profile is the EXCEPTION: it is
        Claude Code's own ``~/.claude``, managed by Claude Code and strictly
        read-only to cw, so it resolves to an EMPTY env -- no
        ``CLAUDE_CONFIG_DIR`` and no token injection (the vanilla launch path).

        A profile whose token entry declares plan-tier fields additionally
        carries ``CLAUDE_CODE_SUBSCRIPTION_TYPE`` and/or
        ``CLAUDE_CODE_RATE_LIMIT_TIER``. Claude Code reads a subscription tier
        from those variables and ONLY from them when auth arrives as a setup
        token (``CLAUDE_CODE_OAUTH_TOKEN``); its own fallback -- fetching the
        OAuth profile -- is unavailable because setup tokens lack the
        ``user:profile`` scope. Without them the tier resolves to null and
        tier-dependent checks fail closed. Declared values are validated here:
        an unrecognized one is a hard error, never a silently ignored field.

        Every named profile also carries
        ``CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL``, which stops
        Claude Code cloning the official plugin marketplace into the profile on
        first launch. Two things to know about it. There is no settings key for
        the same effect -- the marketplace settings keys are managed-policy-only
        -- so the environment is the only lever, and it is undocumented client
        surface. And the suppression is effectively ONE-WAY per profile: once
        the client has recorded the install as ``policy_blocked`` it treats that
        as final, so removing the variable later does not make it try again.
        Un-suppressing a profile means installing the marketplace yourself.
        """
        if self._record_for(name) is None:
            available = sorted(n for n, _, _ in self._records())
            raise ValueError(
                f"Profile {name!r} not found. Available profiles: {available}"
            )

        if name == "default":
            # Vanilla default: Claude Code manages ~/.claude itself. cw injects
            # neither a config dir nor a token.
            return {}

        env: dict[str, str] = {
            "CLAUDE_CONFIG_DIR": str(self.path_for(name)),
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": (
                MARKETPLACE_AUTOINSTALL_OFF
            ),
        }
        data = self.data_for(name)
        token = data.token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env.update(data.plan_env())
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
        if name in RESERVED_PROFILE_NAMES:
            raise ValueError(f"'{name}' is a reserved name")
        target = self.path_for(name)
        if target.exists():
            raise FileExistsError(f"Profile directory already exists: {target}")

        effects.mkdir(target, parents=True)

        # Everything below was created by THIS call (the pre-mkdir FileExistsError
        # guard above guarantees the dir did not pre-exist), so any failure lets
        # us safely remove the whole target dir and re-raise, leaving no debris to
        # block a retry. Removal is symlink-safe (shared session data survives).
        # It is a plain removal, deliberately NOT the archiving delegation
        # delete() performs: nothing here is the user's data yet, and archiving
        # the debris of a half-built profile would put a record in the archive
        # that nobody would ever want to restore.
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
            has_token = self.data_for(name).has_token()
            return Profile(
                name=name,
                path=target,
                has_credentials=has_credentials,
                has_token=has_token,
            )
        except BaseException:
            self._discard_partial_profile_dir(name)
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

    def survey_profile_dir(self, name: str) -> DirSurvey:
        """Count what *name*'s directory holds, WITHOUT touching any of it.

        A pure read, taken before anything is removed. The counts used to fall
        out of the removal loop, which tied two things together that are not
        the same thing: what the directory contained, and how it was emptied.
        Only the second is going to change (the removal is later delegated to
        an archiving tool), and the first must survive that intact.

        Symlinks are counted as symlinks and never followed -- the shared-store
        links point at data that outlives the profile.
        """
        profile_dir = self.path_for(name)
        if not profile_dir.is_dir():
            return DirSurvey(symlinks=0, real_children=0, names=())
        symlinks = 0
        real_children = 0
        names: list[str] = []
        for child in sorted(profile_dir.iterdir()):
            names.append(child.name)
            if child.is_symlink():
                symlinks += 1
            else:
                real_children += 1
        return DirSurvey(
            symlinks=symlinks, real_children=real_children, names=tuple(names)
        )

    def _discard_partial_profile_dir(self, name: str) -> None:
        """Remove the debris of a FAILED :meth:`create`, symlink-safe.

        Only :meth:`create`'s rollback calls this, and only over a directory
        this same call made moments ago. It is not a deletion: nothing in it is
        the user's data, so there is nothing to archive and no handle anyone
        would ever restore. Deleting a real profile goes through
        :meth:`_archive_profile_dir`.

        Symlinks are unlinked without being followed, exactly as before, so a
        shared-store link created a moment ago cannot take the store with it.
        """
        profile_dir = self.path_for(name)
        if not profile_dir.is_dir():
            return
        for child in list(profile_dir.iterdir()):
            if child.is_symlink():
                effects.remove(child)
            elif child.is_dir():
                effects.rmtree(child)
            else:
                effects.remove(child)
        effects.rmdir(profile_dir)

    def _archive_profile_dir(
        self, name: str, archiver: ProfileArchiver
    ) -> ArchiveHandle | None:
        """Hand *name*'s directory to the archiving tool, which then removes it.

        Removal only: what was there is :meth:`survey_profile_dir`'s answer,
        taken before this runs.

        This used to be a per-child removal loop ending in ``rmdir``, whose
        must-be-empty failure was the safety property: a child left behind was
        a hard error rather than something quietly taken with the tree. That
        property belongs to the deletion, not to the loop that implemented it,
        so it survives the delegation in the shape the delegation can express:
        the directory itself must be gone afterwards, and a directory still
        standing after a reportedly successful archival raises instead of being
        removed some other way.

        The whole directory is handed over in one piece, which is safe for the
        shared store precisely because the walk does not follow symlinks: the
        shared-store links are recorded as links and recreated on restore, so
        the store behind them is never read, never copied and never touched.

        Under a preview the invocation is recorded and nothing ran, so there is
        no handle and no directory to check.
        """
        profile_dir = self.path_for(name)
        if not profile_dir.is_dir():
            return None
        handle = archiver.archive(
            profile_dir,
            description=(
                f"claudewheel profile delete '{name}': the profile directory "
                "with its settings, credentials and stored OAuth token"
            ),
        )
        if not effects.previewing() and profile_dir.exists():
            leftovers = sorted(p.name for p in profile_dir.iterdir())
            raise RuntimeError(
                f"Profile directory {profile_dir} still exists after it was "
                f"archived: {', '.join(leftovers) or '<empty>'}"
            )
        return handle

    @staticmethod
    def _bookkeeping_failure(
        name: str, handle: "ArchiveHandle | None", error: OSError
    ) -> str:
        """What to say when the archival worked and the store writes did not.

        Four things, in the order they are useful: what really happened, the
        handle (first, and spelled out, because it is the only thing that
        undoes the deletion), the write that failed, and how to finish the
        cleanup -- re-running the same deletion, which now finds no directory
        to archive and only updates the stores.
        """
        archived = (
            f"archived as {handle.uuid} and removed"
            if handle is not None
            else "removed"
        )
        restore = (
            f" Restore it with: {handle.restore_command}." if handle is not None else ""
        )
        return (
            f"Profile '{name}' was {archived}, but claudewheel could not "
            f"update its own registration: {error}. options.json and "
            f"state.json may still name it -- run `claudewheel profile delete "
            f"{name}` again to finish the cleanup, which archives nothing "
            f"because the directory is already gone.{restore}"
        )

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
        self,
        name: str,
        *,
        archiver: ProfileArchiver,
        allow_data_destruction: bool = False,
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

        The reserved-name refusal is checked first, ahead of the write-store
        requirement: callers consult :meth:`reserved_reason` before they render
        anything, and this backstop must give the same answer whatever else is
        or is not wired up.

        *archiver* is required rather than defaulted, and the store neither
        finds one nor decides what to do without one: whether the archiving
        tool is present, whether it ships what the delegation uses, and whether
        to offer to install it are decisions with a user to ask, so they belong
        to the handler. A store that silently removed the directory when no
        archiver was handed in would be exactly the kind of quiet degradation
        this delegation exists to remove.

        Order is the contract's, and the refusals come first: the archival is
        the first destructive step, so an ``ArchiveError`` out of it leaves the
        profile on disk AND leaves every store still naming it -- options.json
        and state.json are touched only after the directory is really gone.
        """
        reserved = self.reserved_reason(name)
        if reserved is not None:
            raise ValueError(reserved)
        self._require_write_stores()
        assert self.options is not None

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

        # Counted before anything goes: the report describes the directory
        # that was there, not the loop that emptied it.
        survey = self.survey_profile_dir(name)
        handle = self._archive_profile_dir(name, archiver)
        try:
            self.options.remove_value(_PROFILE_SEGMENT, name, _OPTIONS_DEFAULT)
            purged = self._purge_last_config(name)
        except OSError as e:
            # Past the point of no return: the directory is archived and gone.
            # An OSError raised as itself would take the handle with it, so it
            # is re-raised as the one error that carries it.
            raise DeletionBookkeepingError(
                self._bookkeeping_failure(name, handle, e),
                archive=handle,
                reason=str(e),
            ) from e

        return DeletionResult(
            removed_symlinks=survey.symlinks,
            removed_real=survey.real_children,
            removed_from_options=removed_from_options,
            last_config_purged=purged,
            archive=handle,
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
        ``os.rename`` of the dir, options values+pinned swap (plus a verbatim
        metadata-key move -- NO config_dir rewrite), state swap, breadcrumb
        removal. Refuses "default" in either position.

        The profile's token entry needs no step of its own: it lives inside the
        directory being renamed, so it travels with it.
        """
        self._require_write_stores()
        assert self.options is not None
        for candidate in (old, new):
            if candidate in RESERVED_PROFILE_NAMES:
                raise ValueError(f"'{candidate}' cannot be renamed to or from")
        old_dir = self.path_for(old)
        new_dir = self.path_for(new)
        if not old_dir.is_dir():
            raise ValueError(f"Profile directory does not exist: {old_dir}")
        if new_dir.exists():
            raise ValueError(f"Target directory already exists: {new_dir}")

        pending_path = old_dir / _RENAME_PENDING_FILE
        write_json_atomic(pending_path, {"from": old, "to": new})
        effects.rename(old_dir, new_dir)
        self.options.rename_value(_PROFILE_SEGMENT, old, new, _OPTIONS_DEFAULT)
        self._update_state_rename(old, new)
        breadcrumb = new_dir / _RENAME_PENDING_FILE
        if breadcrumb.exists():
            effects.remove(breadcrumb)

    def recover_incomplete_renames(self) -> list[dict[str, Any]]:
        """Finish or unwind interrupted renames from breadcrumbs. Returns a summary.

        Scans ``profiles_dir/*/.rename_pending``. Two crash windows:

        - dir already at ``to`` -> POST-rename crash: re-run the idempotent
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
