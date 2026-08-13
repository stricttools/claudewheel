"""Present everything holding a profile, and stop exactly what the user ticks.

Deleting a profile directory does not free it.  Every surviving Claude Code
process still carries ``CLAUDE_CONFIG_DIR`` pointing at the path that was just
removed, and **any** invocation of the client -- including a read-only status
query -- recreates its configuration directory before doing anything else, so
the deleted profile reappears as a husk.  That is why deletion asks first:
rather than promising a completeness it cannot deliver, it shows every live
process registered under the profile and lets the user decide, per row, which
ones to stop.

What is ticked before the user touches anything
-----------------------------------------------

The daemon and its workers, and nothing else.  Those are claudewheel's own
consequence -- the profile's daemon exists because the profile was launched --
while a background job is work someone started deliberately, so it is listed,
never pre-selected.  Nothing is stopped without an explicit tick.

The screen does not close on confirmation: each ticked row's state line goes
from a green ``running`` to a red ``stopped`` as its process really goes, and
escape leaves afterwards.

Stop-then-remove, never the reverse
-----------------------------------

The caller stops holders *before* removing the directory.  Reversing that order
does not merely risk a husk, it guarantees one: the daemon-stop command is
itself an invocation of the client, so run against an already-deleted profile
it recreates the directory it was asked to shut down.  This ordering will look
wrong to a future reader -- "stop the thing, then delete its home" reads like
an optimisation -- so it is stated here and at the call sites.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import processes
from . import session_registry
from .session_list import (
    STYLE_RUNNING,
    STYLE_STOPPED,
    ListRow,
    build_frame,
    move_focus,
    render_frame,
)
from .session_registry import SessionRecord
from .session_rows import SessionIdentity
from .terminal import Terminal
from .theme import ThemeColors
from .ui import screen_session

#: The kinds pre-ticked when the checklist opens: claudewheel's own daemon and
#: the workers it supervises.
DAEMON_KINDS = frozenset({"daemon", "daemon-worker"})

#: The kind stopped through Claude Code's own daemon-stop command.  Only the
#: supervisor -- a worker is a process of its own and gets a signal.
KIND_DAEMON = "daemon"

#: The three states a row's indicator can read.
STATE_RUNNING = "running"
STATE_STOPPING = "stopping..."
STATE_STOPPED = "stopped"

_HINT_SELECT = (
    "up/down: move   space: toggle   enter: stop the ticked ones   esc: cancel"
)
_HINT_DONE = "esc: continue"


@dataclass
class Holder:
    """One live process holding the profile, and what the screen knows about it."""

    record: SessionRecord
    ticked: bool
    state: str = STATE_RUNNING
    state_style: str = STYLE_RUNNING
    rss_kib: int | None = None

    @property
    def row(self) -> ListRow:
        """This holder as a row of the shared list component."""
        return ListRow(
            record=self.record,
            selected=self.ticked,
            state=self.state,
            state_style=self.state_style,
            rss_kib=self.rss_kib,
        )


@dataclass(frozen=True)
class ChecklistOutcome:
    """What the screen decided and what it managed to stop.

    *still_holding* is every holder whose process is really still up when the
    screen closes -- typically the ones left unticked plus any whose stop did
    not take, but it is re-probed rather than subtracted from the snapshot, so
    a holder that exited on its own while the screen was open is not in it.  It
    is what lets the deletion say the directory may come back instead of
    claiming it is gone.
    """

    confirmed: bool
    stopped: tuple[SessionRecord, ...] = ()
    still_holding: tuple[SessionRecord, ...] = ()


def gather_holders(config_dir: Path) -> list[Holder]:
    """Every live process registered under *config_dir*, memory included.

    Resident memory is measured for all of them in one call, so opening the
    screen spawns one ``ps`` rather than one per row.
    """
    records = session_registry.live_records(config_dir)
    memory = processes.resident_memory([r.pid for r in records])
    return [
        Holder(
            record=record,
            ticked=record.kind in DAEMON_KINDS,
            rss_kib=memory.get(record.pid),
        )
        for record in records
    ]


def stop_order(holders: Sequence[Holder]) -> list[Holder]:
    """The ticked holders, supervisor first.

    A worker stopped before its supervisor can simply be put back; stopping the
    supervisor first makes the rest stay stopped.
    """
    ticked = [h for h in holders if h.ticked]
    return [h for h in ticked if h.record.kind == KIND_DAEMON] + [
        h for h in ticked if h.record.kind != KIND_DAEMON
    ]


def still_the_registered_process(record: SessionRecord) -> bool:
    """True when *record*'s pid still names the process that registered it.

    The checklist gathers its holders once and then waits on a human, so every
    pid it holds is a snapshot of unbounded age by the time anything acts on
    it.  The kernel recycles pid numbers freely, so "the pid exists" answers
    the wrong question -- this asks the phantom filter's question, comparing
    the kernel start token the record recorded against the token the pid
    carries now.
    """
    return session_registry.is_live(record.pid, record.proc_start)


@dataclass
class Stopper:
    """Stops one holder by whichever mechanism its kind calls for.

    The daemon-stop command is issued at most once however many daemon rows are
    ticked: it shuts the supervisor down, and a second invocation would only
    re-create the config directory it was pointed at.
    """

    binary: Path
    config_dir: Path
    env: Mapping[str, str]
    _daemon_stopped: bool | None = field(default=None, init=False)

    def stop(self, holder: Holder) -> bool:
        """Stop *holder* and wait for it to go.  True when it really went.

        Identity is re-checked immediately before anything is signalled, and
        again on every poll of the wait: a pid whose start token no longer
        matches belongs to a different process now, so the one the row names is
        already gone.  That counts as stopped -- the profile is not held by it
        -- and nothing at all is signalled, because the signal would land on a
        stranger.
        """
        if not still_the_registered_process(holder.record):
            return True
        if holder.record.kind == KIND_DAEMON:
            if self._daemon_stopped is None:
                self._daemon_stopped = processes.stop_daemon(
                    self.binary, self.config_dir, env=self.env
                )
            if not self._daemon_stopped:
                return False
        elif not processes.terminate(holder.record.pid):
            return False
        proc_start = holder.record.proc_start
        return processes.wait_for_exit(
            holder.record.pid,
            alive=lambda pid: session_registry.is_live(pid, proc_start),
        )


def run_checklist(
    holders: list[Holder],
    *,
    profile_name: str,
    config_dir: Path,
    binary: Path,
    env: Mapping[str, str],
    theme: ThemeColors,
    terminal: Terminal,
    now_ms: int,
    identity: SessionIdentity | None = None,
) -> ChecklistOutcome:
    """Run the checklist over *holders* and return what it decided.

    Two phases in one screen.  While selecting, up/down move, space toggles and
    enter confirms; escape cancels and stops nothing at all.  After confirming,
    the screen stays where it is and each ticked row is stopped in turn, its
    state line redrawn as it goes, and the loop then waits for a key before
    handing the terminal back.
    """
    focus = 0 if holders else -1
    title = f"Processes holding '{profile_name}'"
    hint = _HINT_SELECT

    def render() -> None:
        rows, cols = terminal.get_size()
        frame = build_frame(
            [h.row for h in holders],
            focus=focus,
            now_ms=now_ms,
            title=title,
            hint=hint,
            height=rows,
            width=max(1, cols - 2),
            identity=identity,
            empty_text="Nothing holds this profile.",
        )
        render_frame(terminal, theme, frame)

    with screen_session(terminal, True, render):
        render()
        while True:
            try:
                key = terminal.read_key()
            except KeyboardInterrupt:
                return ChecklistOutcome(confirmed=False)
            if key in ("ESC", "CTRL_C"):
                return ChecklistOutcome(confirmed=False)
            if key == "DOWN":
                focus = move_focus(focus, len(holders), 1)
            elif key == "UP":
                focus = move_focus(focus, len(holders), -1)
            elif key == " " and 0 <= focus < len(holders):
                holders[focus].ticked = not holders[focus].ticked
            elif key == "ENTER":
                break
            render()

        stopper = Stopper(binary=binary, config_dir=config_dir, env=env)
        stopped: list[SessionRecord] = []
        for holder in stop_order(holders):
            holder.state = STATE_STOPPING
            render()
            if stopper.stop(holder):
                holder.state = STATE_STOPPED
                holder.state_style = STYLE_STOPPED
                stopped.append(holder.record)
            else:
                holder.state = STATE_RUNNING
                holder.state_style = STYLE_RUNNING
            render()

        stopped_pids = {r.pid for r in stopped}
        # Re-probed rather than derived from the snapshot: a holder nobody
        # ticked may have exited on its own while the screen was open, and
        # reporting it as still holding would have an interactive one veto the
        # deletion over a process that is no longer there.
        still = tuple(
            h.record
            for h in holders
            if h.record.pid not in stopped_pids
            and still_the_registered_process(h.record)
        )
        hint = _HINT_DONE
        render()
        try:
            terminal.read_key()
        except KeyboardInterrupt:
            pass
        return ChecklistOutcome(
            confirmed=True, stopped=tuple(stopped), still_holding=still
        )
