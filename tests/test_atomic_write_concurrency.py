"""Concurrency regression tests for the atomic writers in claudewheel.effects.

Two claudewheel processes can write one file at the same time -- two sessions
launching at once, a launch and a ``reconcile-permissions`` run, a hook and the
TUI -- and every settings/state/token write goes through the atomic writers.

The writers used to stage through one FIXED path per target
(``target.with_suffix(".tmp")``), so two concurrent writers shared a single
staging file.  Three measured consequences, all reproduced by the tests below:

* the writer that loses the race gets ``FileNotFoundError`` from its own
  commit, because the winner already renamed the shared staging file away;
* a writer returns success while the published file holds the *other* writer's
  bytes;
* with different payload sizes the loser's write lands INSIDE the file the
  winner already published, so the target holds a splice of both payloads --
  invalid JSON in a settings or token file -- while both writers report
  success.

``with_suffix`` also keys the staging name on the target's STEM, so
``config.json`` and ``config.yaml`` in one directory shared a staging file even
though they are different targets.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from claudewheel.effects import write_text_atomic

# A few hundred synchronized rounds: enough that the race resolves both ways
# many times over (the defect reproduced within ~10 rounds), fast enough that
# the suite does not notice.
ROUNDS = 300

# Distinct sizes on purpose: equal-length payloads can only ever produce one of
# the two payloads even when the write is spliced, which hides the corruption.
PAYLOAD_BIG = "A" * 1024
PAYLOAD_SMALL = "B" * 256

# Seconds any single barrier rendezvous may take before the run is declared
# stuck.  A wedged child then raises instead of hanging the suite.
BARRIER_TIMEOUT = 60.0


def _race_writer(target: str, payload: str, barrier: Any, report: str) -> None:
    """One writer of the two-process race; writes its errors to *report*.

    Runs in a forked child, so nothing is asserted here: every exception the
    writer raises is collected and handed back to the parent as JSON.
    """
    errors: list[str] = []
    for _ in range(ROUNDS):
        barrier.wait()
        try:
            write_text_atomic(target, payload)
        except BaseException as exc:  # noqa: BLE001 -- reported, not handled
            errors.append(f"{type(exc).__name__}: {exc}")
        barrier.wait()
    Path(report).write_text(json.dumps(errors))


def _collision_writer(target: str, payload: str, barrier: Any, report: str) -> None:
    """Writer for the stem-collision race: two different targets, one dir."""
    errors: list[str] = []
    for _ in range(ROUNDS):
        barrier.wait()
        try:
            write_text_atomic(target, payload)
        except BaseException as exc:  # noqa: BLE001 -- reported, not handled
            errors.append(f"{type(exc).__name__}: {exc}")
        barrier.wait()
        try:
            got = Path(target).read_text()
        except BaseException as exc:  # noqa: BLE001 -- reported, not handled
            errors.append(f"read {type(exc).__name__}: {exc}")
            continue
        if got != payload:
            errors.append(f"read back {len(got)} bytes: {got[:40]!r}")
    Path(report).write_text(json.dumps(errors))


class AtomicWriteRaceTests(unittest.TestCase):
    """Two concurrent writers must never publish anything but one payload."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        # fork: the children inherit the target directory and the barrier with
        # no pickling and no re-import of the test module.
        self.ctx = multiprocessing.get_context("fork")

    def _reports(self, *names: str) -> list[str]:
        """Collect the errors every child process reported, flattened."""
        collected: list[str] = []
        for name in names:
            path = self.tmp_path / name
            if not path.exists():
                collected.append(f"{name}: child left no report (it died)")
                continue
            collected.extend(f"{name}: {e}" for e in json.loads(path.read_text()))
        return collected

    def _run(self, procs: list[Any]) -> None:
        """Start *procs*, join them, and fail on a non-zero exit."""
        for p in procs:
            p.start()
        try:
            for p in procs:
                p.join(timeout=BARRIER_TIMEOUT * 2)
        finally:
            for p in procs:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
        for p in procs:
            self.assertEqual(p.exitcode, 0, f"child {p.name} exited {p.exitcode}")

    def test_two_processes_never_publish_a_spliced_file(self) -> None:
        target = self.tmp_path / "settings.json"
        barrier = self.ctx.Barrier(3, timeout=BARRIER_TIMEOUT)
        procs = [
            self.ctx.Process(
                target=_race_writer,
                args=(str(target), PAYLOAD_BIG, barrier, str(self.tmp_path / "a.json")),
                name="big",
            ),
            self.ctx.Process(
                target=_race_writer,
                args=(
                    str(target),
                    PAYLOAD_SMALL,
                    barrier,
                    str(self.tmp_path / "b.json"),
                ),
                name="small",
            ),
        ]
        for p in procs:
            p.start()
        bad: list[str] = []
        try:
            for round_no in range(ROUNDS):
                barrier.wait(timeout=BARRIER_TIMEOUT)
                # Both writers are racing right now: sample the published file
                # a few times, since a reader must only ever see a whole
                # payload, old or new.
                for _ in range(3):
                    try:
                        seen = target.read_text()
                    except FileNotFoundError:
                        continue  # nothing published yet, only before round 1
                    if seen not in (PAYLOAD_BIG, PAYLOAD_SMALL):
                        bad.append(f"round {round_no} mid: {len(seen)} bytes")
                barrier.wait(timeout=BARRIER_TIMEOUT)
                settled = target.read_text()
                if settled not in (PAYLOAD_BIG, PAYLOAD_SMALL):
                    bad.append(
                        f"round {round_no} settled: {len(settled)} bytes "
                        f"{settled[:40]!r}"
                    )
        finally:
            for p in procs:
                p.join(timeout=BARRIER_TIMEOUT)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
        self.assertEqual(bad[:5], [], f"{len(bad)} corrupt observations")
        self.assertEqual(self._reports("a.json", "b.json")[:5], [])
        self.assertEqual([p.exitcode for p in procs], [0, 0])

    def test_same_stem_different_suffix_targets_do_not_collide(self) -> None:
        # config.json and config.yaml share a stem, so the old staging name
        # (with_suffix(".tmp")) was the same file for both.
        barrier = self.ctx.Barrier(2, timeout=BARRIER_TIMEOUT)
        procs = [
            self.ctx.Process(
                target=_collision_writer,
                args=(
                    str(self.tmp_path / "config.json"),
                    PAYLOAD_BIG,
                    barrier,
                    str(self.tmp_path / "json-report"),
                ),
                name="json",
            ),
            self.ctx.Process(
                target=_collision_writer,
                args=(
                    str(self.tmp_path / "config.yaml"),
                    PAYLOAD_SMALL,
                    barrier,
                    str(self.tmp_path / "yaml-report"),
                ),
                name="yaml",
            ),
        ]
        self._run(procs)
        self.assertEqual(self._reports("json-report", "yaml-report")[:5], [])
        self.assertEqual((self.tmp_path / "config.json").read_text(), PAYLOAD_BIG)
        self.assertEqual((self.tmp_path / "config.yaml").read_text(), PAYLOAD_SMALL)

    def test_race_leaves_no_staging_files_behind(self) -> None:
        target = self.tmp_path / "state.json"
        barrier = self.ctx.Barrier(2, timeout=BARRIER_TIMEOUT)
        procs = [
            self.ctx.Process(
                target=_race_writer,
                args=(str(target), PAYLOAD_BIG, barrier, str(self.tmp_path / "a.json")),
                name="big",
            ),
            self.ctx.Process(
                target=_race_writer,
                args=(
                    str(target),
                    PAYLOAD_SMALL,
                    barrier,
                    str(self.tmp_path / "b.json"),
                ),
                name="small",
            ),
        ]
        self._run(procs)
        leftovers = sorted(
            p.name
            for p in self.tmp_path.iterdir()
            if p.name not in {"state.json", "a.json", "b.json"}
        )
        self.assertEqual(leftovers, [])
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
