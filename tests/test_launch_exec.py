"""Tests for launch.do_launch -- the os.execvpe boundary.

resolve_launch_config (covered in test_launch.py and test_launch_integration.py)
builds the ``(cwd, argv, env)`` triple; ``do_launch`` is the thin exec boundary
that chdirs into ``cwd`` and replaces the process image via ``os.execvpe``. It
had no direct test. These pin:

- ``do_launch`` forwards the exact binary path (``argv[0]``), argv list, and env
  dict to ``os.execvpe``;
- it chdirs to the selected directory *before* exec;
- it forwards env VERBATIM -- the ``os.environ`` merge happens inside
  ``resolve_launch_config`` (``env = dict(os.environ)``), NOT in ``do_launch``,
  which passes the same object straight through;
- an end-to-end ``resolve_launch_config`` -> ``do_launch`` pass carries a live GH
  token and model flags all the way to the ``execvpe`` call, against a sandbox
  workspace.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from claudewheel.binaries import BinaryLocator
from claudewheel.launch import do_launch, resolve_launch_config
from claudewheel.workspace import Workspace
from tests.wheelhelpers import SandboxHomeTestCase


class DoLaunchExecBoundaryTests(unittest.TestCase):
    """do_launch chdirs then execs, forwarding the triple verbatim."""

    def test_execvpe_receives_exact_binary_argv_env(self) -> None:
        """execvpe is called once with (argv[0], argv, env) exactly as given."""
        argv = ["/opt/claude/bin/claude", "--verbose", "--model", "m-1"]
        env = {"CLAUDE_CONFIG_DIR": "/cfg", "GH_TOKEN": "gh-tok"}
        with mock.patch("os.chdir"), mock.patch("os.execvpe") as m_exec:
            do_launch("/work/dir", argv, env)

        m_exec.assert_called_once_with(argv[0], argv, env)
        e_bin, e_argv, e_env = m_exec.call_args[0]
        self.assertEqual(e_bin, "/opt/claude/bin/claude")
        self.assertEqual(e_argv, argv)
        self.assertEqual(e_env, env)

    def test_chdir_targets_selected_directory(self) -> None:
        """do_launch changes into the cwd argument."""
        with mock.patch("os.chdir") as m_chdir, mock.patch("os.execvpe"):
            do_launch("/some/project", ["/bin/claude"], {})

        m_chdir.assert_called_once_with("/some/project")

    def test_chdir_happens_before_execvpe(self) -> None:
        """The working directory is switched before the process image is replaced."""
        parent = mock.Mock()
        with (
            mock.patch("os.chdir", parent.chdir),
            mock.patch("os.execvpe", parent.execvpe),
        ):
            do_launch("/dir", ["/bin/claude"], {})

        self.assertEqual(
            [call[0] for call in parent.mock_calls],
            ["chdir", "execvpe"],
        )

    def test_env_forwarded_verbatim_no_os_environ_merge(self) -> None:
        """do_launch passes env straight through -- no os.environ merge, no copy.

        A minimal env lacking common process-env keys (e.g. PATH) must reach
        execvpe unchanged. If do_launch merged os.environ, PATH would appear; it
        must not. The env is even the SAME object (identity), proving there is no
        intervening copy/merge -- that responsibility lives in
        resolve_launch_config.
        """
        minimal_env = {"ONLY_KEY": "only-val"}
        with mock.patch("os.chdir"), mock.patch("os.execvpe") as m_exec:
            do_launch("/dir", ["/bin/claude"], minimal_env)

        passed_env = m_exec.call_args[0][2]
        self.assertEqual(passed_env, {"ONLY_KEY": "only-val"})
        self.assertNotIn("PATH", passed_env)
        self.assertIs(passed_env, minimal_env)


class ResolveThenDoLaunchEndToEndTests(SandboxHomeTestCase):
    """resolve_launch_config -> do_launch carries GH token + model flags to exec."""

    def setUp(self) -> None:
        super().setUp()
        # Workspace.default() honors the poisoned Path.home -> sandbox root.
        self.ws = Workspace.default()
        self.profiles = self.ws.profiles
        self.profiles.create("work", {"model": "claude-opus-4-8"})
        self.ws.tokens.add("work", "tok-xyz")
        # A tmpdir-backed locator so no installed version is required (fallback
        # symlink stands in for the binary path).
        self.locator = BinaryLocator(
            versions_dir=self.home / "versions",
            claude_symlink=self.home / "claude",
        )

    def test_gh_token_and_model_flags_reach_execvpe(self) -> None:
        """A full launch selection flows through resolve -> do_launch to execvpe."""
        proj = self.home / "proj"
        proj.mkdir()
        selections = {
            "profile": "work",
            "github": "ghuser",
            "model": "claude-opus-4-8",
            "directory": str(proj),
        }
        with mock.patch(
            "claudewheel.launch.fetch_gh_token", return_value="gh-live-tok"
        ):
            cwd, argv, env = resolve_launch_config(
                selections,
                {},
                ["--verbose"],
                locator=self.locator,
                profiles=self.profiles,
            )

        with mock.patch("os.chdir") as m_chdir, mock.patch("os.execvpe") as m_exec:
            do_launch(cwd, argv, env)

        # chdir into the selected directory.
        m_chdir.assert_called_once_with(cwd)
        self.assertEqual(cwd, str(proj))

        # execvpe received the exact triple resolve_launch_config produced.
        e_bin, e_argv, e_env = m_exec.call_args[0]
        self.assertEqual(e_bin, argv[0])
        self.assertEqual(e_argv, argv)
        self.assertIs(e_env, env)

        # GH token, OAuth token, and config dir carried end to end.
        self.assertEqual(e_env["GH_TOKEN"], "gh-live-tok")
        self.assertEqual(e_env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-xyz")
        self.assertEqual(
            e_env["CLAUDE_CONFIG_DIR"],
            str(self.sandbox_paths["PROFILES_DIR"] / "work"),
        )

        # Model + default flags carried end to end.
        self.assertIn("--verbose", e_argv)
        self.assertIn("--model", e_argv)
        self.assertEqual(e_argv[e_argv.index("--model") + 1], "claude-opus-4-8")

    def test_resolve_merges_os_environ_do_launch_forwards_it(self) -> None:
        """The os.environ merge is a property of resolve_launch_config.

        A sentinel var set in the process environment must appear in the resolved
        env (because resolve does ``env = dict(os.environ)``) and thus flow
        unchanged through do_launch to execvpe.
        """
        with mock.patch.dict(os.environ, {"CW_SENTINEL_VAR": "sentinel-123"}):
            with mock.patch("claudewheel.launch.fetch_gh_token", return_value=None):
                cwd, argv, env = resolve_launch_config(
                    {"profile": "work"},
                    {},
                    [],
                    locator=self.locator,
                    profiles=self.profiles,
                )

        self.assertEqual(env["CW_SENTINEL_VAR"], "sentinel-123")

        with mock.patch("os.chdir"), mock.patch("os.execvpe") as m_exec:
            do_launch(cwd, argv, env)

        self.assertEqual(m_exec.call_args[0][2]["CW_SENTINEL_VAR"], "sentinel-123")


if __name__ == "__main__":
    unittest.main()
