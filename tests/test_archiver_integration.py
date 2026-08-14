"""13.4: a deleted profile really comes back, against the real saferm.

Everything else in the suite stops at claudewheel's own boundary -- the argv it
composes, the envelope it reads, the stores it does or does not touch. This
file is the one place the other side of that boundary is a real program: a
scratch profile is built in a sandboxed workspace, deleted through the whole
CLI flow, and then restored with the handle the deletion printed. What is
asserted afterwards is not that files exist but that the profile *works*: the
token file is back at 0600, and the launch environment resolves out of it.

The binary is the one ``scripts/build-saferm`` produces, and these tests skip
when it has not been built. A skip is the honest answer: an installed saferm
predating the machine surface answers none of this, and a suite that quietly
exercised claudewheel's stub instead would be claiming a round trip it never
performed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from claudewheel import archiver, cli
from claudewheel.profile_data import (
    PROFILE_DATA_DIR_MODE,
    TOKEN_FILE_MODE,
    TOKEN_FILE_NAME,
)
from claudewheel.tokens import TokenExpiryDisposition, plan_by_key
from claudewheel.workspace import Workspace
from tests.wheelhelpers import SandboxHomeTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILT_SAFERM = REPO_ROOT / "build" / "saferm"

BUILD_HINT = (
    f"no saferm at {BUILT_SAFERM}; run scripts/build-saferm to build one "
    "(the installed binary may predate the machine surface these tests use)"
)


def _capable() -> bool:
    """True when the built binary answers the probe with everything needed."""
    if not (BUILT_SAFERM.is_file() and os.access(BUILT_SAFERM, os.X_OK)):
        return False
    try:
        proc = subprocess.run(
            [str(BUILT_SAFERM), "capabilities", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        features = json.loads(proc.stdout)["payload"]["features"]
    except (ValueError, KeyError, TypeError):
        return False
    return archiver.REQUIRED_FEATURES <= set(features)


@unittest.skipUnless(_capable(), BUILD_HINT)
class RoundTripTests(SandboxHomeTestCase):
    """Delete through the CLI, restore with the printed handle."""

    _SETTINGS: dict[str, Any] = {
        "model": "claude-opus-4-8",
        "permissions": {"allow": [], "deny": [], "ask": []},
    }

    def setUp(self) -> None:
        super().setUp()
        self.ws = Workspace.default()
        self.store = self.ws.profiles
        # The archive lives inside the sandbox: nothing here reaches the real
        # ~/.saferm, and every record dies with the test's temporary home.
        self.saferm_home = self.home / ".saferm-under-test"
        patcher = mock.patch.dict("os.environ", {"SAFERM_HOME": str(self.saferm_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Where detection looks first, pointed at the built binary.
        bin_dir = archiver.bin_dir(self.ws.root)
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "saferm").symlink_to(BUILT_SAFERM)

    # -- helpers -----------------------------------------------------------

    def seed(self, name: str) -> Path:
        """A scratch profile with settings, shared-store links and a token."""
        self.store.create(name, dict(self._SETTINGS))
        self.store.data_for(name).write_token(
            "TOKEN-VALUE",
            expiry=TokenExpiryDisposition.TTL,
            plan=plan_by_key("max-20x"),
        )
        return self.store.path_for(name)

    def delete(self, name: str) -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        argv = [
            "c",
            "profile",
            "delete",
            name,
            "--no-force-delete",
            "--no-force-delete-data",
            "--approve-consequential",
        ]
        with mock.patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = (
                    e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                )
        return out.getvalue(), err.getvalue(), code

    def handle_from(self, printed: str) -> str:
        """The uuid the deletion reported, read off its own summary."""
        for line in printed.splitlines():
            if "Archived as " in line:
                return line.split("Archived as ", 1)[1].strip()
        self.fail(f"the deletion printed no archive handle:\n{printed}")

    def saferm(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BUILT_SAFERM), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    # -- the round trip ----------------------------------------------------

    def test_a_deleted_profile_restores_to_a_working_state(self) -> None:
        target = self.seed("work")
        before = json.loads((target / "settings.json").read_text())

        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        self.assertFalse(target.exists())
        uuid = self.handle_from(out)

        restore = self.saferm("undelete", "--no-update-git-index", uuid)
        self.assertEqual(restore.returncode, 0, restore.stderr)

        # It is a profile again, not a pile of files: discovery finds it, its
        # settings are byte-for-byte what they were, and the launch environment
        # resolves out of its own token file.
        self.assertTrue(target.is_dir())
        self.assertEqual(json.loads((target / "settings.json").read_text()), before)
        self.assertIn("work", [p.name for p in self.store.enumerate()])
        env = self.store.env("work")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(target))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "TOKEN-VALUE")

    def test_the_restored_token_file_is_private_again(self) -> None:
        """The whole reason the deletion is worth making recoverable: a
        long-lived OAuth token comes back, and comes back unreadable to anyone
        else."""
        self.seed("work")
        token_file = self.store.data_for("work").token_file
        self.assertEqual(token_file.name, TOKEN_FILE_NAME)
        self.assertEqual(token_file.stat().st_mode & 0o777, TOKEN_FILE_MODE)

        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        self.assertFalse(token_file.exists())
        self.saferm("undelete", "--no-update-git-index", self.handle_from(out))

        self.assertEqual(token_file.stat().st_mode & 0o777, TOKEN_FILE_MODE)
        self.assertEqual(self.store.data_for("work").token(), "TOKEN-VALUE")
        # The directory the token lives in is a nested entry too, so its
        # owner-only mode survives with it.
        self.assertEqual(
            token_file.parent.stat().st_mode & 0o777, PROFILE_DATA_DIR_MODE
        )

    def test_the_top_level_directorys_own_mode_is_not_restored(self) -> None:
        """A stated restore expectation, not an aspiration.

        Nested entries come back with the modes they had -- the token file at
        0600 and its directory at 0700 are asserted above. The archived
        directory's OWN mode is not among them: it comes back at the default
        instead. That is acceptable here because a profile directory carries no
        secret at its top level (everything sensitive is a nested entry with
        its own mode), but it is pinned so a future reader does not assume a
        guarantee that is not there.
        """
        target = self.seed("work")
        target.chmod(0o701)

        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        self.saferm("undelete", "--no-update-git-index", self.handle_from(out))

        self.assertTrue(target.is_dir())
        self.assertNotEqual(target.stat().st_mode & 0o777, 0o701)
        self.assertEqual(
            self.store.data_for("work").token_file.stat().st_mode & 0o777,
            TOKEN_FILE_MODE,
        )

    def test_one_operation_puts_it_back(self) -> None:
        """The restore command the deletion printed is the whole recipe: no
        second step, no flag the user has to know."""
        target = self.seed("work")
        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)

        command = ""
        for line in out.splitlines():
            if "Restore it with: " in line:
                command = line.split("Restore it with: ", 1)[1].strip()
        self.assertTrue(command.startswith("saferm undelete "))
        restore = self.saferm(*command.split()[1:])
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assertTrue(target.is_dir())

    def test_the_handle_is_reported_and_written_nowhere(self) -> None:
        """Decision 8: no tombstone, no launcher-side record.

        The archive holds everything needed to restore and keeps its own audit
        trail, so a second copy of the handle under ``~/.claudewheel/`` would
        be duplicate state with nobody owning its lifetime. The handle reaches
        the user by being printed; after that it lives in ``saferm list``.
        """
        self.seed("work")
        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        uuid = self.handle_from(out)

        carriers = [
            path
            for path in self.ws.root.rglob("*")
            if path.is_file() and uuid in path.read_bytes().decode("utf-8", "replace")
        ]
        self.assertEqual(carriers, [])
        # And it is still findable where it does live: the archive answers to
        # the handle, and says the record is restorable.
        info = self.saferm("info", uuid)
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn(uuid, info.stdout)
        self.assertIn("restorable", info.stdout)

    def test_the_shared_store_is_never_copied_or_touched(self) -> None:
        """A profile directory is mostly symlinks into a store that outlives
        it. The archival walks without following them, so the store is not
        copied on the way out and not disturbed on the way back."""
        target = self.seed("work")
        payload = self.ws.shared_dir / "projects" / "session.jsonl"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text("conversation history")
        inode = payload.stat().st_ino

        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        # Gone with the profile, but the store behind its links is untouched.
        self.assertFalse(target.exists())
        self.assertEqual(payload.read_text(), "conversation history")
        self.assertEqual(payload.stat().st_ino, inode)

        self.saferm("undelete", "--no-update-git-index", self.handle_from(out))
        link = target / "projects"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), payload.parent.resolve())
        self.assertEqual(payload.read_text(), "conversation history")
        self.assertEqual(payload.stat().st_ino, inode)

    def test_a_hard_linked_file_comes_back_with_its_content(self) -> None:
        """Decision 28's second half: deduplication is lost, content is kept.

        A profile can hold hard links (Claude Code's own caches make them), and
        an archive that dropped them silently would restore a profile missing
        files it had.
        """
        target = self.seed("work")
        original = target / "cache.json"
        original.write_text('{"cached": true}')
        (target / "cache-link.json").hardlink_to(original)

        out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        self.saferm("undelete", "--no-update-git-index", self.handle_from(out))

        self.assertEqual((target / "cache.json").read_text(), '{"cached": true}')
        self.assertEqual((target / "cache-link.json").read_text(), '{"cached": true}')

    @unittest.expectedFailure
    def test_a_dead_socket_is_skipped_rather_than_aborting(self) -> None:
        """Decision 28's first half, NOT yet shipped by the archiving tool.

        The rule is that a socket is skipped with an explicit recorded entry
        and the archival proceeds -- a socket is an endpoint, not data. What
        the tool does today is refuse the whole directory ("archive/tar:
        sockets not supported"), which claudewheel surfaces as a hard error
        with the profile intact. That failure mode is safe, so this is recorded
        as an expected failure rather than worked around.

        It does NOT turn green by itself. pytest reports an unexpected pass as
        a FAILURE, so the day a built saferm ships socket-skipping this test
        XPASSes and the suite goes red. That is the intended alarm, and the
        response to it is: remove the decorator, and extend the test to assert
        the skip is *recorded* (the archival proceeds, the profile is gone, and
        the skipped socket is named in saferm's own answer) rather than merely
        tolerated. The companion test below, which pins today's refusal, goes
        at the same time.

        It also fails loudly if this test is ever made to pass by weakening
        claudewheel's side instead.
        """
        import socket

        target = self.seed("work")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(target / "ipc.sock"))
        sock.close()

        _out, err, code = self.delete("work")
        self.assertEqual(code, 0, err)
        self.assertFalse(target.exists())

    def test_todays_socket_behaviour_leaves_the_profile_intact(self) -> None:
        """The safe half of the same finding, asserted so the failure mode is
        pinned while the rule is unshipped: the deletion is refused, the
        profile is still there, and every store still names it."""
        import socket

        target = self.seed("work")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(target / "ipc.sock"))
        sock.close()

        _out, err, code = self.delete("work")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was deleted", err)
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "settings.json").is_file())
        self.assertEqual(self.store.data_for("work").token(), "TOKEN-VALUE")
        self.assertIn("work", [p.name for p in self.store.enumerate()])


if __name__ == "__main__":
    unittest.main()
