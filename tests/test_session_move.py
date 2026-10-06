"""``claudewheel move-session``: move one session to another directory's store.

Every test builds the layout a real machine has (``SymlinkedStoreLayout``:
managed profiles whose ``projects`` are symlinks into the shared store, and a
``default`` profile with a ``projects`` of its own) and drives
:func:`claudewheel.session_move.move_session` or the CLI against it.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import cli, effects, lifecycle
from claudewheel.lifecycle import MovedEvent, StartedEvent
from claudewheel.mv import _dump_record
from claudewheel.session import store_dir_path
from claudewheel.session_move import MoveSessionError, journal_path, move_session
from claudewheel.shared_store import SharedStore
from tests.wheelhelpers import SymlinkedStoreLayout, live_record, stale_record

SESSION = "5e55a0e1-0000-4000-8000-00000000c0de"
OTHER = "0be7a0e1-1111-4111-8111-111111111111"

# Times well inside a day, the access time after the modification time, so a
# read under relatime leaves the access time alone.
ATIME_NS = time.time_ns() - 3_600_000_000_123
MTIME_NS = time.time_ns() - 7_200_000_000_456


def _relocated(session: str, cwd: str) -> str:
    return json.dumps(
        {"type": "relocated", "sessionId": session, "relocatedCwd": cwd},
        separators=(",", ":"),
        ensure_ascii=False,
    )


class MoveCase(unittest.TestCase):
    """A machine layout with two project directories and one session to move."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.layout = SymlinkedStoreLayout(self.root)
        self.ws = self.layout.ws
        self.alpha = self.layout.projects_root / "alpha"
        self.beta = self.layout.projects_root / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        # The real store root every managed profile's projects/ resolves to.
        self.shared_projects = self.layout.store.resolve()
        self.default_projects = (self.layout.claude_dir / "projects").resolve()
        self._quiet = redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.addCleanup(self._quiet.__exit__, None, None, None)

    # -- fixture writers ----------------------------------------------------

    def seed(
        self,
        projects: Path | None = None,
        cwd: Path | None = None,
        session: str = SESSION,
        *,
        folder: bool = True,
    ) -> Path:
        """Write one session's transcript (and folder) and return its store dir."""
        projects = self.shared_projects if projects is None else projects
        cwd = self.alpha if cwd is None else cwd
        store = projects / SharedStore.encode_path(str(cwd))
        store.mkdir(parents=True, exist_ok=True)
        name = store.name
        # The self-path spelled through a managed profile's projects symlink,
        # as Claude Code writes it when it runs under that profile.
        via_profile = self.ws.profiles_dir / "work" / "projects"
        self.lines: list[Any] = [
            {"type": "user", "cwd": str(cwd), "sessionId": session, "n": 1},
            {
                "type": "user",
                "cwd": str(cwd),
                "toolUseResult": {
                    "persistedOutputPath": f"{projects}/{name}/{session}/tool-results/out.txt"
                },
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "Output too large. Full output saved to: "
                            f"{via_profile}/{name}/{session}/tool-results/out.txt",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "cwd": str(cwd),
                "message": {
                    "content": f"the transcript is {projects}/{name}/{session}.jsonl; "
                    f"not {projects}/{name}/{OTHER}/tool-results/x.txt, "
                    f"not {projects}/{name}-sibling/{session}/x, "
                    f"and not {cwd}/README.md"
                },
            },
            {"type": "user", "cwd": str(cwd), "message": {"content": "héllo ✓"}},
        ]
        transcript = store / f"{session}.jsonl"
        transcript.write_text("".join(json.dumps(r) + "\n" for r in self.lines))
        if folder:
            results = store / session / "tool-results"
            results.mkdir(parents=True)
            (results / "out.txt").write_text("output\n")
            agents = store / session / "subagents"
            agents.mkdir()
            (agents / "agent-a.jsonl").write_text(
                json.dumps(
                    {
                        "cwd": str(cwd),
                        "path": f"{projects}/{name}/{session}/tool-results/out.txt",
                    }
                )
                + "\n"
            )
        os.utime(transcript, ns=(ATIME_NS, MTIME_NS))
        return store

    def expected_transcript(
        self, old_store: Path, new_store: Path, target: Path, session: str = SESSION
    ) -> str:
        """The transcript a move must leave: self-paths moved, the record appended."""
        pattern = re.compile(
            "/projects/"
            + re.escape(old_store.name)
            + "/"
            + re.escape(session)
            + r"(?![\w-])"
        )
        replacement = f"/projects/{new_store.name}/{session}"

        def fix(value: Any, key: str) -> Any:
            if isinstance(value, str):
                return value if key == "cwd" else pattern.sub(replacement, value)
            if isinstance(value, list):
                return [fix(v, key) for v in value]
            if isinstance(value, dict):
                return {
                    pattern.sub(replacement, k): fix(v, k) for k, v in value.items()
                }
            return value

        out = []
        for record in self.lines:
            fixed = fix(record, "")
            out.append(json.dumps(record) if fixed == record else _dump_record(fixed))
        return (
            "".join(line + "\n" for line in out)
            + _relocated(session, str(target))
            + "\n"
        )

    def run_move(self, session: str = SESSION, target: Path | str | None = None) -> Any:
        return move_session(
            self.ws,
            session,
            str(self.beta if target is None else target),
            dry_run=False,
        )

    def refusal(self, session: str = SESSION, target: Path | str | None = None) -> str:
        with self.assertRaises(MoveSessionError) as ctx:
            self.run_move(session, target)
        return str(ctx.exception)

    def moved_events(self, session: str = SESSION) -> list[MovedEvent]:
        path = lifecycle.session_file(self.ws.shared.lifecycle_dir, session)
        return [e for e in lifecycle.read_session(path) if isinstance(e, MovedEvent)]


class MoveTests(MoveCase):
    def test_moves_the_session_into_the_target_store(self) -> None:
        source = self.seed()
        subagent_before = (
            source / SESSION / "subagents" / "agent-a.jsonl"
        ).read_bytes()
        self.run_move()

        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        transcript = target / f"{SESSION}.jsonl"
        # Stat before any read: reading after a utime updates the access time.
        st = os.stat(transcript)
        self.assertEqual((st.st_atime_ns, st.st_mtime_ns), (ATIME_NS, MTIME_NS))
        self.assertEqual(
            transcript.read_text(), self.expected_transcript(source, target, self.beta)
        )
        # The relocated record is the last line, word for word as Claude Code writes it.
        self.assertEqual(
            transcript.read_text().splitlines()[-1],
            f'{{"type":"relocated","sessionId":"{SESSION}","relocatedCwd":"{self.beta}"}}',
        )
        self.assertEqual(
            (target / SESSION / "tool-results" / "out.txt").read_text(), "output\n"
        )
        # Subagent transcripts are left exactly as they were.
        self.assertEqual(
            (target / SESSION / "subagents" / "agent-a.jsonl").read_bytes(),
            subagent_before,
        )
        # The emptied source store dir is gone, and no journal is left.
        self.assertFalse(source.exists())
        self.assertFalse(journal_path(self.ws, SESSION).exists())
        # claudewheel's own reader places the moved session in its new directory.
        self.assertEqual(store_dir_path(target), str(self.beta))

    def test_unchanged_lines_are_kept_byte_for_byte(self) -> None:
        self.seed()
        self.run_move()
        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        lines = (target / f"{SESSION}.jsonl").read_text().splitlines()
        self.assertEqual(lines[0], json.dumps(self.lines[0]))
        self.assertEqual(lines[3], json.dumps(self.lines[3]))

    def test_records_a_moved_lifecycle_event(self) -> None:
        source = self.seed()
        self.run_move()
        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        [event] = self.moved_events()
        self.assertEqual(event.source, "user")
        self.assertEqual(event.old_cwd, str(self.alpha))
        self.assertEqual(event.new_cwd, str(self.beta))
        self.assertEqual(event.old_transcript, str(source / f"{SESSION}.jsonl"))
        self.assertEqual(event.new_transcript, str(target / f"{SESSION}.jsonl"))
        summary = lifecycle.summarize(
            lifecycle.read_session(
                lifecycle.session_file(self.ws.shared.lifecycle_dir, SESSION)
            ),
            session=SESSION,
        )
        self.assertEqual(summary.cwd, str(self.beta))

    def test_old_cwd_is_the_last_relocated_record_when_there_is_one(self) -> None:
        source = self.seed()
        with (source / f"{SESSION}.jsonl").open("a") as fh:
            fh.write(_relocated(SESSION, str(self.alpha)) + "\n")
        self.lines.append(json.loads(_relocated(SESSION, str(self.alpha))))
        self.run_move()
        [event] = self.moved_events()
        self.assertEqual(event.old_cwd, str(self.alpha))

    def test_a_session_without_a_folder_moves_its_transcript(self) -> None:
        source = self.seed(folder=False)
        self.run_move()
        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        self.assertTrue((target / f"{SESSION}.jsonl").is_file())
        self.assertFalse((target / SESSION).exists())
        self.assertFalse(source.exists())

    def test_the_source_store_dir_stays_while_it_holds_anything(self) -> None:
        source = self.seed()
        (source / "memory").mkdir()
        (source / "memory" / "MEMORY.md").write_text("notes\n")
        self.run_move()
        self.assertEqual(sorted(p.name for p in source.iterdir()), ["memory"])

    def test_into_an_existing_target_store_dir(self) -> None:
        self.seed()
        other_store = self.seed(cwd=self.beta, session=OTHER)
        self.run_move()
        self.assertEqual(
            sorted(p.name for p in other_store.iterdir()),
            sorted([f"{OTHER}.jsonl", OTHER, f"{SESSION}.jsonl", SESSION]),
        )

    def test_the_default_profile_store_moves_within_itself(self) -> None:
        """A session in ~/.claude/projects moves to ~/.claude/projects, not the shared store."""
        source = self.seed(self.default_projects)
        self.run_move()
        target = self.default_projects / SharedStore.encode_path(str(self.beta))
        self.assertTrue((target / f"{SESSION}.jsonl").is_file())
        self.assertTrue((target / SESSION).is_dir())
        self.assertFalse(source.exists())
        self.assertFalse(
            (self.shared_projects / SharedStore.encode_path(str(self.beta))).exists()
        )
        self.assertEqual(
            (target / f"{SESSION}.jsonl").read_text(),
            self.expected_transcript(source, target, self.beta),
        )

    def test_a_relative_target_resolves_against_the_cwd(self) -> None:
        self.seed()
        cwd = os.getcwd()
        os.chdir(self.layout.projects_root)
        self.addCleanup(os.chdir, cwd)
        self.run_move(target="beta")
        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        self.assertTrue((target / f"{SESSION}.jsonl").is_file())


class RefusalTests(MoveCase):
    """Every refusal happens before anything changes, and names what it found."""

    def assertUnchanged(self, before: dict[str, bytes]) -> None:
        self.assertEqual(self.layout.snapshot(), before)
        self.assertFalse(journal_path(self.ws, SESSION).exists())

    def test_a_session_id_must_be_a_full_lowercase_uuid(self) -> None:
        self.seed()
        before = self.layout.snapshot()
        for value in (SESSION.upper(), SESSION[:8], SESSION[2:30], f" {SESSION}"):
            with self.subTest(value=value):
                msg = self.refusal(session=value)
                self.assertIn(f"{value!r} is not a session id", msg)
                self.assertIn("lowercase", msg)
        self.assertUnchanged(before)

    def test_no_store_holds_the_session(self) -> None:
        msg = self.refusal()
        self.assertIn(f"no session store holds {SESSION}", msg)
        self.assertIn(str(self.shared_projects), msg)
        self.assertIn(str(self.default_projects), msg)

    def test_more_than_one_store_dir_holds_the_session(self) -> None:
        a = self.seed()
        b = self.seed(self.default_projects)
        before = self.layout.snapshot()
        msg = self.refusal()
        self.assertIn("more than one session store dir holds", msg)
        self.assertIn(f"{a}: {SESSION}.jsonl, {SESSION}/", msg)
        self.assertIn(f"{b}: {SESSION}.jsonl, {SESSION}/", msg)
        self.assertUnchanged(before)

    def test_the_store_dir_holding_the_folder_lacks_the_transcript(self) -> None:
        store = self.seed()
        (store / f"{SESSION}.jsonl").unlink()
        msg = self.refusal()
        self.assertIn(f"{store} holds {SESSION}/ but not {SESSION}.jsonl", msg)

    def test_the_target_must_be_an_existing_directory(self) -> None:
        self.seed()
        missing = self.layout.projects_root / "gamma"
        msg = self.refusal(target=missing)
        self.assertIn(f"not an existing directory: {missing}", msg)
        a_file = self.layout.projects_root / "file.txt"
        a_file.write_text("x")
        msg = self.refusal(target=a_file)
        self.assertIn(f"not an existing directory: {a_file}", msg)

    def test_the_target_store_dir_must_differ_from_the_source(self) -> None:
        source = self.seed()
        before = self.layout.snapshot()
        msg = self.refusal(target=self.alpha)
        self.assertIn(f"already in {source}", msg)
        # A different directory whose store dir name is the same.
        twin = self.layout.projects_root / "al.pha"
        (self.layout.projects_root / "al-pha").mkdir()
        twin.mkdir()
        twin_source = self.seed(cwd=self.layout.projects_root / "al-pha", session=OTHER)
        msg = self.refusal(session=OTHER, target=twin)
        self.assertIn(f"already in {twin_source}", msg)
        self.assertIn(str(twin), msg)
        self.assertEqual(
            {k: v for k, v in self.layout.snapshot().items() if OTHER not in k}, before
        )

    def test_the_target_store_dir_already_holds_the_session(self) -> None:
        self.seed()
        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        (target / SESSION).mkdir(parents=True)
        msg = self.refusal()
        self.assertIn("more than one session store dir holds", msg)
        self.assertIn(f"the target's store dir {target} already holds {SESSION}/", msg)

    def test_a_running_session_is_refused_until_it_is_not(self) -> None:
        self.seed()
        sessions = self.ws.profiles_dir / "work" / "sessions"
        record = live_record(sessions, extra={"sessionId": SESSION})
        before = self.layout.snapshot()
        msg = self.refusal()
        self.assertIn(f"session {SESSION} is running", msg)
        self.assertIn(f"pid {os.getpid()}", msg)
        self.assertIn("profile work", msg)
        self.assertUnchanged(before)
        # The process ends: its registry file is left behind, dead.
        record.unlink()
        stale_record(sessions, extra={"sessionId": SESSION})
        self.run_move()
        self.assertEqual(len(self.moved_events()), 1)

    def test_a_running_session_under_the_default_profile_is_refused(self) -> None:
        self.seed()
        live_record(self.layout.claude_dir / "sessions", extra={"sessionId": SESSION})
        self.assertIn("profile default", self.refusal())

    def test_a_starting_session_is_refused(self) -> None:
        self.seed()
        lifecycle.append_event(
            self.ws.shared.lifecycle_dir,
            StartedEvent(
                session=SESSION,
                source="hook",
                cwd=str(self.alpha),
                config_dir=str(self.ws.profiles_dir / "work"),
                profile="work",
                claude_version=None,
                model=None,
                permissions=None,
                entry="startup",
                transcript=None,
                pid=None,
            ),
        )
        msg = self.refusal()
        self.assertIn(f"session {SESSION} is starting", msg)

    def test_a_background_job_naming_the_session_is_refused(self) -> None:
        self.seed()
        for profile_dir, field, value in (
            (self.ws.profiles_dir / "hn", "sessionId", SESSION),
            (self.ws.profiles_dir / "work", "resumeSessionId", SESSION),
            (
                self.layout.claude_dir,
                "linkScanPath",
                f"{self.ws.profiles_dir}/work/projects/x/{SESSION}.jsonl",
            ),
        ):
            with self.subTest(field=field):
                job = profile_dir / "jobs" / "54b6cf61"
                job.mkdir(parents=True)
                state = job / "state.json"
                state.write_text(json.dumps({"name": "nightly", field: value}))
                msg = self.refusal()
                self.assertIn(
                    f"background job {state} (nightly) refers to session {SESSION}", msg
                )
                state.unlink()
                job.rmdir()
                (profile_dir / "jobs").rmdir()

    def test_an_unrelated_background_job_does_not_block(self) -> None:
        self.seed()
        job = self.ws.profiles_dir / "hn" / "jobs" / "aa"
        job.mkdir(parents=True)
        (job / "state.json").write_text(
            json.dumps({"sessionId": OTHER, "cwd": str(self.alpha)})
        )
        self.run_move()

    def test_an_unreadable_job_record_is_an_error_naming_it(self) -> None:
        self.seed()
        job = self.ws.profiles_dir / "hn" / "jobs" / "aa"
        job.mkdir(parents=True)
        (job / "state.json").write_text("{not json")
        msg = self.refusal()
        self.assertIn(str(job / "state.json"), msg)

    def test_symlinks_into_the_session_folder_are_refused_naming_each(self) -> None:
        source = self.seed()
        other_store = self.seed(cwd=self.beta, session=OTHER)
        tasks = other_store / OTHER / "tasks"
        tasks.mkdir()
        absolute = tasks / "a.output"
        # Spelled through a profile's projects symlink: still this session's folder.
        absolute.symlink_to(
            self.ws.profiles_dir
            / "work"
            / "projects"
            / source.name
            / SESSION
            / "tool-results"
            / "out.txt"
        )
        relative = tasks / "b.output"
        relative.symlink_to(Path("..") / ".." / ".." / source.name / SESSION)
        unrelated = tasks / "c.output"
        unrelated.symlink_to(other_store / OTHER / "tool-results")
        before = self.layout.snapshot()
        msg = self.refusal()
        self.assertIn(f"{absolute} -> ", msg)
        self.assertIn(f"{relative} -> ", msg)
        self.assertNotIn(str(unrelated), msg)
        self.assertUnchanged(before)


# ---------------------------------------------------------------------------
# Interrupted moves
# ---------------------------------------------------------------------------

# The mutating effects a move issues, directly or through lifecycle.append_event.
_FAULTABLE = (
    "mkdir",
    "write_text_atomic",
    "rename",
    "set_times",
    "open_write",
    "rmdir",
    "remove",
)


class _Injected(OSError):
    pass


@contextmanager
def _fault_at(call: int) -> Iterator[list[str]]:
    """Fail the *call*-th mutating effect (1-based); yield the names of those issued."""
    issued: list[str] = []
    originals = {name: getattr(effects, name) for name in _FAULTABLE}

    def wrap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def effect(*args: Any, **kwargs: Any) -> Any:
            issued.append(name)
            if len(issued) == call:
                raise _Injected(f"injected failure of effect {call} ({name})")
            return real(*args, **kwargs)

        return effect

    with ExitStack() as stack:
        for name, real in originals.items():
            stack.enter_context(mock.patch.object(effects, name, wrap(name, real)))
        yield issued


def _final_state(case: MoveCase) -> dict[str, Any]:
    """Everything a finished move leaves, with the fixture root written as <root>."""
    root = str(case.root)
    encoded_root = SharedStore.encode_path_untruncated(root)

    def norm(text: str) -> str:
        return text.replace(root, "<root>").replace(encoded_root, "<encoded root>")

    files: dict[str, Any] = {}
    for base in (case.shared_projects, case.default_projects):
        for path in sorted(base.rglob("*")):
            rel = norm(str(path))
            if path.is_symlink() or path.is_dir():
                files[rel] = "dir" if path.is_dir() else "link"
            else:
                st = os.stat(path)
                files[rel] = (
                    norm(path.read_text()),
                    st.st_mtime_ns
                    if path.suffix == ".jsonl" and path.parent.name != "subagents"
                    else None,
                )
    events = []
    for event in case.moved_events():
        data = json.loads(lifecycle.event_to_json(event))
        del data["id"], data["at"]
        events.append(json.loads(norm(json.dumps(data))))
    files["<moved events>"] = events
    files["<journal>"] = journal_path(case.ws, SESSION).exists()
    return files


class InterruptedMoveTests(unittest.TestCase):
    """A move stopped after any effect is completed by a rerun, to the same end."""

    def fresh(self) -> MoveCase:
        case = MoveCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.seed()
        return case

    def test_a_rerun_completes_a_move_interrupted_at_each_effect(self) -> None:
        reference = self.fresh()
        with _fault_at(0) as issued:
            reference.run_move()
        expected = _final_state(reference)
        self.assertGreater(len(issued), 6)

        for call in range(1, len(issued) + 1):
            with self.subTest(call=call, effect=issued[call - 1]):
                case = self.fresh()
                with _fault_at(call):
                    with self.assertRaises(_Injected):
                        case.run_move()
                case.run_move()
                self.assertEqual(_final_state(case), expected)

    def test_each_step_is_recorded_in_the_journal_as_it_completes(self) -> None:
        case = self.fresh()
        reference = self.fresh()
        with _fault_at(0) as issued:
            reference.run_move()
        # Stop at the last effect, the journal's removal: every step is recorded.
        last = len(issued)
        self.assertEqual(issued[-1], "remove")
        with _fault_at(last):
            with self.assertRaises(_Injected):
                case.run_move()
        journal = json.loads(journal_path(case.ws, SESSION).read_text())
        self.assertEqual(
            journal["steps_done"],
            [
                "transcript-moved",
                "folder-moved",
                "transcript-rewritten",
                "times-restored",
                "lifecycle-recorded",
                "source-removed",
            ],
        )

    def test_a_different_target_is_refused_while_a_journal_exists(self) -> None:
        case = self.fresh()
        with _fault_at(4):
            with self.assertRaises(_Injected):
                case.run_move()
        journal = journal_path(case.ws, SESSION)
        self.assertTrue(journal.exists())
        gamma = case.layout.projects_root / "gamma"
        gamma.mkdir()
        msg = case.refusal(target=gamma)
        self.assertIn(str(journal), msg)
        self.assertIn(str(case.beta), msg)
        # The fix the message names: rerun with the journaled directory.
        fix = f"claudewheel move-session {SESSION} {case.beta}"
        self.assertIn(fix, msg)
        case.run_move(target=case.beta)
        self.assertFalse(journal.exists())

    def test_a_journaled_split_is_the_journaled_move_not_a_duplicate(self) -> None:
        """Transcript in the target, folder still in the source: the rerun finishes it."""
        case = self.fresh()
        reference = self.fresh()
        with _fault_at(0) as issued:
            reference.run_move()
        # The first rename is the transcript's; fail the second, the folder's.
        second_rename = [i for i, name in enumerate(issued, 1) if name == "rename"][1]
        with _fault_at(second_rename):
            with self.assertRaises(_Injected):
                case.run_move()
        source = case.shared_projects / SharedStore.encode_path(str(case.alpha))
        target = case.shared_projects / SharedStore.encode_path(str(case.beta))
        self.assertTrue((target / f"{SESSION}.jsonl").exists())
        self.assertTrue((source / SESSION).is_dir())
        case.run_move()
        self.assertTrue((target / SESSION).is_dir())
        self.assertFalse(source.exists())


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class CommandTests(MoveCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            Path, "home", autospec=True, return_value=self.layout.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.layout.home),
                "CLAUDEWHEEL_CONFIG_DIR": str(self.ws.root),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args: str) -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            mock.patch("sys.argv", ["claudewheel", *args]),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return out.getvalue(), err.getvalue(), code

    def test_the_command_moves_the_session(self) -> None:
        self.seed()
        out, err, code = self.run_cli("move-session", SESSION, str(self.beta))
        self.assertEqual(code, 0, err)
        target = self.shared_projects / SharedStore.encode_path(str(self.beta))
        self.assertTrue((target / f"{SESSION}.jsonl").is_file())
        self.assertIn(f"Moved session {SESSION}", out)
        self.assertIn(str(self.beta), out)

    def test_a_refusal_is_an_error_and_exit_1(self) -> None:
        out, err, code = self.run_cli("move-session", SESSION[:8], str(self.beta))
        self.assertEqual(code, 1)
        self.assertIn("Error: ", err)
        self.assertIn("is not a session id", err)

    def test_both_arguments_are_required(self) -> None:
        _out, err, code = self.run_cli("move-session", SESSION)
        self.assertNotEqual(code, 0)
        self.assertIn("directory", err)


if __name__ == "__main__":
    unittest.main()
