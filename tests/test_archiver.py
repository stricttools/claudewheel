"""Finding saferm, negotiating on its features, and handing a directory over.

Every test here stubs the wrapper seam -- ``claudewheel.effects.run`` -- rather
than a real binary, so the unit of test is claudewheel's half of the contract:
which argv it composes, what it does with the envelope, and what it refuses.
The round trip against a real saferm lives in
``tests/test_archiver_integration.py``.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from claudewheel import archiver
from tests.wheelhelpers import saferm_envelope_document

from claudewheel.archiver import (
    REQUIRED_FEATURES,
    ArchiveError,
    ArchiveHandle,
    Saferm,
    Unavailable,
)

ALL_FEATURES = [
    "git-index-switches",
    "group-id",
    "machine-payloads",
    "on-conflict-modes",
    "on-error-modes",
    "restore-destination",
    "trace-origin",
    "uuid-handles",
]


def envelope(payload: Any, *, command: str = "capabilities") -> str:
    """A strictcli machine-mode envelope carrying *payload*.

    Built from the suite's one description of that shape, so this double and
    the out-of-process stub in ``tests/wheelhelpers.py`` cannot drift apart.
    """
    return json.dumps(saferm_envelope_document(command, payload, app_version="0.8.1"))


def completed(stdout: str = "", *, code: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["saferm"], code, stdout, stderr)


#: Sentinel for "drop this key from the record entirely", which is a different
#: malformation from "the key is there and holds something unusable".
_DROP = object()


class FeatureSetTests(unittest.TestCase):
    """The pinned feature set is exactly what the delegation uses."""

    def test_the_required_features_are_the_four_the_delegation_uses(self) -> None:
        self.assertEqual(
            REQUIRED_FEATURES,
            {
                "machine-payloads",
                "on-error-modes",
                "git-index-switches",
                "uuid-handles",
            },
        )

    def test_a_real_saferm_feature_list_satisfies_them(self) -> None:
        self.assertTrue(REQUIRED_FEATURES <= set(ALL_FEATURES))


class LocateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(_tempdir()))

    def test_absent_everywhere_is_none(self) -> None:
        with mock.patch("shutil.which", autospec=True, return_value=None):
            self.assertIsNone(archiver.locate(self.root))

    def test_path_is_used_when_claudewheel_installed_none(self) -> None:
        with mock.patch("shutil.which", autospec=True, return_value="/usr/bin/saferm"):
            self.assertEqual(archiver.locate(self.root), Path("/usr/bin/saferm"))

    def test_claudewheels_own_copy_wins_over_path(self) -> None:
        own = archiver.bin_dir(self.root)
        own.mkdir(parents=True)
        binary = own / "saferm"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        with mock.patch("shutil.which", autospec=True, return_value="/usr/bin/saferm"):
            self.assertEqual(archiver.locate(self.root), binary)

    def test_a_non_executable_own_copy_is_not_used(self) -> None:
        own = archiver.bin_dir(self.root)
        own.mkdir(parents=True)
        (own / "saferm").write_text("not a program")
        (own / "saferm").chmod(0o644)
        with mock.patch("shutil.which", autospec=True, return_value=None):
            self.assertIsNone(archiver.locate(self.root))


class ProbeTests(unittest.TestCase):
    """The capabilities verb is the negotiation; no version is ever compared."""

    def test_the_probe_is_a_declared_read_of_the_capabilities_verb(self) -> None:
        with mock.patch(
            "claudewheel.archiver.effects.run",
            autospec=True,
            return_value=completed(envelope({"features": ALL_FEATURES})),
        ) as run:
            features = archiver.probe(Path("/usr/bin/saferm"))
        self.assertEqual(features, frozenset(ALL_FEATURES))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/saferm", "capabilities", "--json"])
        self.assertTrue(run.call_args.kwargs["read"])

    def test_a_missing_verb_answers_none(self) -> None:
        with mock.patch(
            "claudewheel.archiver.effects.run",
            autospec=True,
            return_value=completed("", code=2, stderr="unknown command"),
        ):
            self.assertIsNone(archiver.probe(Path("/usr/bin/saferm")))

    def test_unparseable_output_answers_none(self) -> None:
        with mock.patch(
            "claudewheel.archiver.effects.run",
            autospec=True,
            return_value=completed("git-index-switches\nuuid-handles\n"),
        ):
            self.assertIsNone(archiver.probe(Path("/usr/bin/saferm")))

    def test_an_unrunnable_binary_answers_none(self) -> None:
        with mock.patch(
            "claudewheel.archiver.effects.run",
            autospec=True,
            side_effect=OSError("Exec format error"),
        ):
            self.assertIsNone(archiver.probe(Path("/usr/bin/saferm")))

    def test_a_timeout_answers_none(self) -> None:
        with mock.patch(
            "claudewheel.archiver.effects.run",
            autospec=True,
            side_effect=subprocess.TimeoutExpired("saferm", 15.0),
        ):
            self.assertIsNone(archiver.probe(Path("/usr/bin/saferm")))


class DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(_tempdir()))

    def _detect(self, *, which: str | None, features: Any) -> Any:
        run = mock.MagicMock()
        if features is None:
            run.return_value = completed("", code=2)
        else:
            run.return_value = completed(envelope({"features": features}))
        with (
            mock.patch("shutil.which", autospec=True, return_value=which),
            mock.patch("claudewheel.archiver.effects.run", run),
        ):
            return archiver.detect(self.root)

    def test_absent(self) -> None:
        found = self._detect(which=None, features=None)
        self.assertEqual(found, Unavailable(kind="absent"))
        self.assertFalse(found.upgrade)
        self.assertIn("not installed", found.diagnosis())

    def test_no_capabilities_verb(self) -> None:
        found = self._detect(which="/usr/bin/saferm", features=None)
        self.assertEqual(found.kind, "no-verb")
        self.assertTrue(found.upgrade)
        self.assertIn("too old", found.diagnosis())

    def test_missing_a_required_feature(self) -> None:
        short = [f for f in ALL_FEATURES if f != "uuid-handles"]
        found = self._detect(which="/usr/bin/saferm", features=short)
        self.assertEqual(found.kind, "missing-features")
        self.assertEqual(found.missing, ("uuid-handles",))
        self.assertIn("uuid-handles", found.diagnosis())

    def test_a_capable_saferm(self) -> None:
        found = self._detect(which="/usr/bin/saferm", features=ALL_FEATURES)
        self.assertIsInstance(found, Saferm)
        self.assertEqual(found.binary, Path("/usr/bin/saferm"))

    def test_extra_unknown_features_are_fine(self) -> None:
        found = self._detect(
            which="/usr/bin/saferm", features=[*ALL_FEATURES, "something-new"]
        )
        self.assertIsInstance(found, Saferm)

    def test_no_version_string_is_ever_compared(self) -> None:
        """Decision 9: features, never numbers.

        A locally built saferm reports a Go pseudo-version no semver parser
        accepts, so an envelope whose ``app_version`` is one must still be
        accepted on the strength of its feature list alone.
        """
        run = mock.MagicMock(
            return_value=completed(
                json.dumps(
                    {
                        "interface_version": 1,
                        "app": "saferm",
                        "app_version": "0.8.2-0.20260813233643-b6b3112e92db",
                        "command": "capabilities",
                        "exit_code": 0,
                        "payload": {"features": ALL_FEATURES},
                    }
                )
            )
        )
        with (
            mock.patch("shutil.which", autospec=True, return_value="/usr/bin/saferm"),
            mock.patch("claudewheel.archiver.effects.run", run),
        ):
            self.assertIsInstance(archiver.detect(self.root), Saferm)


class UnavailableMessageTests(unittest.TestCase):
    """Decision 25: name the remedy, never an override."""

    def _messages(self, *, previewing: bool = False) -> list[str]:
        return [
            Unavailable(kind="absent").refusal_error("work", previewing=previewing),
            Unavailable(kind="no-verb", binary=Path("/usr/bin/saferm")).refusal_error(
                "work", previewing=previewing
            ),
            Unavailable(
                kind="missing-features",
                binary=Path("/usr/bin/saferm"),
                missing=("uuid-handles",),
            ).refusal_error("work", previewing=previewing),
        ]

    def test_the_no_terminal_message_says_there_is_no_terminal(self) -> None:
        for message in self._messages():
            self.assertIn("no terminal", message)
            self.assertNotIn("--dry-run", message)

    def test_a_preview_is_told_why_it_got_no_offer(self) -> None:
        """The same refusal is reached at a real terminal under --dry-run,
        where 'there is no terminal to offer the install at' is simply false:
        the reason is that a preview installs nothing."""
        for message in self._messages(previewing=True):
            self.assertIn("--dry-run", message)
            self.assertNotIn("no terminal", message)
            self.assertIn("nothing was deleted", message.lower())

    def test_both_wordings_name_saferm_and_the_install(self) -> None:
        for message in self._messages() + self._messages(previewing=True):
            self.assertIn("saferm", message)
            self.assertIn("go install github.com/smm-h/saferm@v0", message)

    def test_the_headless_message_names_saferm_and_the_install(self) -> None:
        for message in self._messages():
            self.assertIn("saferm", message)
            self.assertIn("go install github.com/smm-h/saferm@v0", message)

    def test_the_headless_message_states_irreversibility(self) -> None:
        for message in self._messages():
            self.assertIn("irreversible", message)
            self.assertIn("OAuth token", message)

    def test_the_headless_message_says_nothing_was_deleted(self) -> None:
        for message in self._messages():
            self.assertIn("nothing was deleted", message.lower())
            self.assertIn("work", message)

    def test_the_message_never_teaches_an_override(self) -> None:
        """There is no bypass flag, so no message may look like there is one."""
        banned = (
            "--force",
            "--skip",
            "--no-archive",
            "--allow",
            "--approve-consequential",
            "anyway",
            "override",
            "bypass",
        )
        for message in self._messages():
            for token in banned:
                self.assertNotIn(token, message.lower())

    def test_go_installs_pin_v0_never_latest(self) -> None:
        for command in archiver.INSTALL_COMMANDS:
            if command.startswith("go install"):
                self.assertTrue(command.endswith("@v0"))


class ArchiveTests(unittest.TestCase):
    """The delegated invocation, and what it does with the answer."""

    def setUp(self) -> None:
        self.saferm = Saferm(
            binary=Path("/usr/bin/saferm"), features=frozenset(ALL_FEATURES)
        )
        self.payload = {
            "group_id": "33e8059a-19c3-40bb-a4a7-b61e2088173d",
            "archived": [
                {
                    "id": 1,
                    "uuid": "4129d284-7510-4281-937d-286b42bb8d6c",
                    "path": "/cw/profiles/work",
                    "size": 4096,
                }
            ],
            "failed": [],
        }

    def _archive(self, result: Any) -> Any:
        with mock.patch(
            "claudewheel.archiver.effects.run", autospec=True, return_value=result
        ) as run:
            self.run_mock = run
            return self.saferm.archive(
                Path("/cw/profiles/work"), description="deleting profile 'work'"
            )

    def test_the_argv_is_fully_explicit(self) -> None:
        self._archive(completed(envelope(self.payload, command="delete")))
        argv = self.run_mock.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "/usr/bin/saferm",
                "delete",
                "--on-error",
                "abort",
                "--no-update-git-index",
                "--recursive",
                "--description",
                "deleting profile 'work'",
                "--json",
                "/cw/profiles/work",
            ],
        )

    def test_the_run_carries_the_declared_grant(self) -> None:
        self._archive(completed(envelope(self.payload, command="delete")))
        self.assertEqual(self.run_mock.call_args.kwargs["grant"], "archive-delegation")
        self.assertFalse(self.run_mock.call_args.kwargs.get("read", False))

    def test_a_success_yields_the_durable_handle(self) -> None:
        handle = self._archive(completed(envelope(self.payload, command="delete")))
        self.assertEqual(
            handle,
            ArchiveHandle(
                uuid="4129d284-7510-4281-937d-286b42bb8d6c",
                group_id="33e8059a-19c3-40bb-a4a7-b61e2088173d",
                path="/cw/profiles/work",
                size=4096,
            ),
        )
        self.assertEqual(
            handle.restore_command,
            "saferm undelete --no-update-git-index "
            "4129d284-7510-4281-937d-286b42bb8d6c",
        )

    def test_the_restore_command_never_stages_anything_in_a_git_index(self) -> None:
        """Both sides of the round trip keep out of somebody else's index.

        The delete side passes ``--no-update-git-index`` deliberately: a
        profile can sit inside a git worktree (a version-controlled dotfiles
        repo is the ordinary case), and claudewheel is archiving a directory it
        does not own. The restore has exactly the same problem in reverse --
        ``saferm undelete`` stages the restored path by default, which would
        put ``.credentials.json`` and the stored OAuth token into that
        worktree's index -- so the command claudewheel prints carries the flag
        too.
        """
        handle = self._archive(completed(envelope(self.payload, command="delete")))
        assert handle is not None
        self.assertIn("--no-update-git-index", handle.restore_command)

    def test_a_recorded_invocation_yields_no_handle(self) -> None:
        """A preview: the run was recorded, so there is nothing to hand back."""
        import strictcli

        carrier = mock.MagicMock(spec=strictcli.Unsettled)
        self.assertIsNone(self._archive(carrier))

    def test_a_non_zero_exit_raises_and_says_nothing_was_deleted(self) -> None:
        with self.assertRaises(ArchiveError) as ctx:
            self._archive(
                completed(
                    envelope({"group_id": "g", "archived": [], "failed": []}),
                    code=6,
                    stderr="error: archiving /cw/profiles/work: sockets not supported",
                )
            )
        self.assertIn("exited 6", str(ctx.exception))
        self.assertIn("sockets not supported", str(ctx.exception))
        self.assertIn("Nothing was deleted", str(ctx.exception))

    def test_an_unreadable_envelope_raises(self) -> None:
        with self.assertRaises(ArchiveError):
            self._archive(completed("archived: [1] ... /cw/profiles/work"))

    def test_an_empty_archived_list_raises(self) -> None:
        with self.assertRaises(ArchiveError):
            self._archive(
                completed(
                    envelope(
                        {"group_id": "g", "archived": [], "failed": []},
                        command="delete",
                    )
                )
            )

    # -- a success whose payload cannot be read -----------------------------
    #
    # Every branch above this one happens BEFORE anything is destroyed, and
    # says so. These do not: saferm exited 0, so the directory is archived and
    # gone, and what failed is claudewheel's reading of the answer. Reporting
    # one of these as a refusal would tell the user the profile is still there
    # when it is not, and would leave the handle unsaid.

    def _record(self, **overrides: Any) -> Any:
        archived = cast("list[dict[str, Any]]", self.payload["archived"])
        record: dict[str, Any] = dict(archived[0])
        for key, value in overrides.items():
            if value is _DROP:
                record.pop(key, None)
            else:
                record[key] = value
        payload = dict(self.payload, archived=[record])
        return completed(envelope(payload, command="delete"))

    def test_a_record_with_no_uuid_is_an_unreadable_success(self) -> None:
        from claudewheel.archiver import ArchiveUnreadable

        with self.assertRaises(ArchiveUnreadable) as ctx:
            self._archive(self._record(uuid=""))
        message = str(ctx.exception)
        self.assertNotIn("Nothing was deleted", message)
        self.assertIn("saferm list", message)

    def test_a_missing_uuid_key_is_an_unreadable_success(self) -> None:
        from claudewheel.archiver import ArchiveUnreadable

        with self.assertRaises(ArchiveUnreadable) as ctx:
            self._archive(self._record(uuid=_DROP))
        self.assertNotIn("Nothing was deleted", str(ctx.exception))

    def test_a_non_numeric_size_is_an_unreadable_success_not_a_refusal(self) -> None:
        """It used to be a ValueError out of int(), which the CLI caught as a
        refusal and reported with 'Nothing was deleted' -- after the directory
        had been archived and removed."""
        from claudewheel.archiver import ArchiveUnreadable

        with self.assertRaises(ArchiveUnreadable) as ctx:
            self._archive(self._record(size="four thousand"))
        self.assertNotIn("Nothing was deleted", str(ctx.exception))

    def test_a_broken_size_still_reports_the_handle_it_did_get(self) -> None:
        """Whatever handle information survived is in the message: with a uuid
        in hand the deletion is still recoverable, and the user is told how."""
        with self.assertRaises(ArchiveError) as ctx:
            self._archive(self._record(size=[1, 2]))
        message = str(ctx.exception)
        self.assertIn("4129d284-7510-4281-937d-286b42bb8d6c", message)
        self.assertIn(
            "saferm undelete --no-update-git-index "
            "4129d284-7510-4281-937d-286b42bb8d6c",
            message,
        )

    def test_a_record_that_is_not_an_object_is_an_unreadable_success(self) -> None:
        """It used to be an uncaught AttributeError from record.get()."""
        from claudewheel.archiver import ArchiveUnreadable

        payload = dict(self.payload, archived=["/cw/profiles/work"])
        with self.assertRaises(ArchiveUnreadable) as ctx:
            self._archive(completed(envelope(payload, command="delete")))
        self.assertIn("saferm list", str(ctx.exception))

    def test_an_unreadable_success_is_never_reported_as_a_refusal(self) -> None:
        """One rule over all of them: none of these may claim the profile is
        still there, and each names where the record can be found."""
        from claudewheel.archiver import ArchiveUnreadable

        cases = [
            completed("not json at all"),
            completed(
                envelope(
                    {"group_id": "g", "archived": [], "failed": []}, command="delete"
                )
            ),
            self._record(uuid=""),
            self._record(size="nope"),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ArchiveUnreadable) as ctx:
                    self._archive(case)
                message = str(ctx.exception)
                self.assertNotIn("Nothing was deleted", message)
                self.assertIn("saferm list", message)

    def test_a_timeout_raises_with_the_profile_intact(self) -> None:
        with mock.patch(
            "claudewheel.archiver.effects.run",
            autospec=True,
            side_effect=subprocess.TimeoutExpired("saferm", 1800.0),
        ):
            with self.assertRaises(ArchiveError) as ctx:
                self.saferm.archive(Path("/cw/profiles/work"), description="why")
        self.assertIn("Nothing was deleted", str(ctx.exception))


class AssetNameTests(unittest.TestCase):
    def test_linux_amd64(self) -> None:
        with (
            mock.patch("platform.machine", autospec=True, return_value="x86_64"),
            mock.patch("sys.platform", "linux"),
        ):
            self.assertEqual(
                archiver.asset_name("0.8.1"), "saferm_0.8.1_linux_amd64.tar.gz"
            )

    def test_darwin_arm64(self) -> None:
        with (
            mock.patch("platform.machine", autospec=True, return_value="arm64"),
            mock.patch("sys.platform", "darwin"),
        ):
            self.assertEqual(
                archiver.asset_name("0.8.1"), "saferm_0.8.1_darwin_arm64.tar.gz"
            )


def _tempdir() -> Any:
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
