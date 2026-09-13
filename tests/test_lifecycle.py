"""Tests for the per-session lifecycle event store.

The store is one append-only JSONL file per Claude Code session, validated line
by line by the strictspec-generated validator.  These tests exercise three
layers separately:

1. the line itself -- round trips through ``event_to_json``/``parse_event``, and
   every way a line can be rejected (missing or wrong ``format_version``, an
   unknown ``kind``, an unknown key, a bad enum);
2. the file -- what ``read_session`` tolerates (only an interrupted FINAL line)
   and what it refuses (anything else), and how ``append_event`` writes;
3. the derived picture -- ``summarize``'s latest-wins reading, the crash sweep,
   name capture, and the state every session table will be drawn from.

No test touches the real ``~/.claudewheel``: every file goes into a
``tempfile.TemporaryDirectory`` registered for cleanup.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claudewheel import lifecycle
from claudewheel.lifecycle import (
    FORMAT_VERSION,
    HIDDEN_BY_DEFAULT_STATES,
    LIFECYCLE_DIRNAME,
    LIVE_STATES,
    LOOSE_END_STATES,
    STATES,
    SWEEP_GRACE_MS,
    EndedEvent,
    Event,
    LifecycleError,
    MarkEvent,
    NamedEvent,
    SessionLifecycle,
    StartedEvent,
    append_event,
    capture_name,
    derive_state,
    event_to_json,
    load_all,
    new_event_id,
    now_timestamp,
    parse_event,
    parse_timestamp_ms,
    read_session,
    session_file,
    summarize,
    sweep_crashed,
)

SESSION = "0123abcd-1234-5678-9abc-0123456789ab"
OTHER_SESSION = "fedcba98-7654-3210-fedc-ba9876543210"

# A fixed wall clock for every test that compares timestamps: 2026-09-13
# 01:02:03.456 UTC, in milliseconds since the epoch.
NOW_MS = 1789261323456


def _at(offset_ms: int = 0) -> str:
    """A timestamp *offset_ms* milliseconds after the fixed clock."""
    return now_timestamp(NOW_MS + offset_ms)


def started(**overrides: object) -> StartedEvent:
    """A fully-populated StartedEvent, overridable field by field."""
    fields: dict[str, object] = {
        "id": "e-started",
        "at": _at(),
        "session": SESSION,
        "source": "hook",
        "cwd": "/home/m/Projects/claudewheel",
        "config_dir": "/home/m/.claudewheel/profiles/work",
        "profile": "work",
        "claude_version": "2.1.0",
        "model": "opus",
        "permissions": "acceptEdits",
        "entry": "startup",
        "transcript": "/home/m/.claudewheel/shared/projects/p/s.jsonl",
        "pid": 4242,
    }
    fields.update(overrides)
    return StartedEvent(**fields)  # type: ignore[arg-type]


def ended(**overrides: object) -> EndedEvent:
    fields: dict[str, object] = {
        "id": "e-ended",
        "at": _at(1000),
        "session": SESSION,
        "source": "hook",
        "outcome": "exited",
        "reason": "prompt_input_exit",
        "detail": None,
    }
    fields.update(overrides)
    return EndedEvent(**fields)  # type: ignore[arg-type]


def named(**overrides: object) -> NamedEvent:
    fields: dict[str, object] = {
        "id": "e-named",
        "at": _at(500),
        "session": SESSION,
        "source": "sweep",
        "name": "claudewheel-lifecycle",
        "name_source": "derived",
    }
    fields.update(overrides)
    return NamedEvent(**fields)  # type: ignore[arg-type]


def mark(**overrides: object) -> MarkEvent:
    fields: dict[str, object] = {
        "id": "e-mark",
        "at": _at(700),
        "session": SESSION,
        "source": "user",
        "state": "on-hold",
        "note": "waiting on review",
    }
    fields.update(overrides)
    return MarkEvent(**fields)  # type: ignore[arg-type]


class TempDirCase(unittest.TestCase):
    """A test case owning one temporary lifecycle directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.lifecycle_dir = self.root / LIFECYCLE_DIRNAME

    @property
    def path(self) -> Path:
        return session_file(self.lifecycle_dir, SESSION)

    def write_lines(self, *lines: str) -> Path:
        """Write raw lines (each newline-terminated) to the session file."""
        return self.write_raw("".join(f"{line}\n" for line in lines))

    def write_raw(self, text: str) -> Path:
        self.lifecycle_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        return self.path


# ---------------------------------------------------------------------------
# Timestamps and ids
# ---------------------------------------------------------------------------


class TimestampTests(unittest.TestCase):
    """now_timestamp writes RFC 3339 UTC milliseconds; parse_timestamp_ms reads it."""

    def test_format(self) -> None:
        self.assertEqual(now_timestamp(NOW_MS), "2026-09-13T01:02:03.456Z")

    def test_round_trip(self) -> None:
        for ms in (0, 1, 999, NOW_MS, NOW_MS + 1, NOW_MS - 457):
            self.assertEqual(parse_timestamp_ms(now_timestamp(ms)), ms)

    def test_default_is_now(self) -> None:
        stamp = now_timestamp()
        self.assertTrue(stamp.endswith("Z"))
        self.assertEqual(len(stamp), len("2026-09-13T01:02:03.456Z"))

    def test_lexical_order_is_time_order(self) -> None:
        stamps = [now_timestamp(NOW_MS + d) for d in (0, 1, 44, 1000, 86_400_000)]
        self.assertEqual(stamps, sorted(stamps))

    def test_offset_form_is_read_too(self) -> None:
        # A hand-written line may carry +00:00 instead of Z; both are the
        # same instant and the schema accepts both.
        self.assertEqual(parse_timestamp_ms("2026-09-13T01:02:03.456+00:00"), NOW_MS)

    def test_junk_is_an_error(self) -> None:
        with self.assertRaises(LifecycleError):
            parse_timestamp_ms("yesterday")

    def test_naive_timestamp_is_an_error(self) -> None:
        with self.assertRaises(LifecycleError):
            parse_timestamp_ms("2026-09-13T01:02:03.456")


class EventIdTests(unittest.TestCase):
    """new_event_id is unique and sorts by creation order."""

    def test_unique_over_a_thousand(self) -> None:
        ids = [new_event_id() for _ in range(1000)]
        self.assertEqual(len(set(ids)), 1000)

    def test_sorts_by_creation_order(self) -> None:
        ids = [new_event_id() for _ in range(1000)]
        self.assertEqual(ids, sorted(ids))


# ---------------------------------------------------------------------------
# One line
# ---------------------------------------------------------------------------


class SessionFileTests(unittest.TestCase):
    """session_file builds a path only from a real session uuid."""

    def test_path(self) -> None:
        self.assertEqual(
            session_file(Path("/s/lifecycle"), SESSION),
            Path("/s/lifecycle") / f"{SESSION}.jsonl",
        )

    def test_junk_is_refused(self) -> None:
        for junk in (
            "",
            "..",
            "../escape",
            "NOT-A-UUID",
            SESSION.upper(),
            SESSION + "x",
        ):
            with self.subTest(junk=junk), self.assertRaises(ValueError):
                session_file(Path("/s/lifecycle"), junk)


class RoundTripTests(unittest.TestCase):
    """Every kind survives event_to_json -> parse_event unchanged."""

    def test_each_kind(self) -> None:
        for event in (started(), ended(), named(), mark()):
            with self.subTest(kind=type(event).__name__):
                line = event_to_json(event)
                self.assertEqual(
                    parse_event(line, path=Path("x.jsonl"), lineno=1), event
                )

    def test_nulls_are_written_not_omitted(self) -> None:
        line = event_to_json(mark(state=None, note=None))
        data = json.loads(line)
        self.assertIsNone(data["state"])
        self.assertIsNone(data["note"])

    def test_key_order_is_schema_order(self) -> None:
        data = json.loads(event_to_json(ended()))
        self.assertEqual(
            list(data),
            [
                "format_version",
                "id",
                "at",
                "session",
                "source",
                "kind",
                "outcome",
                "reason",
                "detail",
            ],
        )

    def test_format_version_and_kind(self) -> None:
        data = json.loads(event_to_json(named()))
        self.assertEqual(data["format_version"], FORMAT_VERSION)
        self.assertEqual(data["kind"], "named")

    def test_line_carries_no_newline(self) -> None:
        self.assertNotIn("\n", event_to_json(started()))


class ParseRejectionTests(unittest.TestCase):
    """parse_event refuses every malformed line, naming file and line."""

    def assert_refused(self, line: str, *, needle: str | None = None) -> str:
        with self.assertRaises(LifecycleError) as caught:
            parse_event(line, path=Path("/s/lifecycle/x.jsonl"), lineno=7)
        message = str(caught.exception)
        self.assertIn("/s/lifecycle/x.jsonl", message)
        self.assertIn("7", message)
        if needle is not None:
            self.assertIn(needle, message)
        return message

    def _with(self, **overrides: object) -> str:
        data = json.loads(event_to_json(started()))
        data.update(overrides)
        return json.dumps(data)

    def _without(self, key: str) -> str:
        data = json.loads(event_to_json(started()))
        del data[key]
        return json.dumps(data)

    def test_not_json(self) -> None:
        self.assert_refused("{not json")

    def test_not_an_object(self) -> None:
        self.assert_refused("[1, 2, 3]")

    def test_missing_format_version(self) -> None:
        self.assert_refused(self._without("format_version"))

    def test_wrong_format_version(self) -> None:
        self.assert_refused(self._with(format_version=2))

    def test_unknown_kind(self) -> None:
        self.assert_refused(self._with(kind="resurrected"))

    def test_unknown_key(self) -> None:
        self.assert_refused(self._with(surprise="yes"))

    def test_missing_required_field(self) -> None:
        self.assert_refused(self._without("cwd"))

    def test_missing_nullable_field(self) -> None:
        # A nullable field is still a required KEY: "unknown" is written as
        # null, never by leaving the key out.
        self.assert_refused(self._without("profile"))

    def test_bad_enum(self) -> None:
        self.assert_refused(self._with(entry="bogus"))

    def test_bad_session_uuid(self) -> None:
        self.assert_refused(self._with(session="not-a-uuid"))

    def test_wrong_type(self) -> None:
        self.assert_refused(self._with(pid="4242"))


# ---------------------------------------------------------------------------
# One file
# ---------------------------------------------------------------------------


class ReadSessionTests(TempDirCase):
    """read_session reads a whole file, tolerating only an interrupted tail."""

    def test_missing_file(self) -> None:
        self.assertEqual(read_session(self.path), [])

    def test_empty_file(self) -> None:
        self.write_raw("")
        self.assertEqual(read_session(self.path), [])

    def test_file_order(self) -> None:
        events: list[Event] = [started(), named(), mark(), ended()]
        self.write_lines(*[event_to_json(e) for e in events])
        self.assertEqual(read_session(self.path), events)

    def test_blank_lines_are_skipped(self) -> None:
        self.write_raw(f"\n{event_to_json(started())}\n\n")
        self.assertEqual(read_session(self.path), [started()])

    def test_interrupted_final_line_is_dropped(self) -> None:
        good = event_to_json(started())
        self.write_raw(f"{good}\n" + event_to_json(ended())[:40])
        self.assertEqual(read_session(self.path), [started()])

    def test_interrupted_only_line_is_dropped(self) -> None:
        self.write_raw(event_to_json(started())[:30])
        self.assertEqual(read_session(self.path), [])

    def test_complete_final_line_without_newline_is_read(self) -> None:
        self.write_raw(event_to_json(started()))
        self.assertEqual(read_session(self.path), [started()])

    def test_damaged_non_final_line(self) -> None:
        self.write_lines("{truncated", event_to_json(ended()))
        with self.assertRaises(LifecycleError) as caught:
            read_session(self.path)
        message = str(caught.exception)
        self.assertIn(str(self.path), message)
        self.assertIn("1", message)

    def test_damaged_final_line_that_parses_as_json(self) -> None:
        # A final line that IS json but is not an event is damage of a kind an
        # interrupted write cannot produce: it is refused.
        self.write_raw(f"{event_to_json(started())}\n" + '{"a":1}')
        with self.assertRaises(LifecycleError):
            read_session(self.path)

    def test_line_number_names_the_offending_line(self) -> None:
        self.write_lines(
            event_to_json(started()),
            event_to_json(named()),
            '{"format_version":1,"kind":"mark"}',
            event_to_json(ended()),
        )
        with self.assertRaises(LifecycleError) as caught:
            read_session(self.path)
        self.assertIn(f"{self.path}:3", str(caught.exception))

    def test_unknown_kind_in_the_middle(self) -> None:
        data = json.loads(event_to_json(started()))
        data["kind"] = "exploded"
        self.write_lines(json.dumps(data), event_to_json(ended()))
        with self.assertRaises(LifecycleError):
            read_session(self.path)

    def test_missing_format_version_in_file(self) -> None:
        data = json.loads(event_to_json(started()))
        del data["format_version"]
        self.write_lines(json.dumps(data))
        with self.assertRaises(LifecycleError):
            read_session(self.path)

    def test_future_format_version_in_file(self) -> None:
        data = json.loads(event_to_json(started()))
        data["format_version"] = 2
        self.write_lines(json.dumps(data))
        with self.assertRaises(LifecycleError):
            read_session(self.path)

    def test_unknown_key_in_file(self) -> None:
        data = json.loads(event_to_json(started()))
        data["tomorrow"] = True
        self.write_lines(json.dumps(data))
        with self.assertRaises(LifecycleError):
            read_session(self.path)


class AppendEventTests(TempDirCase):
    """append_event validates first, then appends exactly one line."""

    def test_creates_directory_and_file(self) -> None:
        self.assertFalse(self.lifecycle_dir.exists())
        append_event(self.lifecycle_dir, started())
        self.assertTrue(self.path.is_file())
        self.assertEqual(read_session(self.path), [started()])

    def test_stamps_id_and_at(self) -> None:
        written = append_event(self.lifecycle_dir, started(id="", at=""))
        self.assertNotEqual(written.id, "")
        self.assertNotEqual(written.at, "")
        self.assertEqual(read_session(self.path), [written])

    def test_keeps_provided_id_and_at(self) -> None:
        event = started(id="mine", at=_at(99))
        written = append_event(self.lifecycle_dir, event)
        self.assertEqual(written, event)

    def test_returns_the_stamped_copy_without_touching_the_caller(self) -> None:
        event = started(id="", at="")
        append_event(self.lifecycle_dir, event)
        self.assertEqual(event.id, "")
        self.assertEqual(event.at, "")

    def test_appends_rather_than_truncates(self) -> None:
        append_event(self.lifecycle_dir, started())
        append_event(self.lifecycle_dir, ended())
        self.assertEqual(read_session(self.path), [started(), ended()])
        self.assertEqual(self.path.read_text(encoding="utf-8").count("\n"), 2)

    def test_invalid_event_is_refused_before_the_file_exists(self) -> None:
        with self.assertRaises(LifecycleError):
            append_event(self.lifecycle_dir, started(entry="bogus"))
        self.assertFalse(self.path.exists())

    def test_invalid_event_leaves_an_existing_file_untouched(self) -> None:
        append_event(self.lifecycle_dir, started())
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(LifecycleError):
            append_event(self.lifecycle_dir, ended(outcome="vanished"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_repairs_a_missing_trailing_newline(self) -> None:
        self.write_raw(event_to_json(started()))  # no trailing newline
        append_event(self.lifecycle_dir, ended())
        self.assertEqual(read_session(self.path), [started(), ended()])

    def test_leaves_a_damaged_tail_damaged(self) -> None:
        # The separating newline lets the new event start its own line; the
        # damaged line stays damaged, and read_session names it.
        self.write_raw(f"{event_to_json(started())}\n" + '{"a":1}')
        append_event(self.lifecycle_dir, ended())
        with self.assertRaises(LifecycleError):
            read_session(self.path)

    def test_refuses_a_junk_session(self) -> None:
        with self.assertRaises(ValueError):
            append_event(self.lifecycle_dir, started(session="nope"))


class LoadAllTests(TempDirCase):
    """load_all summarizes every session file in the directory."""

    def test_missing_dir(self) -> None:
        self.assertEqual(load_all(self.lifecycle_dir), {})

    def test_two_sessions(self) -> None:
        append_event(self.lifecycle_dir, started())
        append_event(self.lifecycle_dir, started(session=OTHER_SESSION, id="other"))
        loaded = load_all(self.lifecycle_dir)
        self.assertEqual(sorted(loaded), sorted([SESSION, OTHER_SESSION]))
        self.assertEqual(loaded[SESSION].session, SESSION)
        self.assertIsNotNone(loaded[OTHER_SESSION].started)

    def test_non_jsonl_files_are_ignored(self) -> None:
        append_event(self.lifecycle_dir, started())
        (self.lifecycle_dir / "notes.txt").write_text("scribbles\n", encoding="utf-8")
        (self.lifecycle_dir / "README").write_text("hi\n", encoding="utf-8")
        self.assertEqual(list(load_all(self.lifecycle_dir)), [SESSION])

    def test_non_uuid_jsonl_stem_is_an_error(self) -> None:
        self.lifecycle_dir.mkdir(parents=True, exist_ok=True)
        stray = self.lifecycle_dir / "scratch.jsonl"
        stray.write_text("", encoding="utf-8")
        with self.assertRaises(LifecycleError) as caught:
            load_all(self.lifecycle_dir)
        self.assertIn(str(stray), str(caught.exception))


# ---------------------------------------------------------------------------
# The derived picture
# ---------------------------------------------------------------------------


class SummarizeTests(unittest.TestCase):
    """summarize reads latest-wins, with the cross-event rules on top."""

    def test_empty(self) -> None:
        summary = summarize([], session=SESSION)
        self.assertEqual(
            summary,
            SessionLifecycle(
                session=SESSION,
                started=None,
                ended=None,
                name=None,
                mark=None,
                last_at="",
            ),
        )

    def test_latest_started_wins(self) -> None:
        # The newest `at` wins whatever order the lines came in.
        first = started(id="a", at=_at(0))
        second = started(id="b", at=_at(5000), entry="resume")
        self.assertEqual(summarize([first, second], session=SESSION).started, second)
        self.assertEqual(summarize([second, first], session=SESSION).started, second)

    def test_file_position_breaks_a_tie(self) -> None:
        first = started(id="a", at=_at())
        second = started(id="b", at=_at())
        self.assertEqual(summarize([first, second], session=SESSION).started, second)

    def test_latest_name_wins(self) -> None:
        old = named(id="a", at=_at(0), name="old")
        new = named(id="b", at=_at(10), name="new")
        self.assertEqual(summarize([old, new], session=SESSION).name, new)

    def test_ended_after_started(self) -> None:
        summary = summarize([started(), ended()], session=SESSION)
        self.assertEqual(summary.ended, ended())

    def test_ended_without_started(self) -> None:
        summary = summarize([ended()], session=SESSION)
        self.assertEqual(summary.ended, ended())

    def test_ended_is_ignored_when_a_later_started_exists(self) -> None:
        # A resumed session: it exited, then started again. The old exit is a
        # previous run's and must not make the session look dead.
        resumed = started(id="again", at=_at(5000), entry="resume")
        summary = summarize([started(), ended(), resumed], session=SESSION)
        self.assertEqual(summary.started, resumed)
        self.assertIsNone(summary.ended)

    def test_mark_latest_wins(self) -> None:
        first = mark(id="a", at=_at(0), state="on-hold")
        second = mark(id="b", at=_at(10), state="blocked")
        self.assertEqual(summarize([first, second], session=SESSION).mark, second)

    def test_null_mark_clears(self) -> None:
        set_mark = mark(id="a", at=_at(0), state="done")
        cleared = mark(id="b", at=_at(10), state=None, note=None)
        self.assertIsNone(summarize([set_mark, cleared], session=SESSION).mark)

    def test_mark_set_again_after_clearing(self) -> None:
        events: list[Event] = [
            mark(id="a", at=_at(0), state="done"),
            mark(id="b", at=_at(10), state=None, note=None),
            mark(id="c", at=_at(20), state="blocked"),
        ]
        result = summarize(events, session=SESSION).mark
        assert result is not None
        self.assertEqual(result.state, "blocked")

    def test_last_at_is_the_greatest(self) -> None:
        events: list[Event] = [
            ended(at=_at(1000)),
            started(at=_at(0)),
            mark(at=_at(500)),
        ]
        self.assertEqual(summarize(events, session=SESSION).last_at, _at(1000))

    def test_session_is_carried(self) -> None:
        self.assertEqual(summarize([], session=OTHER_SESSION).session, OTHER_SESSION)


class SweepCrashedTests(TempDirCase):
    """sweep_crashed records the sessions nothing is running and nothing ended."""

    def _lifecycles(self, *events: Event) -> dict[str, SessionLifecycle]:
        """Write *events* to the store, then read back what the sweep will see."""
        for event in events:
            append_event(self.lifecycle_dir, event)
        return load_all(self.lifecycle_dir)

    def test_marks_a_dead_session(self) -> None:
        lifecycles = self._lifecycles(started(at=_at(-SWEEP_GRACE_MS - 1)))
        written = sweep_crashed(
            self.lifecycle_dir, lifecycles, live_sessions=set(), now_ms=NOW_MS
        )
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].outcome, "crashed")
        self.assertEqual(written[0].source, "sweep")
        self.assertIsNone(written[0].reason)
        self.assertEqual(written[0].session, SESSION)
        self.assertEqual(
            read_session(self.path), [started(at=_at(-SWEEP_GRACE_MS - 1)), written[0]]
        )

    def test_respects_the_grace_period(self) -> None:
        lifecycles = self._lifecycles(started(at=_at(-SWEEP_GRACE_MS + 1)))
        self.assertEqual(
            sweep_crashed(
                self.lifecycle_dir, lifecycles, live_sessions=set(), now_ms=NOW_MS
            ),
            [],
        )
        self.assertEqual(len(read_session(self.path)), 1)  # only the started line

    def test_skips_a_live_session(self) -> None:
        lifecycles = self._lifecycles(started(at=_at(-SWEEP_GRACE_MS - 1)))
        self.assertEqual(
            sweep_crashed(
                self.lifecycle_dir, lifecycles, live_sessions={SESSION}, now_ms=NOW_MS
            ),
            [],
        )

    def test_skips_an_already_ended_session(self) -> None:
        lifecycles = self._lifecycles(started(at=_at(-SWEEP_GRACE_MS - 1)), ended())
        self.assertEqual(
            sweep_crashed(
                self.lifecycle_dir, lifecycles, live_sessions=set(), now_ms=NOW_MS
            ),
            [],
        )

    def test_skips_a_session_that_never_started(self) -> None:
        lifecycles = self._lifecycles(mark())
        self.assertEqual(
            sweep_crashed(
                self.lifecycle_dir, lifecycles, live_sessions=set(), now_ms=NOW_MS
            ),
            [],
        )

    def test_marks_only_the_qualifying_sessions(self) -> None:
        lifecycles = self._lifecycles(
            started(at=_at(-SWEEP_GRACE_MS - 1)),
            started(session=OTHER_SESSION, at=_at(-10)),
        )
        written = sweep_crashed(
            self.lifecycle_dir, lifecycles, live_sessions=set(), now_ms=NOW_MS
        )
        self.assertEqual([e.session for e in written], [SESSION])

    def test_is_idempotent(self) -> None:
        events = [started(at=_at(-SWEEP_GRACE_MS - 1))]
        first = sweep_crashed(
            self.lifecycle_dir,
            self._lifecycles(*events),
            live_sessions=set(),
            now_ms=NOW_MS,
        )
        self.assertEqual(len(first), 1)
        after = load_all(self.lifecycle_dir)
        second = sweep_crashed(
            self.lifecycle_dir, after, live_sessions=set(), now_ms=NOW_MS
        )
        self.assertEqual(second, [])
        self.assertEqual(len(read_session(self.path)), 2)


class CaptureNameTests(TempDirCase):
    """capture_name writes on first sight and on change, never on a repeat."""

    def test_first_sight(self) -> None:
        written = capture_name(
            self.lifecycle_dir,
            None,
            session=SESSION,
            name="alpha",
            name_source="derived",
        )
        assert written is not None
        self.assertEqual(written.name, "alpha")
        self.assertEqual(written.source, "sweep")
        self.assertEqual(read_session(self.path), [written])

    def test_no_name_writes_nothing(self) -> None:
        for name in (None, ""):
            with self.subTest(name=name):
                self.assertIsNone(
                    capture_name(
                        self.lifecycle_dir,
                        None,
                        session=SESSION,
                        name=name,
                        name_source=None,
                    )
                )
                self.assertFalse(self.path.exists())

    def test_repeat_writes_nothing(self) -> None:
        capture_name(
            self.lifecycle_dir,
            None,
            session=SESSION,
            name="alpha",
            name_source="derived",
        )
        lifecycle_now = load_all(self.lifecycle_dir)[SESSION]
        self.assertIsNone(
            capture_name(
                self.lifecycle_dir,
                lifecycle_now,
                session=SESSION,
                name="alpha",
                name_source="derived",
            )
        )
        self.assertEqual(len(read_session(self.path)), 1)

    def test_changed_name_writes(self) -> None:
        capture_name(
            self.lifecycle_dir,
            None,
            session=SESSION,
            name="alpha",
            name_source="derived",
        )
        lifecycle_now = load_all(self.lifecycle_dir)[SESSION]
        written = capture_name(
            self.lifecycle_dir,
            lifecycle_now,
            session=SESSION,
            name="beta",
            name_source="derived",
        )
        assert written is not None
        self.assertEqual(written.name, "beta")
        self.assertEqual(len(read_session(self.path)), 2)

    def test_changed_name_source_writes(self) -> None:
        capture_name(
            self.lifecycle_dir,
            None,
            session=SESSION,
            name="alpha",
            name_source="derived",
        )
        lifecycle_now = load_all(self.lifecycle_dir)[SESSION]
        written = capture_name(
            self.lifecycle_dir,
            lifecycle_now,
            session=SESSION,
            name="alpha",
            name_source="user",
        )
        assert written is not None
        self.assertEqual(written.name_source, "user")

    def test_lifecycle_without_a_name_writes(self) -> None:
        append_event(self.lifecycle_dir, started())
        lifecycle_now = load_all(self.lifecycle_dir)[SESSION]
        self.assertIsNone(lifecycle_now.name)
        written = capture_name(
            self.lifecycle_dir,
            lifecycle_now,
            session=SESSION,
            name="alpha",
            name_source=None,
        )
        self.assertIsNotNone(written)


class DeriveStateTests(unittest.TestCase):
    """derive_state is the single authority the session table reads."""

    def summary(self, *events: Event) -> SessionLifecycle:
        return summarize(list(events), session=SESSION)

    def test_state_sets_partition_the_vocabulary(self) -> None:
        self.assertEqual(len(set(STATES)), len(STATES))
        self.assertEqual(
            LIVE_STATES | LOOSE_END_STATES | HIDDEN_BY_DEFAULT_STATES, set(STATES)
        )
        self.assertFalse(LIVE_STATES & LOOSE_END_STATES)
        self.assertFalse(LIVE_STATES & HIDDEN_BY_DEFAULT_STATES)
        self.assertFalse(LOOSE_END_STATES & HIDDEN_BY_DEFAULT_STATES)

    def test_live_but_unverified(self) -> None:
        # Rule 1 beats every status: the process exists but the kernel start
        # token could not be checked, so the identity is unproven.
        self.assertEqual(
            derive_state(
                self.summary(started()),
                live=True,
                verified=False,
                status="busy",
                registry_present=True,
                now_ms=NOW_MS,
            ),
            "unverified",
        )

    def test_live_statuses(self) -> None:
        cases = {
            "busy": "working",
            "shell": "shell",
            "idle": "idle",
            "waiting": "waiting",
            None: "running",
            "something-new": "running",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(
                    derive_state(
                        self.summary(started()),
                        live=True,
                        verified=True,
                        status=status,
                        registry_present=True,
                        now_ms=NOW_MS,
                    ),
                    expected,
                )

    def test_live_beats_a_mark(self) -> None:
        self.assertEqual(
            derive_state(
                self.summary(started(), mark(state="done")),
                live=True,
                verified=True,
                status="idle",
                registry_present=True,
                now_ms=NOW_MS,
            ),
            "idle",
        )

    def test_mark_wins_over_ended(self) -> None:
        for state in ("on-hold", "blocked", "done"):
            with self.subTest(state=state):
                self.assertEqual(
                    derive_state(
                        self.summary(
                            started(), ended(), mark(at=_at(2000), state=state)
                        ),
                        live=False,
                        verified=False,
                        status=None,
                        registry_present=False,
                        now_ms=NOW_MS,
                    ),
                    state,
                )

    def test_ended_outcome(self) -> None:
        for outcome in ("exited", "crashed"):
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    derive_state(
                        self.summary(started(), ended(outcome=outcome, reason=None)),
                        live=False,
                        verified=False,
                        status=None,
                        registry_present=True,
                        now_ms=NOW_MS,
                    ),
                    outcome,
                )

    def test_registry_without_a_process_is_a_crash(self) -> None:
        # No lifecycle at all, but Claude Code left a registry file behind:
        # the process died without writing a SessionEnd.
        self.assertEqual(
            derive_state(
                None,
                live=False,
                verified=False,
                status=None,
                registry_present=True,
                now_ms=NOW_MS,
            ),
            "crashed",
        )

    def test_started_within_the_grace_is_starting(self) -> None:
        self.assertEqual(
            derive_state(
                self.summary(started(at=_at(-SWEEP_GRACE_MS + 1))),
                live=False,
                verified=False,
                status=None,
                registry_present=False,
                now_ms=NOW_MS,
            ),
            "starting",
        )

    def test_started_long_ago_is_a_crash(self) -> None:
        self.assertEqual(
            derive_state(
                self.summary(started(at=_at(-SWEEP_GRACE_MS - 1))),
                live=False,
                verified=False,
                status=None,
                registry_present=False,
                now_ms=NOW_MS,
            ),
            "crashed",
        )

    def test_nothing_says_it_is_alive(self) -> None:
        # A cleared mark and a name, no started and no ended: the session is
        # over as far as anything recorded knows.
        cleared = self.summary(
            named(),
            mark(id="a", at=_at(10), state="done"),
            mark(id="b", at=_at(20), state=None, note=None),
        )
        self.assertEqual(
            derive_state(
                cleared,
                live=False,
                verified=False,
                status=None,
                registry_present=False,
                now_ms=NOW_MS,
            ),
            "exited",
        )

    def test_no_lifecycle_and_no_registry(self) -> None:
        self.assertEqual(
            derive_state(
                None,
                live=False,
                verified=False,
                status=None,
                registry_present=False,
                now_ms=NOW_MS,
            ),
            "exited",
        )

    def test_every_derived_state_is_in_the_vocabulary(self) -> None:
        summaries = [
            None,
            self.summary(),
            self.summary(started()),
            self.summary(started(), ended()),
            self.summary(started(), mark()),
        ]
        for summary in summaries:
            for live in (True, False):
                for verified in (True, False):
                    for status in ("busy", "shell", "idle", "waiting", None, "?"):
                        for registry in (True, False):
                            state = derive_state(
                                summary,
                                live=live,
                                verified=verified,
                                status=status,
                                registry_present=registry,
                                now_ms=NOW_MS,
                            )
                            self.assertIn(state, STATES)


class ModuleSurfaceTests(unittest.TestCase):
    """__all__ names every public object, and nothing it does not have."""

    def test_all_resolves(self) -> None:
        for name in lifecycle.__all__:
            self.assertTrue(hasattr(lifecycle, name), name)

    def test_format_version_is_one(self) -> None:
        self.assertEqual(FORMAT_VERSION, 1)

    def test_dirname(self) -> None:
        self.assertEqual(LIFECYCLE_DIRNAME, "lifecycle")
