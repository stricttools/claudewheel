"""Tests for the session row block formatter and the current-session marker.

Nothing here touches a terminal, a clock or the ambient environment: the row
formatter takes its "now" and its identity as parameters, so every line count
and every marker decision is reproducible.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from claudewheel.session_registry import SessionRecord
from claudewheel.session_rows import (
    CURRENT_MARK,
    PID_ENV,
    SESSION_ID_ENV,
    SessionIdentity,
    current_identity,
    format_memory,
    format_row,
    format_uptime,
    is_current,
)

STARTED_AT = 1_786_536_700_326
NOW_MS = STARTED_AT + 7 * 3_600_000 + 12 * 60_000  # seven hours, twelve minutes
SESSION_ID = "b90b8375-6d62-457f-b66f-4bf9e7557d8e"


def _record(**overrides: object) -> SessionRecord:
    """A registry record shaped like the ones Claude Code really writes."""
    fields: dict[str, object] = dict(
        path=Path("/tmp/sessions/2214292.json"),
        pid=2214292,
        kind="interactive",
        live=True,
        session_id=SESSION_ID,
        cwd="/home/m/Projects",
        status="busy",
        name="projects-7a",
        version="2.1.226",
        started_at=STARTED_AT,
        proc_start="655794975",
    )
    fields.update(overrides)
    return SessionRecord(**fields)  # type: ignore[arg-type]


class LineCountTests(unittest.TestCase):
    """The exact block height per state -- the viewport's row heights."""

    def test_collapsed_is_two_lines(self) -> None:
        """A collapsed row with no state line is exactly two lines."""
        lines = format_row(_record(), highlighted=False, now_ms=NOW_MS)
        self.assertEqual(len(lines), 2)

    def test_collapsed_with_state_is_three_lines(self) -> None:
        """A collapsed row carrying a per-row state is exactly three lines."""
        lines = format_row(_record(), highlighted=False, now_ms=NOW_MS, state="running")
        self.assertEqual(len(lines), 3)
        self.assertIn("running", lines[2])

    def test_highlighted_is_five_lines(self) -> None:
        """A highlighted row is exactly five lines, state or no state."""
        plain = format_row(_record(), highlighted=True, now_ms=NOW_MS)
        stateful = format_row(
            _record(), highlighted=True, now_ms=NOW_MS, state="stopped"
        )
        self.assertEqual(len(plain), 5)
        self.assertEqual(len(stateful), 5)
        self.assertEqual(plain[4], "")
        self.assertIn("stopped", stateful[4])

    def test_counts_do_not_move_with_missing_fields(self) -> None:
        """A record missing every optional field still has its state's height."""
        bare = _record(
            session_id=None,
            cwd=None,
            status=None,
            name=None,
            version=None,
            started_at=None,
        )
        self.assertEqual(len(format_row(bare, highlighted=False, now_ms=NOW_MS)), 2)
        self.assertEqual(len(format_row(bare, highlighted=True, now_ms=NOW_MS)), 5)
        self.assertEqual(
            len(format_row(bare, highlighted=False, now_ms=NOW_MS, state="running")), 3
        )

    def test_counts_do_not_move_with_memory_or_selector(self) -> None:
        """The checklist's extra columns change content, never the height."""
        checklist = format_row(
            _record(),
            highlighted=False,
            now_ms=NOW_MS,
            rss_kib=1_310_720,
            state="running",
            selector="[x]",
        )
        self.assertEqual(len(checklist), 3)
        self.assertEqual(
            len(
                format_row(
                    _record(),
                    highlighted=True,
                    now_ms=NOW_MS,
                    rss_kib=1_310_720,
                    state="running",
                    selector="[ ]",
                )
            ),
            5,
        )


class ContentTests(unittest.TestCase):
    def test_collapsed_content(self) -> None:
        """The header names the session; the summary carries directory and uptime."""
        header, summary = format_row(_record(), highlighted=False, now_ms=NOW_MS)
        self.assertIn("projects-7a", header)
        self.assertIn("interactive", header)
        self.assertIn("busy", header)
        self.assertIn("/home/m/Projects", summary)
        self.assertIn("up 7h 12m", summary)

    def test_highlighted_content(self) -> None:
        """The expanded block adds the directory, the identity and the resources."""
        lines = format_row(
            _record(), highlighted=True, now_ms=NOW_MS, rss_kib=1_310_720
        )
        self.assertIn("projects-7a", lines[0])
        self.assertIn("/home/m/Projects", lines[1])
        self.assertIn("pid 2214292", lines[2])
        self.assertIn(SESSION_ID, lines[2])
        self.assertIn("2.1.226", lines[2])
        self.assertIn("up 7h 12m", lines[3])
        self.assertIn("1.2 GiB", lines[3])

    def test_memory_only_appears_when_it_was_measured(self) -> None:
        """An unmeasured process shows no memory clause rather than a fake one."""
        lines = format_row(_record(), highlighted=True, now_ms=NOW_MS)
        self.assertNotIn("GiB", lines[3])
        self.assertNotIn("KiB", lines[3])

    def test_selector_prefixes_the_header_and_indents_the_rest(self) -> None:
        """The checklist's toggle sits at the left edge and the block aligns under it."""
        lines = format_row(
            _record(), highlighted=False, now_ms=NOW_MS, selector="[x]", state="running"
        )
        self.assertTrue(lines[0].startswith("[x] "))
        for line in lines[1:]:
            self.assertTrue(line.startswith(" " * 6), line)

    def test_stale_record_says_so(self) -> None:
        """A record whose process is gone is marked stale in its header."""
        header = format_row(_record(live=False), highlighted=False, now_ms=NOW_MS)[0]
        self.assertIn("stale", header)


class CurrentSessionMarkerTests(unittest.TestCase):
    """Both environment values must match, independently, for the mark."""

    IDENTITY = SessionIdentity(session_id=SESSION_ID, pid=2214292)

    def test_matching_both_is_marked(self) -> None:
        """A record agreeing on session id and pid is the current session."""
        self.assertTrue(is_current(_record(), self.IDENTITY))
        header = format_row(
            _record(), highlighted=False, now_ms=NOW_MS, identity=self.IDENTITY
        )[0]
        self.assertIn(CURRENT_MARK, header)

    def test_matching_only_the_session_id_is_not_marked(self) -> None:
        """The same session id under a different pid is not this process."""
        other = _record(pid=999)
        self.assertFalse(is_current(other, self.IDENTITY))
        header = format_row(
            other, highlighted=False, now_ms=NOW_MS, identity=self.IDENTITY
        )[0]
        self.assertNotIn(CURRENT_MARK, header)

    def test_matching_only_the_pid_is_not_marked(self) -> None:
        """The same pid under a different session id is a recycled number."""
        other = _record(session_id="00000000-0000-0000-0000-000000000000")
        self.assertFalse(is_current(other, self.IDENTITY))
        header = format_row(
            other, highlighted=False, now_ms=NOW_MS, identity=self.IDENTITY
        )[0]
        self.assertNotIn(CURRENT_MARK, header)

    def test_matching_neither_is_not_marked(self) -> None:
        """An unrelated record carries no mark."""
        other = _record(pid=999, session_id="00000000-0000-0000-0000-000000000000")
        self.assertFalse(is_current(other, self.IDENTITY))

    def test_a_record_without_a_session_id_never_matches(self) -> None:
        """A record whose file carries no session id cannot be the current one."""
        self.assertFalse(is_current(_record(session_id=None), self.IDENTITY))

    def test_no_identity_marks_nothing(self) -> None:
        """With no identity resolved, no row claims to be the current session."""
        self.assertFalse(is_current(_record(), None))
        header = format_row(_record(), highlighted=False, now_ms=NOW_MS)[0]
        self.assertNotIn(CURRENT_MARK, header)

    def test_exactly_one_row_of_a_registry_is_marked(self) -> None:
        """Across a registry with a recycled pid and a shared id, one row matches."""
        records = [
            _record(pid=999),
            _record(session_id="00000000-0000-0000-0000-000000000000"),
            _record(),
        ]
        marked = [r for r in records if is_current(r, self.IDENTITY)]
        self.assertEqual(len(marked), 1)
        self.assertIs(marked[0], records[2])


class IdentityFromEnvironmentTests(unittest.TestCase):
    def test_the_two_variables_are_the_ones_claude_code_exports(self) -> None:
        """The pair is the session id and the pid, by their exported names."""
        self.assertEqual(SESSION_ID_ENV, "CLAUDE_CODE_SESSION_ID")
        self.assertEqual(PID_ENV, "CLAUDE_PID")

    def test_both_present(self) -> None:
        """Both variables present and well-formed resolve to an identity."""
        identity = current_identity({SESSION_ID_ENV: SESSION_ID, PID_ENV: "2214292"})
        self.assertEqual(identity, SessionIdentity(session_id=SESSION_ID, pid=2214292))

    def test_missing_either_variable_resolves_to_nothing(self) -> None:
        """One value alone is not an identity -- both or neither."""
        self.assertIsNone(current_identity({SESSION_ID_ENV: SESSION_ID}))
        self.assertIsNone(current_identity({PID_ENV: "2214292"}))
        self.assertIsNone(current_identity({}))

    def test_unusable_values_resolve_to_nothing(self) -> None:
        """Empty or non-numeric values are no identity rather than a guess."""
        for env in (
            {SESSION_ID_ENV: "", PID_ENV: "2214292"},
            {SESSION_ID_ENV: SESSION_ID, PID_ENV: ""},
            {SESSION_ID_ENV: SESSION_ID, PID_ENV: "not-a-pid"},
            {SESSION_ID_ENV: SESSION_ID, PID_ENV: "-1"},
            {SESSION_ID_ENV: SESSION_ID, PID_ENV: "0"},
        ):
            with self.subTest(env=env):
                self.assertIsNone(current_identity(env))

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        """A value padded by a shell still resolves."""
        identity = current_identity(
            {SESSION_ID_ENV: f" {SESSION_ID} ", PID_ENV: " 2214292 "}
        )
        self.assertEqual(identity, SessionIdentity(session_id=SESSION_ID, pid=2214292))

    def test_the_mapping_is_the_only_source(self) -> None:
        """The environment is a parameter: an empty mapping resolves to nothing."""
        self.assertIsNone(current_identity({}))


class UptimeTests(unittest.TestCase):
    def test_scales(self) -> None:
        """Uptime reads in the two largest units that apply."""
        cases = [
            (5_000, "5s"),
            (90_000, "1m"),
            (3_600_000 + 720_000, "1h 12m"),
            (7 * 3_600_000 + 12 * 60_000, "7h 12m"),
            (50 * 3_600_000, "2d 2h"),
        ]
        for delta, expected in cases:
            with self.subTest(delta=delta):
                self.assertEqual(
                    format_uptime(STARTED_AT, STARTED_AT + delta), expected
                )

    def test_unknown_start(self) -> None:
        """A record with no start time has an unknown uptime, not a zero one."""
        self.assertEqual(format_uptime(None, NOW_MS), "unknown")

    def test_a_start_in_the_future_reads_as_zero(self) -> None:
        """Clock skew shows no uptime rather than a negative one."""
        self.assertEqual(format_uptime(NOW_MS + 10_000, NOW_MS), "0s")


class MemoryTests(unittest.TestCase):
    def test_units(self) -> None:
        """Resident memory arrives in KiB and reads in the fitting unit."""
        self.assertEqual(format_memory(512), "512 KiB")
        self.assertEqual(format_memory(2048), "2.0 MiB")
        self.assertEqual(format_memory(1_310_720), "1.2 GiB")

    def test_negative_is_an_error(self) -> None:
        """A negative resident size is a caller bug."""
        with self.assertRaises(ValueError):
            format_memory(-1)


if __name__ == "__main__":
    unittest.main()
