"""The single authorized surface for effectful calls in claudewheel production code.

Every subprocess launch, filesystem mutation and network call made by
``claudewheel/`` goes through this module.  Nothing else in the package may
call ``subprocess.run``, ``open(path, "w")``, ``Path.write_text``,
``os.makedirs``, ``shutil.rmtree``, ``urllib.request.urlopen`` or their
siblings directly -- ``tests/test_effects_chokepoint.py`` enforces that with an
AST scan and a two-entry exemption list (this module, which holds the
primitives, and ``claudewheel/pty_runner.py``, whose post-``pty.fork`` child
branch is unreachable from the parent process).

Why a chokepoint: claudewheel rides strictcli's ``ctx.effects`` regime, where
every mutation is declared, previewable under ``--dry-run``, and recorded into
the would-do log.  This CLI creates, renames and deletes Claude Code profile
directories, writes OAuth tokens into each profile's own data directory, rewrites
every managed profile's ``settings.json`` to the canonical guardrail model,
deploys hook scripts and downloads and installs release binaries -- a
``--dry-run`` that executed any of that would be worse than no dry run at all.
With every effect funnelled through this one module the regime is adapted in
one file rather than at ~70 call sites.

The mode rule (declared, never inferred)
----------------------------------------

The dispatch context is bound here for the length of a command handler
(``claudewheel.cli._bind`` does it for every registered command), and from then
on:

* **Preview mode** (``--dry-run``; ``ctx.dry_run`` is true) -- every mutating
  operation below is minted on ``ctx.effects``.  It is recorded, never
  executed, and returns strictcli's ``Unsettled`` carrier.  Forwarding that
  carrier into a later effect keeps the preview going; reading a field off it
  truncates the preview with the framework's own error, which is the honest
  outcome when nothing ran.
* **Live mode** -- the operations execute directly, with their full claudewheel
  semantics: per-call timeouts, the ``write_text_atomic`` temp-file +
  ``rename`` that preserves a target's mode, the 0600-from-creation secret
  write, chunked downloads with progress callbacks, and the ``exist_ok`` /
  ``missing_ok`` distinctions call sites branch on.  The contract's closed
  method set expresses none of those, so routing a live run through it would
  silently drop a hang guard or leak a token through a umask-readable temp
  file.

The split is by *mode*, decided before anything runs, and identical on every
invocation -- it is not a fallback: nothing here ever tries the handle, fails,
and retries elsewhere.

Reads are never effects
-----------------------

``read=True`` marks a subprocess run or an HTTP request as a declared read.  A
declared read executes in **every** mode and is never minted, never recorded
and never logged -- the same treatment strictcli gives an allowlisted observe,
and for the same reason: a preview that could not look at the world would have
nothing to preview.

It is declared per call site rather than through an app-level
``proc_observe_allowlist`` because the argv cannot classify these: the
``claude`` binary is both ``--version`` and ``auth login``, and a user hook
script's argv is whatever the user put in their settings.  An allowlist prefix
short enough to cover the reads would be a blanket exemption over the writes --
exactly the breadth hazard strictcli's own ``observe-allowlist-breadth`` check
warns about.  HTTP reads need the same escape for a second reason: the closed
method set's ``http`` is a ``NET_MUTATE``, so a ``GET`` that validates a stored
OAuth token could not be issued at all from the ``read_only``
``profile check-tokens`` (§9.1) if it had to be minted.

Unbound calls -- the library path -- execute directly too.  The TUI event loop,
the wizard's form runner and the test suite call these functions outside any
command dispatch, and there is no handle to mint on there.
``tests/test_effects_binding.py`` asserts that every registered command handler
is bound, so a bound path is never missed by accident.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import strictcli

# The dispatch context of the command currently running, or None outside a
# command dispatch (the TUI, library callers, direct unit-test calls).
_CTX: ContextVar[Any] = ContextVar("claudewheel_effects_ctx", default=None)


@contextmanager
def bound(ctx: Any) -> Iterator[None]:
    """Bind *ctx* as the dispatch context for the length of the block.

    ``claudewheel.cli._bind`` wraps every registered command handler in this,
    which is why no handler carries a decorator of its own: claudewheel already
    funnels every dispatch through one wrapper, and that wrapper is the honest
    place to bind.
    """
    token = _CTX.set(ctx)
    try:
        yield
    finally:
        _CTX.reset(token)


def unsettled(value: Any) -> bool:
    """True when *value* is a carrier standing in for a recorded mutation.

    The one thing a caller may do with a carrier besides forwarding it into a
    later effect: recognize it, and decline to read a result that does not
    exist.  Call sites that would otherwise reach for ``.returncode`` return
    the carrier itself instead, so a preview walks past a mutation whose output
    nobody needed and truncates (honestly) at the first caller that does need
    it.
    """
    return isinstance(value, strictcli.Unsettled)


def previewing() -> bool:
    """True when the current dispatch is previewing rather than executing."""
    return _handle() is not None


def issue(dry_run: bool) -> bool:
    """True when a mutation a caller has already flagged *dry_run* should run.

    Several claudewheel cores (``run_mv``, ``run_import``, ``migrate_sessions``,
    ``run_reconcile``, ``run_stats``) take their own ``dry_run`` parameter and
    narrate a preview far richer than the would-do log -- per-session counts,
    per-file collision reports, per-target guardrail diffs.  Those parameters
    stay, and the CLI passes :func:`previewing` into them, so the user still
    has exactly one switch.

    What this predicate decides is whether the mutation is nevertheless
    *issued*:

    * **Bound dispatch under ``--dry-run``** -- yes.  Issuing it is what fills
      the would-do log; the chokepoint records it and nothing runs.  Skipping it
      would leave a mutating command's log empty, which reads as "this command
      would do nothing" -- the one answer the contract says the framework must
      never give.
    * **Unbound library call with ``dry_run=True``** -- no.  There is no handle
      to record on, so issuing the call would perform it, and a core asked for
      a dry run must not mutate.  ``selfdoc``-style suppression by the handle
      alone cannot cover this path, and a ``dry_run=True`` that writes anyway
      would be the worst footgun in the package.

    Live mode (``dry_run`` false) always issues, in both cases.
    """
    return not dry_run or previewing()


def _handle() -> Any:
    """The strictcli effects handle to mint on, or None to execute directly."""
    ctx = _CTX.get()
    if ctx is None or not getattr(ctx, "dry_run", False):
        return None
    return ctx.effects


def _reject_mock(path: Any) -> None:
    """Refuse a ``unittest.mock`` stand-in handed to a filesystem effect.

    An unspecced ``MagicMock`` answers ``__fspath__`` with a repr like
    ``<MagicMock id=...>/mock.scripts_dir``, so ``os.fspath`` succeeds and the
    effect quietly creates a real file or directory tree named after the mock --
    one such attribute access materialized a directory tree in the repository
    root through :func:`mkdir`.  A mock is never a path, so this is a
    ``TypeError`` at the boundary rather than a write nobody asked for.

    ``unittest.mock`` is looked up in ``sys.modules`` instead of imported: when
    the module was never imported no mock can exist, and production runs never
    pay for the import.
    """
    mock_module = sys.modules.get("unittest.mock")
    if mock_module is None:
        return
    if isinstance(path, mock_module.NonCallableMock):
        raise TypeError(
            f"path operand is a unittest.mock object, not a path: {path!r}. "
            "Pass a real path (a str, an os.PathLike, or a spec'd mock's "
            "return value)."
        )


def _path(path: Any) -> Path:
    """Normalize a path operand for a live filesystem effect."""
    _reject_mock(path)
    return Path(path)


def _p(path: Any) -> str:
    """Render a path operand as text for the handle."""
    _reject_mock(path)
    return str(os.fspath(path))


# ---------------------------------------------------------------------------
# Answering a machine
# ---------------------------------------------------------------------------


def payload(value: Any) -> None:
    """Supply this dispatch's machine payload.

    Forwards to ``ctx.payload``, which is mode-independent by contract: a
    handler calls it on every run and the framework decides whether the value
    becomes a document.  Under ``--json`` it is emitted inside the envelope,
    validated against the command's declared ``payload_schema``; without it the
    value is built and dropped.  A handler therefore never branches on
    ``ctx.json``, and there is no second code path to keep in step with the
    human rendering.

    Unbound -- the TUI, a library caller, a unit test calling a handler
    directly -- there is no envelope to write into and nothing is recorded,
    the same rule the rest of this module follows outside a dispatch.
    """
    ctx = _CTX.get()
    if ctx is None:
        return
    ctx.payload(value)


def info(msg: str) -> None:
    """Write one informational line through the dispatch context.

    Every line a command means for a human goes through here rather than
    ``print``.  Under ``--json`` the context records it as an envelope
    diagnostic and writes nothing, which is what keeps the envelope the sole
    document on stdout; without it the line is printed exactly as ``print``
    would have (and suppressed by ``--quiet``, the framework's own rule for
    informational output).

    Unbound, it prints -- the library path, as everywhere else here.
    """
    ctx = _CTX.get()
    if ctx is None:
        print(msg)
        return
    ctx.info(msg)


# ---------------------------------------------------------------------------
# Process effects
# ---------------------------------------------------------------------------


def run(
    argv: list[str],
    *,
    cwd: Any = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    input: Any = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    read: bool = False,
    resource: str | None = None,
    skip_if_current: str | None = None,
    grant: str | None = None,
) -> Any:
    """Run *argv* to completion and return the ``CompletedProcess``.

    In preview mode a declared read (*read* true) still executes and returns a
    real ``CompletedProcess``; anything else is recorded on ``ctx.effects.run``
    and returns the ``Unsettled`` carrier standing in for the run that did not
    happen.

    Args:
        argv: argument list.
        cwd: working directory for the child process.
        env: complete environment mapping for the child (None inherits).
        timeout: seconds before ``TimeoutExpired`` is raised.
        check: raise ``CalledProcessError`` on a non-zero exit.
        capture_output: capture stdout/stderr instead of inheriting them.
        text: decode captured streams as text.
        input: payload written to the child's stdin.
        stdin: explicit stdin redirection.
        stdout: explicit stdout redirection.
        stderr: explicit stderr redirection.
        read: declare this run an observation -- it changes nothing, so it
            executes in every mode and is never recorded.
        resource: opaque token naming what this run produces (preview only).
        skip_if_current: token the preview annotates the line with, spelling
            out that the handler skips this step when the resource is current.
        grant: name of a grant declared on the running command, whose reason is
            rendered beside the step in the preview.
    """
    h = _handle()
    if h is None or read:
        return _direct_run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            capture_output=capture_output,
            text=text,
            input=input,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    listed = list(argv)
    result = h.run(
        listed,
        cwd=cwd,
        env=env,
        check=False,
        stream=not capture_output,
        resource=resource,
        skip_if_current=skip_if_current,
        grant=grant,
    )
    if unsettled(result):
        # A recorded mutation: nothing ran, so there is no exit code to test.
        return result
    return _completed_from(result, listed, capture_output, text, check)


def exec_replace(
    cwd: str,
    argv: list[str],
    env: dict[str, str],
    *,
    resource: str | None = None,
    grant: str | None = None,
) -> Any:
    """Chdir to *cwd* and replace this process with *argv*.  Does not return.

    ``os.execvpe`` is a process replacement, which the contract's closed method
    set has no member for.  A preview records it as the ``run:`` it effectively
    is -- the child runs to completion and this process never comes back -- and
    then *does* return the carrier, because there is no replacement to perform
    and the caller's dispatch must be allowed to finish rendering the log.
    """
    h = _handle()
    if h is not None:
        return h.run(
            list(argv),
            cwd=cwd,
            check=False,
            stream=True,
            resource=resource,
            grant=grant,
        )
    os.chdir(cwd)
    os.execvpe(argv[0], argv, env)


def kill(pid: int, sig: int) -> None:
    """Send signal *sig* to *pid*.

    The contract's closed method set has no signal, so a preview records the
    ``kill`` that performs it -- the same treatment :func:`symlink` gets, and
    for the same reason: a reader of the would-do log can act on the rendered
    command, where an invented verb would tell them nothing.  ``os.kill``
    raises exactly as it always does (``ProcessLookupError`` for a process that
    is already gone, ``PermissionError`` for one that is not ours), so callers
    keep their existing handling.
    """
    h = _handle()
    if h is not None:
        h.run(["kill", f"-{sig}", str(pid)])
        return
    os.kill(pid, sig)  # effects: exempt -- the live primitive


def run_under_pty(
    argv: list[str],
    env: dict[str, str],
    *,
    input_bytes: bytes | None = None,
    proxy_terminal: bool = True,
    resource: str | None = None,
    grant: str | None = None,
) -> Any:
    """Run *argv* under a fresh PTY; return ``(exit_code, captured_bytes)``.

    In preview mode the run is recorded and the ``Unsettled`` carrier is
    returned in place of the tuple: nothing ran, so there is neither an exit
    code nor captured output, and inventing either would make the preview lie
    about an interactive login the user never performed.
    """
    h = _handle()
    if h is not None:
        return h.run(
            list(argv), check=False, stream=True, resource=resource, grant=grant
        )
    from .pty_runner import run_under_pty as _pty_run

    return _pty_run(argv, env, input_bytes=input_bytes, proxy_terminal=proxy_terminal)


# ---------------------------------------------------------------------------
# Filesystem effects
# ---------------------------------------------------------------------------


class _RecordedWriter(io.StringIO):
    """A file-like sink that mints one ``write`` effect when it is closed.

    :func:`open_write` hands streaming writers (``json.dump``, loops of
    ``f.write``) a real file object in live mode.  The contract has no
    streaming write, so in preview mode the content accumulates here and the
    single resulting ``write`` carries the byte count the file would have had.
    """

    def __init__(self, handle: Any, path: str, binary: bool = False) -> None:
        super().__init__()
        self._handle = handle
        self._path = path
        self._binary = binary

    def close(self) -> None:
        if self.closed:
            return
        content: Any = self.getvalue()
        if self._binary:
            content = content.encode()
        self._handle.write(self._path, content)
        super().close()

    def __enter__(self) -> "_RecordedWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def open_write(path: Any, mode: str = "w", *, encoding: str | None = None) -> Any:
    """Open *path* for writing and return the file object.

    A thin ``open`` wrapper for streaming writers.  Use it as a context
    manager, exactly like ``open``.  Whole-content writers should prefer
    :func:`write_text` / :func:`write_bytes` / :func:`write_text_atomic`.
    """
    if not any(ch in mode for ch in "wax"):
        raise ValueError(f"open_write requires a write mode, got {mode!r}")
    h = _handle()
    if h is None:
        return open(_path(path), mode, encoding=encoding)
    return _RecordedWriter(h, _p(path), binary="b" in mode)


def write_text(path: Any, text: str, *, encoding: str | None = None) -> None:
    """Write *text* to *path*, truncating any existing file."""
    h = _handle()
    if h is None:
        _path(path).write_text(text, encoding=encoding)
        return
    h.write(_p(path), text)


def write_bytes(path: Any, data: Any) -> None:
    """Write *data* to *path*, truncating any existing file."""
    h = _handle()
    if h is None:
        _path(path).write_bytes(data)
        return
    h.write(_p(path), data)


def write(path: Any, content: Any) -> None:
    """Write *content* to *path*, truncating any existing file.

    The one writer that accepts a forwarded ``Unsettled`` carrier as its
    content: it exists so a recorded download can be named as the source of a
    recorded file without anything being transferred.  In live mode *content*
    must be real ``str`` or ``bytes``.
    """
    h = _handle()
    if h is None:
        if isinstance(content, bytes):
            _path(path).write_bytes(content)
        else:
            _path(path).write_text(content)
        return
    h.write(_p(path), content)


def _stage_and_replace(target: Path, text: str, *, secret: bool) -> None:
    """Write *text* through a unique staging file and replace *target* with it.

    The staging file is created by ``tempfile.mkstemp`` in the target's own
    directory (``os.replace`` must not cross a filesystem) with the target's
    FULL name as the prefix.  Both properties are the fix for a measured
    corruption: a staging path derived from the target (``with_suffix(".tmp")``)
    is shared by every concurrent writer of that target -- the loser's commit
    raised ``FileNotFoundError``, or worse, its bytes landed inside the file the
    winner had already published, so a settings or token file could be a splice
    of two writes while both writers reported success.  Keying on the stem also
    made ``config.json`` and ``config.yaml`` collide.

    The mode is set on the staging file before the commit, never on the target
    afterwards: ``mkstemp`` creates 0600, and for a *secret* write that is the
    final mode too, so token bytes are never observable above 0600.  A
    non-secret write adopts the target's existing mode when there is one (the
    replace installs a new inode, so a restrictive mode would otherwise be
    reset on every update) and 0644 for a fresh target.

    ``os.replace`` is atomic for readers -- they see the whole old file or the
    whole new one -- and it is a directory operation, so the write also succeeds
    when *target* itself is read-only.  No fsync: this is concurrency safety,
    not a durability guarantee.
    """
    fd, tmp = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=".tmp"
    )
    try:
        try:
            stream = os.fdopen(fd, "w")
        except BaseException:
            # fdopen did not take ownership of the descriptor, so nothing else
            # will ever close it: the staging file below is unlinked either way.
            os.close(fd)
            raise
        with stream as f:
            f.write(text)
        if secret:
            mode = 0o600
        else:
            try:
                mode = target.stat().st_mode & 0o777
            except FileNotFoundError:
                mode = 0o644  # fresh file
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_text_atomic(path: Any, text: str) -> None:
    """Atomic staged text write that preserves the target's file mode.

    The commit replaces the target inode, so without a chmod any pre-existing
    restrictive mode on the target would be silently reset on every update;
    fresh targets get 0644.  Because the commit is a directory operation the
    write also succeeds when *path* itself is read-only, which is why live mode
    keeps the staging file instead of routing through the contract's plain
    ``write``.  See :func:`_stage_and_replace` for the staging rules.
    """
    target = _path(path)
    h = _handle()
    if h is None:
        _stage_and_replace(target, text, secret=False)
        return
    h.write(_p(target), text)


def write_json_atomic(path: Any, data: Any) -> None:
    """Atomic JSON write (indent=2, trailing newline), preserving file mode."""
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def write_json_atomic_secret(path: Any, data: Any) -> None:
    """Atomic JSON write for secret-holding files: target is always 0600.

    The staging file is created 0600 from the start (``tempfile.mkstemp``'s own
    mode, never umask-readable even transiently) and chmod'd to exactly 0600
    before the commit in case the umask stripped owner bits at creation.  The
    target's existing mode is deliberately NOT adopted: a secrets file that was
    somehow left world-readable is tightened by the next write, never
    preserved.
    """
    target = _path(path)
    text = json.dumps(data, indent=2) + "\n"
    h = _handle()
    if h is None:
        _stage_and_replace(target, text, secret=True)
        return
    h.write(_p(target), text)
    h.chmod(_p(target), 0o600)


def mkdir(path: Any, *, parents: bool = False, exist_ok: bool = False) -> None:
    """Create the directory *path*.

    The defaults mirror ``Path.mkdir`` exactly (missing parents and an existing
    *path* both raise) so translating a call site never changes its behavior.
    The contract's ``mkdir`` always creates parents and never minds an existing
    directory, which is a superset of every shape used here.
    """
    h = _handle()
    if h is None:
        _path(path).mkdir(parents=parents, exist_ok=exist_ok)
        return
    h.mkdir(_p(path))


def remove(path: Any, *, missing_ok: bool = False) -> None:
    """Delete the file or symlink at *path*."""
    h = _handle()
    if h is None:
        _path(path).unlink(missing_ok=missing_ok)
        return
    h.remove(_p(path))


def rmdir(path: Any) -> None:
    """Remove the empty directory at *path*.

    Kept distinct from :func:`rmtree` because the "must be empty" check is a
    safety property at several call sites: a non-empty profile directory means
    the per-child removal above missed something, and that must raise rather
    than take the whole tree with it.
    """
    h = _handle()
    if h is None:
        _path(path).rmdir()
        return
    h.remove(_p(path))


def rmtree(path: Any, *, ignore_errors: bool = False) -> None:
    """Recursively delete the directory tree at *path*."""
    h = _handle()
    if h is None:
        _reject_mock(path)
        shutil.rmtree(path, ignore_errors=ignore_errors)
        return
    h.remove(_p(path))


def rename(src: Any, dst: Any) -> None:
    """Rename *src* to *dst* (``Path.rename`` semantics: no cross-device move).

    ``Path.rename`` rather than ``os.rename`` on purpose: it is one of the
    commit seams the write canary in ``tests/wheelhelpers.py`` patches (the
    atomic writers commit through ``os.replace``, which the canary patches too)
    to prove no test ever renames anything over the real ``~/.claude``.
    """
    h = _handle()
    if h is None:
        _path(src).rename(_path(dst))
        return
    h.rename(_p(src), _p(dst))


def move(src: Any, dst: Any) -> None:
    """Move *src* to *dst*, falling back to copy+delete across devices."""
    h = _handle()
    if h is None:
        _reject_mock(src)
        _reject_mock(dst)
        shutil.move(str(src), str(dst))
        return
    h.rename(_p(src), _p(dst))


def chmod(path: Any, mode: int) -> None:
    """Set the permission bits of *path*."""
    h = _handle()
    if h is None:
        _path(path).chmod(mode)
        return
    h.chmod(_p(path), mode)


def symlink(link: Any, target: Any) -> None:
    """Create *link* as a symbolic link pointing at *target*.

    The contract's closed method set has no symlink, so a preview records the
    ``ln -s`` that performs it -- a faithful rendering of the work, and one a
    reader of the would-do log can act on, rather than an invented verb.
    """
    h = _handle()
    if h is None:
        _reject_mock(target)
        _path(link).symlink_to(target)  # effects: exempt -- the live primitive
        return
    h.run(["ln", "-s", _p(target), _p(link)])


def copy_file(src: Any, dst: Any) -> Any:
    """Copy *src* to *dst*, preserving metadata (``shutil.copy2``)."""
    # Above the mode branch, for the same reason as copytree: in preview mode
    # the source is only read (``Path(src).read_bytes()``), which turns a mock
    # operand into a FileNotFoundError about a repr-named path instead of the
    # boundary's TypeError.
    _reject_mock(src)
    _reject_mock(dst)
    h = _handle()
    if h is None:
        return shutil.copy2(str(src), str(dst))
    # The contract has no copy: reading the source is not an effect, writing
    # the destination is the one that gets recorded.
    h.write(_p(dst), Path(src).read_bytes())
    return dst


def copytree(src: Any, dst: Any, *, dirs_exist_ok: bool = False) -> Any:
    """Recursively copy the directory tree *src* to *dst*."""
    # Above the mode branch, like write_text_atomic: a mock operand is not a
    # path in either mode, and a preview that accepted one would record
    # nothing and hand the mock back as if the copy had been planned.
    _reject_mock(src)
    _reject_mock(dst)
    h = _handle()
    if h is None:
        return shutil.copytree(str(src), str(dst), dirs_exist_ok=dirs_exist_ok)
    # One mkdir plus one write per file, so the preview names every path the
    # copy would create rather than a single opaque "copy tree" line.
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(str(dst), rel)
        h.mkdir(_p(target_dir))
        for name in filenames:
            h.write(
                os.path.join(_p(target_dir), name),
                Path(dirpath, name).read_bytes(),
            )
    return dst


# ---------------------------------------------------------------------------
# Network effects
# ---------------------------------------------------------------------------


def http_read(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    method: str = "GET",
    data: bytes | None = None,
) -> bytes:
    """Perform a declared-read HTTP request and return the response body.

    A declared read executes in every mode, exactly like an allowlisted
    observe: it changes nothing on the far side, and a preview that could not
    validate a token or list the available versions would have nothing to
    preview.  Raises ``urllib.error.HTTPError`` / ``URLError`` exactly as
    ``urlopen`` does, so callers keep their existing error handling.
    """
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body: bytes = resp.read()
    return body


def http_status(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    method: str = "GET",
) -> int:
    """Perform a declared-read HTTP request and return its status code.

    The probe shape: callers that only need "did the far side accept this
    credential" get the number without a body.  ``urllib``'s
    ``HTTPError`` still propagates for a non-2xx response, exactly as it does
    from ``urlopen``, so callers keep their existing 401 handling.
    """
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return int(resp.status)


def http_stream(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> Any:
    """Open a declared-read HTTP GET and return the response for chunked reads.

    The streaming shape, for payloads too large to hold in memory: the caller
    reads the response in chunks and writes them through :func:`open_write`.
    Live mode only -- a preview never opens the stream (see
    :func:`install_version`), because there is nothing to stream into.
    """
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    resource: str | None = None,
    grant: str | None = None,
) -> Any:
    """Perform a recorded HTTP request, or record it in preview mode.

    Live mode returns the response body as ``bytes``.  Preview mode records a
    ``net:`` line and returns the ``Unsettled`` carrier, which may be forwarded
    into a later ``write`` so the preview names the file the download would
    land in without transferring it.
    """
    h = _handle()
    if h is None:
        return http_read(url, headers=headers, timeout=timeout, method=method)
    return h.http(method, url, headers=headers, resource=resource, grant=grant)


# ---------------------------------------------------------------------------
# Live-mode primitives
# ---------------------------------------------------------------------------


def _direct_run(
    argv: list[str],
    *,
    cwd: Any,
    env: dict[str, str] | None,
    timeout: float | None,
    check: bool,
    capture_output: bool,
    text: bool,
    input: Any,
    stdin: Any,
    stdout: Any,
    stderr: Any,
) -> Any:
    """Execute *argv* with the full subprocess semantics claudewheel relies on."""
    kwargs: dict[str, Any] = {}
    if capture_output:
        kwargs["capture_output"] = True
    else:
        if stdout is not None:
            kwargs["stdout"] = stdout
        if stderr is not None:
            kwargs["stderr"] = stderr
    if stdin is not None:
        kwargs["stdin"] = stdin
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=check,
        text=text,
        input=input,
        **kwargs,
    )


def _completed_from(
    result: Any, argv: list[str], capture_output: bool, text: bool, check: bool
) -> Any:
    """Adapt a settled strictcli ``Completed`` to ``CompletedProcess``."""
    out, err = result.stdout, result.stderr
    if not capture_output:
        out = err = None
    elif not text:
        out, err = out.encode(), err.encode()
    if check and result.exit_code != 0:
        raise subprocess.CalledProcessError(result.exit_code, argv, out, err)
    return subprocess.CompletedProcess(argv, result.exit_code, out, err)
