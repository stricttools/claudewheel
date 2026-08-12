"""Tests for claudewheel.session_registry and the guards built on it.

The registry files are written by Claude Code itself (an external dependency):
one JSON file per live process at ``<config_dir>/sessions/<pid>.json``.  The
fixtures here mint records in that exact shape, and liveness is simulated
without spawning anything: the *live* records name this very test process and
carry its real kernel start token, the *reused-identifier* records name it with
the wrong token, and the *stale* records name a PID that does not exist.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from claudewheel import cli, profile_info, profile_ops, session_registry
from claudewheel.shared_store import SharedStore
from claudewheel.workspace import Workspace
from tests.wheelhelpers import (
    build_profile_dir,
    dead_pid,
    live_record,
    phantom_record,
    stale_record,
    write_session_record,
)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


class ReadRecordsTests(unittest.TestCase):
    """read_records() classifies every registry file it finds."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name)
        self.sessions = self.config_dir / "sessions"

    def test_missing_sessions_dir(self) -> None:
        self.assertEqual(session_registry.read_records(self.config_dir), [])

    def test_live_interactive_record(self) -> None:
        live_record(self.sessions)
        (record,) = session_registry.read_records(self.config_dir)
        self.assertEqual(record.pid, os.getpid())
        self.assertEqual(record.kind, "interactive")
        self.assertEqual(record.status, "busy")
        self.assertEqual(record.name, "projects-9a")
        self.assertTrue(record.live)
        self.assertTrue(record.interactive)

    def test_stale_record_is_not_live(self) -> None:
        stale_record(self.sessions)
        (record,) = session_registry.read_records(self.config_dir)
        self.assertFalse(record.live)

    def test_reused_identifier_record_is_not_live(self) -> None:
        """The PID is alive but belongs to a different process than the record."""
        phantom_record(self.sessions)
        (record,) = session_registry.read_records(self.config_dir)
        self.assertEqual(record.pid, os.getpid())
        self.assertFalse(record.live)

    def test_mixed_registry_classification(self) -> None:
        """One registry holding a live, a stale and a reused-identifier record.

        The reused-identifier one gets its own config dir: it names the same PID
        as the live one, and one PID is one filename.
        """
        live_record(self.sessions)
        stale_record(self.sessions)
        phantom_dir = self.config_dir / "reused"
        phantom_record(phantom_dir / "sessions")

        records = session_registry.read_records(self.config_dir)
        self.assertEqual(len(records), 2)
        self.assertEqual([r.live for r in records if r.pid == os.getpid()], [True])
        self.assertEqual(len(session_registry.live_records(self.config_dir)), 1)
        (phantom,) = session_registry.read_records(phantom_dir)
        self.assertFalse(phantom.live)

    def test_background_kinds_are_live_but_not_interactive(self) -> None:
        for kind in ("bg", "daemon", "daemon-worker"):
            with self.subTest(kind=kind):
                config_dir = self.config_dir / kind
                live_record(config_dir / "sessions", kind=kind, name=None)
                (record,) = session_registry.read_records(config_dir)
                self.assertTrue(record.live)
                self.assertFalse(record.interactive)

    def test_record_without_proc_start_falls_back_to_pid_existence(self) -> None:
        write_session_record(self.sessions, os.getpid(), proc_start=None)
        (record,) = session_registry.read_records(self.config_dir)
        self.assertTrue(record.live)
        self.assertIsNone(record.proc_start)

    def test_unparseable_and_foreign_files_are_skipped(self) -> None:
        self.sessions.mkdir(parents=True)
        (self.sessions / "1.json").write_text("{not json")
        (self.sessions / "notes.txt").write_text(str(os.getpid()))
        # The suffix the two former readers scanned for is NOT a registry file.
        (self.sessions / "sess.pid").write_text(str(os.getpid()))
        self.assertEqual(session_registry.read_records(self.config_dir), [])

    def test_pid_read_from_filename_when_the_field_is_absent(self) -> None:
        self.sessions.mkdir(parents=True)
        (self.sessions / f"{os.getpid()}.json").write_text(
            json.dumps({"kind": "interactive"})
        )
        (record,) = session_registry.read_records(self.config_dir)
        self.assertEqual(record.pid, os.getpid())
        self.assertTrue(record.live)

    def test_absent_kind_defaults_to_interactive(self) -> None:
        """Claude Code omits nothing today, but an unlabelled record must not
        silently become a deletable background one."""
        write_session_record(
            self.sessions, os.getpid(), proc_start=None, extra={"kind": None}
        )
        (record,) = session_registry.read_records(self.config_dir)
        self.assertEqual(record.kind, "interactive")
        self.assertTrue(record.interactive)

    def test_unrecognized_kind_is_read_as_interactive(self) -> None:
        """A kind claudewheel has never heard of must not become deletable.

        Only the three known background kinds are non-interactive; every other
        string -- a future Claude Code kind, a typo, anything -- is read the
        conservative way, so a delete or a rename still refuses.
        """
        for kind in ("supervisor", "interactive-remote", ""):
            with self.subTest(kind=kind):
                config_dir = self.config_dir / f"kind-{kind or 'empty'}"
                live_record(config_dir / "sessions", kind=kind)
                (record,) = session_registry.read_records(config_dir)
                self.assertTrue(record.live)
                self.assertTrue(record.interactive)


class LiveHelperTests(unittest.TestCase):
    """live_records() / has_live_interactive() over the same fixtures."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name)
        self.sessions = self.config_dir / "sessions"

    def test_live_records_drops_phantoms(self) -> None:
        stale_record(self.sessions)
        live_record(self.sessions)
        records = session_registry.live_records(self.config_dir)
        self.assertEqual([r.pid for r in records], [os.getpid()])

    def test_has_live_interactive_true(self) -> None:
        live_record(self.sessions)
        self.assertTrue(session_registry.has_live_interactive(self.config_dir))

    def test_has_live_interactive_false_for_background_only(self) -> None:
        live_record(self.sessions, kind="daemon")
        self.assertTrue(session_registry.live_records(self.config_dir))
        self.assertFalse(session_registry.has_live_interactive(self.config_dir))

    def test_has_live_interactive_false_for_stale_interactive(self) -> None:
        stale_record(self.sessions)
        self.assertFalse(session_registry.has_live_interactive(self.config_dir))

    def test_two_live_processes_one_interactive(self) -> None:
        live_record(self.sessions, kind="interactive")
        live_record(self.sessions, pid=os.getppid(), kind="daemon", name=None)
        self.assertEqual(len(session_registry.live_records(self.config_dir)), 2)
        self.assertEqual(
            [r.pid for r in session_registry.live_interactive_records(self.config_dir)],
            [os.getpid()],
        )
        self.assertTrue(session_registry.has_live_interactive(self.config_dir))


class ProcessStartTokenTests(unittest.TestCase):
    """The process probe underneath the phantom filter."""

    def test_own_token_is_stable(self) -> None:
        token = session_registry.process_start_token(os.getpid())
        self.assertIsNotNone(token)
        self.assertEqual(token, session_registry.process_start_token(os.getpid()))

    def test_dead_pid_has_no_token(self) -> None:
        self.assertIsNone(session_registry.process_start_token(dead_pid()))


# ---------------------------------------------------------------------------
# Consumers
# ---------------------------------------------------------------------------


class _ProfileFixture(unittest.TestCase):
    """A real workspace with one profile directory on disk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.launcher_dir = root / ".claudewheel"
        self.profiles_dir = self.launcher_dir / "profiles"
        self.shared_dir = self.launcher_dir / "shared"
        self.shared_dir.mkdir(parents=True)
        for d in SharedStore.SHARED_SUBDIRS:
            (self.shared_dir / d).mkdir()
        self.profile = build_profile_dir(
            self.profiles_dir,
            "work",
            parents=True,
            exist_ok=False,
            credentials=True,
            settings_text="{}",
        )
        self.sessions = self.profile / "sessions"
        self.ws = Workspace.open(self.launcher_dir, claude_dir=root / ".claude")


class ReaderDelegationTests(_ProfileFixture):
    """Both former readers now delegate to the one module."""

    def test_is_profile_running_sees_a_live_interactive_record(self) -> None:
        live_record(self.sessions)
        self.assertTrue(profile_ops._is_profile_running(self.ws, "work"))

    def test_is_profile_running_ignores_background_records(self) -> None:
        live_record(self.sessions, kind="bg")
        self.assertFalse(profile_ops._is_profile_running(self.ws, "work"))

    def test_is_profile_running_ignores_stale_records(self) -> None:
        stale_record(self.sessions)
        self.assertFalse(profile_ops._is_profile_running(self.ws, "work"))

    def test_is_profile_running_without_sessions_dir(self) -> None:
        self.assertFalse(profile_ops._is_profile_running(self.ws, "work"))

    def test_report_counts_live_and_interactive_sessions(self) -> None:
        live_record(self.sessions)
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.active_sessions, 1)
        self.assertEqual(report.interactive_sessions, 1)

    def test_report_counts_background_as_active_but_not_interactive(self) -> None:
        live_record(self.sessions, kind="daemon")
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.active_sessions, 1)
        self.assertEqual(report.interactive_sessions, 0)

    def test_report_excludes_stale_records(self) -> None:
        stale_record(self.sessions)
        report = profile_info.gather_profile_info(self.ws, "work")
        self.assertEqual(report.active_sessions, 0)
        self.assertEqual(report.interactive_sessions, 0)

    def test_report_line_names_the_interactive_share(self) -> None:
        live_record(self.sessions, kind="daemon")
        report = profile_info.gather_profile_info(self.ws, "work")
        lines = profile_info.format_report(report)
        self.assertIn("Active sessions: 1 (0 interactive)", lines)


class DeleteGuardTests(_ProfileFixture):
    """2.2's stated property, end to end through the CLI delete handler."""

    def _delete(self) -> tuple[int | None, str]:
        err = io.StringIO()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                cli._handle_delete_profile(
                    self.ws, "work", force_delete=False, force_delete_data=False
                )
        except SystemExit as exc:
            code = exc.code
            return (code if isinstance(code, int) else 1), err.getvalue()
        return None, err.getvalue()

    def test_live_interactive_session_refuses_deletion(self) -> None:
        live_record(self.sessions)
        code, err = self._delete()
        self.assertEqual(code, 1)
        self.assertIn("interactive", err)
        self.assertTrue(self.profile.is_dir())

    def test_background_processes_do_not_refuse_deletion(self) -> None:
        live_record(self.sessions, kind="daemon")
        live_record(self.sessions, pid=os.getppid(), kind="daemon-worker", name=None)
        code, _err = self._delete()
        self.assertIsNone(code)
        self.assertFalse(self.profile.is_dir())

    def test_unrecognized_kind_refuses_deletion(self) -> None:
        """A live record whose kind is not one of the known background ones
        blocks the delete, exactly as an interactive one does."""
        live_record(self.sessions, kind="supervisor")
        code, err = self._delete()
        self.assertEqual(code, 1)
        self.assertIn("interactive", err)
        self.assertTrue(self.profile.is_dir())

    def test_stale_interactive_record_does_not_refuse_deletion(self) -> None:
        stale_record(self.sessions)
        code, _err = self._delete()
        self.assertIsNone(code)
        self.assertFalse(self.profile.is_dir())


class RenameGuardTests(_ProfileFixture):
    """The rename guard reads the same predicate."""

    def _rename(self) -> tuple[int | None, str]:
        err = io.StringIO()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                cli._handle_rename_profile(self.ws, "work", "play")
        except SystemExit as exc:
            code = exc.code
            return (code if isinstance(code, int) else 1), err.getvalue()
        return None, err.getvalue()

    def test_live_interactive_session_refuses_rename(self) -> None:
        live_record(self.sessions)
        code, err = self._rename()
        self.assertEqual(code, 1)
        self.assertIn("interactive", err)
        self.assertTrue(self.profile.is_dir())

    def test_background_processes_do_not_refuse_rename(self) -> None:
        live_record(self.sessions, kind="bg")
        code, _err = self._rename()
        self.assertIsNone(code)
        self.assertTrue((self.profiles_dir / "play").is_dir())


if __name__ == "__main__":
    unittest.main()
