"""Delegate profile deletion to saferm, so a deleted profile can be restored.

Deleting a profile used to be a removal loop: unlink the shared-store symlinks,
remove every real child, rmdir the directory.  What went that way was gone --
``settings.json``, ``.credentials.json`` and the profile's own stored OAuth
token among it.  Deletion now hands the whole directory to `saferm
<https://github.com/smm-h/saferm>`_ instead, which archives it and then removes
it, so the same operation is recoverable with one ``saferm undelete``.

What this module owns
---------------------

* **Finding saferm** -- claudewheel's own installed copy first
  (``~/.claudewheel/bin/saferm``, where :func:`install` puts one), then
  whatever ``PATH`` resolves.
* **Negotiating on features, never on a version string.**  :func:`probe` asks
  ``saferm capabilities --json`` what this binary ships and compares the answer
  against :data:`REQUIRED_FEATURES`.  A missing verb and a missing feature are
  treated exactly like saferm not being installed at all -- one code path, one
  remedy.  A version comparison is deliberately not done: a locally built
  saferm reports a Go pseudo-version no semver parser accepts, and a release
  number says nothing about what a build actually carries.
* **The delegation itself** -- :meth:`Saferm.archive`, routed through
  claudewheel's effects chokepoint so a ``--dry-run`` records the invocation
  and removes nothing.
* **Installing saferm** -- :func:`install`, shaped on
  :mod:`claudewheel.install`'s verified download: fetch the release's
  ``checksums.txt``, pick this platform's asset, download it, verify its
  SHA-256 against the manifest line, and only then unpack and rename the binary
  into place.

What this module deliberately does not own
------------------------------------------

**No record of the archive is kept anywhere in claudewheel.**  saferm's archive
already holds everything needed to restore, and it has its own audit trail, so
a second copy of the handle in a launcher-side file would be state that can go
stale and that nobody owns the lifetime of.  The handle is *reported* -- printed
where the user can see it and see the command that uses it -- and after that it
lives in ``saferm list`` like every other deletion.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from . import effects

#: The program claudewheel delegates deletion to.
SAFERM = "saferm"

#: The features the delegation actually uses, pinned by name.  Each one is a
#: thing :meth:`Saferm.archive` or the restore instructions depend on:
#:
#: * ``machine-payloads`` -- the ``--json`` envelope carrying the archived
#:   records, which is where the handle comes from.
#: * ``on-error-modes`` -- ``--on-error``, mandatory on ``delete``.
#: * ``git-index-switches`` -- ``--no-update-git-index``, so archiving somebody
#:   else's directory never stages anything in a git index.
#: * ``uuid-handles`` -- the durable handle ``saferm undelete`` accepts.
REQUIRED_FEATURES = frozenset(
    {
        "machine-payloads",
        "on-error-modes",
        "git-index-switches",
        "uuid-handles",
    }
)

#: How long the capabilities probe may take.  It reads nothing and opens no
#: database, so a slow answer means something is wrong rather than busy.
PROBE_TIMEOUT = 15.0

#: How long one archival may take.  A profile directory is small, but the
#: conversation history under a real (non-symlinked) shared name is not.
ARCHIVE_TIMEOUT = 1800.0

#: Where the release assets and their checksum manifest live.
RELEASE_BASE = "https://github.com/smm-h/saferm/releases/latest/download"

#: Timeouts for the two halves of an install.
MANIFEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 300.0

#: What to tell a user, or a machine, to run.  Ordered as claudewheel would
#: install it first, then the ecosystem's own channels.
INSTALL_COMMANDS = (
    "go install github.com/smm-h/saferm@v0",
    "npm install -g saferemove",
    "uv tool install saferm",
    "brew install smm-h/tap/saferm",
)


class ArchiveError(RuntimeError):
    """saferm refused or failed, and nothing was destroyed.

    Raised from :meth:`Saferm.archive` for every answer that stops the deletion
    before claudewheel has mutated anything of its own: the delegation is the
    first destructive step, so the profile is still on disk and every store
    still names it.  Each of these messages says exactly that.

    :class:`ArchiveUnreadable` is the deliberate exception to the sentence
    above and subclasses this one, because a caller's next move -- stop, report,
    change nothing else -- is the same either way.
    """


class ArchiveUnreadable(ArchiveError):
    """saferm reported success, and its answer could not be read.

    The distinction from a plain :class:`ArchiveError` is the only thing that
    is true here and false there: saferm exited 0, so the archival ran and the
    profile directory is gone.  What is missing is claudewheel's handle for it.

    Nothing about that is quietly recoverable, so it is a hard error like any
    other -- but its message never claims the profile is still there, always
    carries whatever handle information the answer did contain (a uuid alone is
    enough to restore with), and always names ``saferm list``, where the record
    is regardless of what claudewheel could parse.
    """


class InstallError(RuntimeError):
    """The install could not be completed. saferm is still absent."""


@dataclass(frozen=True)
class ArchiveHandle:
    """What one delegated archival hands back.

    *uuid* is the durable handle: ``saferm undelete <uuid>`` restores the whole
    profile directory, the stored token included.  *group_id* names the
    invocation, so an archival of several paths stays recoverable as a batch --
    claudewheel always hands over exactly one directory, but the identifier is
    minted either way and is worth reporting.
    """

    uuid: str
    group_id: str
    path: str
    size: int

    @property
    def restore_command(self) -> str:
        """The one command that puts the profile back.

        ``--no-update-git-index`` is on both sides of the round trip, for one
        reason.  A profile directory can sit inside a git worktree -- a
        version-controlled ``~/dotfiles`` is the ordinary case -- and
        ``undelete`` stages the restored path by default, which would put the
        profile's ``.credentials.json`` and its stored OAuth token into an
        index claudewheel does not own.  The archival refuses to touch that
        index (see :meth:`Saferm.archive`); the restore claudewheel prints
        refuses too, so following the printed instruction cannot stage a secret
        somebody is about to commit.
        """
        return f"{SAFERM} undelete --no-update-git-index {self.uuid}"


class ProfileArchiver(Protocol):
    """What :meth:`claudewheel.profile_store.ProfileStore.delete` requires.

    The store never locates, probes or installs anything -- it is handed
    something that can archive a directory, and refuses to delete without one.
    :class:`Saferm` is the implementation; tests supply their own.
    """

    def archive(self, path: Path, *, description: str) -> ArchiveHandle | None:
        """Archive *path* and remove it, or record the invocation in a preview.

        Returns None when the invocation was recorded rather than performed
        (``--dry-run``): nothing ran, so there is no handle to hand back.
        Raises :class:`ArchiveError` when the archival failed.
        """
        ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class Saferm:
    """A saferm binary that has answered the capabilities probe.

    Constructed only by :func:`detect` and :func:`install`, so holding one is
    itself the statement that the negotiation succeeded.
    """

    binary: Path
    features: frozenset[str]

    def archive(self, path: Path, *, description: str) -> ArchiveHandle | None:
        """Hand *path* to saferm: archived, then removed.

        The invocation is fully explicit, because every one of its flags is a
        decision claudewheel is making on the user's behalf:

        * ``--on-error abort`` -- a profile is exactly one directory, so there
          is no remainder to carry on with.  ``continue`` would only mean
          "report the failure later".
        * ``--no-update-git-index`` -- claudewheel is archiving somebody else's
          directory.  A profile that happens to sit inside a git worktree must
          not have its removal staged in that worktree's index.
        * ``--description`` -- mandatory, and the audit trail's whole point.
        * ``--json`` -- the envelope is where the handle comes from.  Parsing
          prose would be a second interface nobody declared.

        Routed through the effects chokepoint with an explicit grant, so a
        ``--dry-run`` records ``run: saferm delete ...`` and removes nothing.
        """
        argv = [
            str(self.binary),
            "delete",
            "--on-error",
            "abort",
            "--no-update-git-index",
            "--recursive",
            "--description",
            description,
            "--json",
            str(path),
        ]
        try:
            result = effects.run(
                argv,
                capture_output=True,
                text=True,
                timeout=ARCHIVE_TIMEOUT,
                resource=f"profile-archive:{path}",
                grant="archive-delegation",
            )
        except subprocess.TimeoutExpired as e:
            raise ArchiveError(
                f"{SAFERM} did not finish archiving {path} within "
                f"{ARCHIVE_TIMEOUT:.0f}s. Nothing was deleted."
            ) from e
        except OSError as e:
            raise ArchiveError(
                f"could not run {self.binary} to archive {path}: {e}. "
                "Nothing was deleted."
            ) from e

        if effects.unsettled(result):
            # A preview: the run was recorded, the directory is still there,
            # and there is no handle because nothing was archived.
            return None

        if result.returncode != 0:
            raise ArchiveError(
                f"{SAFERM} exited {result.returncode} archiving {path}"
                f"{_detail(result)}. Nothing was deleted."
            )

        # Past this point saferm has exited 0, which means the archival ran and
        # the directory is gone. Every refusal above says "nothing was
        # deleted"; nothing below it may, however badly the answer reads. The
        # parse is strict for the same reason: a handle assembled out of a
        # malformed record -- an empty uuid, a size that is not a number -- is
        # a handle that restores nothing, reported as if it did.
        payload = _payload(result.stdout)
        if payload is None:
            raise _unreadable(path, "could not be parsed")
        group_id = str(payload.get("group_id", "") or "")
        archived = payload.get("archived")
        if not isinstance(archived, list) or not archived:
            raise _unreadable(path, "named no archived record", group_id=group_id)
        record = archived[0]
        if not isinstance(record, dict):
            raise _unreadable(
                path,
                f"named an archived record that is not an object ({record!r})",
                group_id=group_id,
            )
        uuid = str(record.get("uuid", "") or "").strip()
        if not uuid:
            raise _unreadable(
                path, "named an archived record with no uuid", group_id=group_id
            )
        raw_size = record.get("size", 0) or 0
        try:
            size = int(raw_size)
        except (TypeError, ValueError):
            raise _unreadable(
                path,
                f"named a size that is not a number ({raw_size!r})",
                uuid=uuid,
                group_id=group_id,
            ) from None
        return ArchiveHandle(
            uuid=uuid,
            group_id=group_id,
            path=str(record.get("path", path)),
            size=size,
        )


@dataclass(frozen=True)
class Unavailable:
    """Why deletion cannot proceed, and what to do about it.

    Three shapes, one remedy.  ``absent`` is no binary anywhere; ``no-verb`` is
    a binary too old to answer the probe at all; ``missing-features`` is one
    that answers but does not ship what the delegation uses.  All three route
    to the same install-or-upgrade offer, which is the whole point of
    negotiating on features rather than on a version number.
    """

    kind: Literal["absent", "no-verb", "missing-features"]
    binary: Path | None = None
    missing: tuple[str, ...] = ()

    @property
    def upgrade(self) -> bool:
        """True when a saferm exists and is merely too old."""
        return self.binary is not None

    def diagnosis(self) -> str:
        """One line naming saferm and saying exactly what is wrong with it."""
        if self.kind == "absent":
            return f"{SAFERM} is not installed."
        if self.kind == "no-verb":
            return (
                f"{SAFERM} at {self.binary} is too old: it does not answer "
                "`saferm capabilities`."
            )
        return f"{SAFERM} at {self.binary} does not ship: {', '.join(self.missing)}."

    def stakes(self, name: str) -> str:
        """Why the deletion stops here rather than proceeding without saferm."""
        return (
            f"Deleting profile '{name}' delegates to {SAFERM} so it can be "
            "restored afterwards. Without it the deletion would be "
            "irreversible: the profile directory, its settings.json, its "
            ".credentials.json and its stored OAuth token would be gone for "
            "good."
        )

    def remedy(self) -> str:
        """The install or upgrade lines, indented for a message body."""
        return "\n".join(f"  {cmd}" for cmd in INSTALL_COMMANDS)

    def headless_error(self, name: str) -> str:
        """The hard-abort message for a run with no terminal to ask at.

        A non-interactive caller is a machine -- an agent, or a monitored job --
        and a hard error is the input to its next action: nothing happened, the
        profile is still there, and the remedy is named.  There is deliberately
        no flag that proceeds anyway, so this message has no override to teach.
        """
        verb = "Upgrade" if self.upgrade else "Install"
        return (
            f"error: {self.diagnosis()}\n"
            f"{self.stakes(name)}\n"
            f"There is no terminal to offer the install at, so nothing was "
            f"deleted -- profile '{name}' is untouched.\n"
            f"{verb} {SAFERM} and run this again:\n"
            f"{self.remedy()}"
        )

    def offer_lines(self, name: str) -> list[str]:
        """The interactive introduction to the install offer."""
        return [
            self.diagnosis(),
            "",
            *self.stakes(name).split(". "),
        ]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def bin_dir(root: Path) -> Path:
    """Where claudewheel keeps the saferm it installed itself."""
    return root / "bin"


def locate(root: Path) -> Path | None:
    """The saferm binary claudewheel would use, or None.

    claudewheel's own copy wins over ``PATH``: a user who accepted the install
    offer gets the binary that offer produced, whatever an older one on ``PATH``
    would have answered.
    """
    own = bin_dir(root) / SAFERM
    if own.is_file() and os.access(own, os.X_OK):
        return own
    found = shutil.which(SAFERM)
    return Path(found) if found else None


def probe(binary: Path) -> frozenset[str] | None:
    """Ask *binary* what it ships. None when it cannot answer at all.

    A declared read: ``capabilities`` is ``read_only``, reads no database and
    creates no state directory, so it executes in every mode -- a preview that
    could not find out whether saferm is usable could not preview a deletion.
    """
    try:
        result = effects.run(
            [str(binary), "capabilities", "--json"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            read=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    payload = _payload(result.stdout)
    if payload is None:
        return None
    features = payload.get("features")
    if not isinstance(features, list):
        return None
    return frozenset(str(f) for f in features)


def detect(root: Path) -> Saferm | Unavailable:
    """Find a saferm that ships everything the delegation uses.

    The single entry point every deletion path calls before it decides
    anything.  A missing binary, a binary with no ``capabilities`` verb and a
    binary missing one feature are three different findings with one answer.
    """
    binary = locate(root)
    if binary is None:
        return Unavailable(kind="absent")
    features = probe(binary)
    if features is None:
        return Unavailable(kind="no-verb", binary=binary)
    missing = tuple(sorted(REQUIRED_FEATURES - features))
    if missing:
        return Unavailable(kind="missing-features", binary=binary, missing=missing)
    return Saferm(binary=binary, features=features)


# ---------------------------------------------------------------------------
# Installing saferm
# ---------------------------------------------------------------------------


def asset_name(version: str) -> str:
    """The release asset for this platform, named the way GoReleaser named it."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = machine
    if sys.platform == "darwin":
        goos, ext = "darwin", "tar.gz"
    elif sys.platform == "win32":
        goos, ext = "windows", "zip"
    else:
        goos, ext = "linux", "tar.gz"
    return f"{SAFERM}_{version}_{goos}_{arch}.{ext}"


def fetch_checksums() -> dict[str, str]:
    """The latest release's checksum manifest, as ``{asset: sha256}``.

    A declared read, exactly like the Claude Code manifest fetch: it is what
    names the version, the asset and the digest, so a preview that could not
    read it could not say what it would install.
    """
    url = f"{RELEASE_BASE}/checksums.txt"
    try:
        body = effects.http_read(
            url, headers={"User-Agent": "claudewheel"}, timeout=MANIFEST_TIMEOUT
        )
    except urllib.error.HTTPError as e:
        raise InstallError(f"could not read {url} (HTTP {e.code})") from e
    except Exception as e:
        raise InstallError(f"could not read {url}: {e}") from e

    manifest: dict[str, str] = {}
    for line in body.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            manifest[parts[1]] = parts[0]
    if not manifest:
        raise InstallError(f"{url} named no assets")
    return manifest


def _version_from(manifest: dict[str, str]) -> str:
    """The version every asset in *manifest* carries (``saferm_<v>_<os>_...``)."""
    for name in manifest:
        parts = name.split("_")
        if len(parts) >= 4 and parts[0] == SAFERM:
            return parts[1]
    raise InstallError("the checksum manifest names no saferm asset")


def install(
    root: Path, progress_callback: Callable[[int, int], None] | None = None
) -> Path:
    """Download, verify and install the latest saferm under *root*.

    The shape is :func:`claudewheel.install.install_version`'s, and for the
    same reason: an executable fetched over the network is installed only after
    its content has been checked against a digest the release published
    separately.  A mismatch removes the staged file and raises -- there is no
    branch that installs it anyway.
    """
    import hashlib

    manifest = fetch_checksums()
    version = _version_from(manifest)
    asset = asset_name(version)
    if asset not in manifest:
        raise InstallError(
            f"{SAFERM} {version} publishes no asset for this platform "
            f"({asset}). Available: {', '.join(sorted(manifest))}"
        )
    if asset.endswith(".zip"):
        raise InstallError(
            f"claudewheel cannot unpack {asset}; install {SAFERM} yourself:\n"
            + "\n".join(f"  {cmd}" for cmd in INSTALL_COMMANDS)
        )
    expected = manifest[asset]

    url = f"{RELEASE_BASE}/{asset}"
    try:
        blob = effects.http_read(
            url, headers={"User-Agent": "claudewheel"}, timeout=DOWNLOAD_TIMEOUT
        )
    except Exception as e:
        raise InstallError(f"could not download {url}: {e}") from e
    if progress_callback is not None:
        progress_callback(len(blob), len(blob))

    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise InstallError(
            f"checksum mismatch for {asset}: expected {expected[:16]}..., "
            f"got {actual[:16]}...  Nothing was installed."
        )

    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            member = tar.extractfile(SAFERM)
            if member is None:
                raise InstallError(f"{asset} holds no `{SAFERM}` entry")
            payload = member.read()
    except InstallError:
        raise
    except (tarfile.TarError, KeyError, OSError) as e:
        raise InstallError(f"could not unpack {asset}: {e}") from e

    target_dir = bin_dir(root)
    dest = target_dir / SAFERM
    tmp = dest.with_name(dest.name + ".downloading")
    effects.mkdir(target_dir, parents=True, exist_ok=True)
    effects.write_bytes(tmp, payload)
    effects.chmod(tmp, 0o755)
    effects.rename(tmp, dest)
    return dest


# ---------------------------------------------------------------------------
# Envelope reading
# ---------------------------------------------------------------------------


def _payload(stdout: Any) -> dict[str, Any] | None:
    """The payload object out of a strictcli machine-mode envelope, or None.

    In machine mode the envelope is the sole document on stdout, so this is one
    parse and one member read -- never a scan for JSON inside prose.
    """
    if not isinstance(stdout, str):
        return None
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload


def _unreadable(
    path: Path, detail: str, *, uuid: str = "", group_id: str = ""
) -> ArchiveUnreadable:
    """The error for a success whose payload claudewheel could not use.

    Three things every one of these says, in this order: the archival happened
    (so the profile is not where the reader might assume), whatever handle
    information did survive, and where the record is regardless -- ``saferm
    list``.  A uuid alone is a complete restore, so when there is one it is
    spelled out as the command rather than mentioned as a fact.
    """
    if uuid:
        known = (
            f" The handle it did report is {uuid}, so the profile can still be "
            f"restored: {SAFERM} undelete --no-update-git-index {uuid}."
        )
    elif group_id:
        known = f" The invocation's group id is {group_id}."
    else:
        known = ""
    return ArchiveUnreadable(
        f"{SAFERM} exited 0 archiving {path}, so the profile directory was "
        f"archived and removed, but its --json answer {detail}, so claudewheel "
        f"has no handle to report.{known} Find the record with `{SAFERM} list`."
    )


def _detail(result: Any) -> str:
    """saferm's own stderr, appended to an error when it said anything."""
    text = (getattr(result, "stderr", "") or "").strip()
    return f": {text}" if text else ""
