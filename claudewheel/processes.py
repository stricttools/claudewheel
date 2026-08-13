"""Measure and stop the processes holding a profile.

The deletion checklist shows every live process registered under a profile and
stops the ones the user ticks.  Both halves of that need the world -- reading
resident memory, running Claude Code's own daemon-stop command, sending a
signal -- so both halves live here, behind :mod:`claudewheel.effects`.

Resident memory
---------------

``ps -o pid=,rss=`` is the portable answer: it reports KiB on Linux and on
macOS, needs no third-party package, and one invocation can measure every pid
at once.  Three rules the parser keeps:

* **One call for all pids.**  A per-row call would be one process spawn per
  session block, on every refresh.
* **``pid=`` is always requested.**  ps does not answer in argument order, so a
  row is only interpretable next to the pid it belongs to.
* **A missing row means the process is gone.**  ps simply omits a pid it cannot
  find, so the pid is absent from the result rather than present with a zero.

Resident memory is never summed across a process tree: shared pages would be
counted once per member, and the total would exceed the machine's memory.

Stopping
--------

Two mechanisms, chosen by the record's ``kind`` rather than by trying one and
falling back to the other:

* **The daemon** is stopped through Claude Code's own
  ``claude daemon stop --any --keep-workers``.  The daemon is addressed *per
  config directory*: the client derives its runtime socket directory from a
  hash of the resolved config dir (``/tmp/cc-daemon-<uid>/<hash>``), so running
  the command with ``CLAUDE_CONFIG_DIR`` pointing at a profile stops that
  profile's daemon and no other.  ``--keep-workers`` is always passed, so a
  detached session the user did NOT tick is never taken down as a side effect
  of stopping the supervisor -- the checklist stops exactly what was ticked.
* **Everything else** -- a daemon worker, a background job, an interactive
  session -- gets a ``SIGTERM`` to its own pid.

Both go through the effects seam, so a ``--dry-run`` deletion records the stops
it would perform instead of performing them.
"""

from __future__ import annotations

import signal as signal_module
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from . import effects
from .session_registry import pid_exists

#: The ps output format.  ``pid=`` first because row order is not argument
#: order; the trailing ``=`` on each field suppresses the header line.
RSS_FORMAT = "pid=,rss="

#: Seconds before a measurement or a stop command is given up on.
PROBE_TIMEOUT_S = 5.0

#: How long a signalled process is waited on before it is reported as still up.
EXIT_TIMEOUT_S = 5.0

#: How often the exit poll asks.
EXIT_POLL_S = 0.1

#: The one liveness answer in the package -- the registry's probe, not a second
#: implementation of it.
alive = pid_exists


def resident_memory(pids: Iterable[int]) -> dict[int, int]:
    """Resident set size in KiB for each of *pids*, measured in one ``ps`` call.

    A pid ps did not report is absent from the mapping: the process is gone (or
    was never ours), and inventing a zero for it would draw a live row with no
    memory.  A platform with no ``ps``, an unparseable row and a preview that
    recorded the call instead of running it all yield the same thing -- no
    measurement for that pid, so the caller simply draws no memory clause.
    """
    wanted = [int(p) for p in pids]
    if not wanted:
        return {}
    argv = ["ps", "-o", RSS_FORMAT, "-p", ",".join(str(p) for p in wanted)]
    try:
        result = effects.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            read=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if effects.unsettled(result):
        return {}
    # ps exits non-zero when it matched no pid at all; that is an empty answer,
    # not an error, so the exit code is deliberately not tested.
    measured: dict[int, int] = {}
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        raw_pid, raw_rss = fields
        if not raw_pid.isdigit() or not raw_rss.isdigit():
            continue
        measured[int(raw_pid)] = int(raw_rss)
    return measured


def stop_daemon(binary: Path, config_dir: Path, *, env: Mapping[str, str]) -> bool:
    """Stop the Claude Code daemon owning *config_dir*.  True when it worked.

    *env* is the environment the command inherits with ``CLAUDE_CONFIG_DIR``
    forced to *config_dir* -- that variable is the whole addressing scheme (see
    the module docstring), so it is set here rather than left to the caller's
    ambient environment.
    """
    child_env = dict(env)
    child_env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    argv = [str(binary), "daemon", "stop", "--any", "--keep-workers"]
    try:
        result = effects.run(
            argv,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if effects.unsettled(result):
        # A preview recorded the stop; nothing ran, so nothing was stopped.
        return False
    return bool(result.returncode == 0)


def terminate(pid: int) -> bool:
    """Send ``SIGTERM`` to *pid*.  True when the signal was delivered or moot.

    A process that has already exited is not a failure: the checklist's goal is
    "this no longer holds the profile", and a dead process meets it.  A signal
    we are not allowed to send is a failure, because the process really is
    still there.
    """
    try:
        effects.kill(pid, signal_module.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def wait_for_exit(
    pid: int,
    *,
    timeout_s: float = EXIT_TIMEOUT_S,
    poll_s: float = EXIT_POLL_S,
    alive: Callable[[int], bool] = alive,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until *pid* is gone.  True when it went, False on timeout.

    The clock, the sleep and the liveness probe are all parameters so the poll
    is exercisable without a real process and without real time passing.
    """
    deadline = now() + timeout_s
    while True:
        if not alive(pid):
            return True
        if now() >= deadline:
            return False
        sleep(poll_s)
