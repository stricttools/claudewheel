"""``--dry-run`` records; it never touches ``~/.claudewheel/``.

The claim the whole effects migration exists to make: a preview of a
profile-mutating command writes NOTHING and still tells the operator exactly
what it would have written.  These tests drive the real CLI entry point --
argv in, ``main()`` -- against a sandboxed config directory, then assert on
both halves: the sandbox is byte-identical afterwards, and the framework's
would-do log names the files.

They are deliberately end-to-end rather than unit tests of the chokepoint. A
chokepoint that records correctly but is never reached is exactly the failure
mode this file is here to catch.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.profile_data import PROFILE_DATA_DIRNAME, TOKEN_FILE_NAME
from tests.wheelhelpers import SandboxHomeTestCase, write_stub_saferm


def _tree(root: Path) -> dict[str, bytes]:
    """Every regular file under *root*, keyed by relative path."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


class DryRunRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.launcher = Path(self._tmp.name) / "cw"
        (self.launcher / "profiles" / "work").mkdir(parents=True)
        self.settings = self.launcher / "profiles" / "work" / "settings.json"
        self.settings.write_text(
            json.dumps({"permissions": {"deny": ["Bash(bogus:*)"]}}, indent=2)
        )
        patcher = mock.patch.dict(
            "os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(self.launcher)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv: list[str]) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit:
                pass
        return out.getvalue(), err.getvalue()

    def test_reconcile_permissions_dry_run_writes_nothing(self) -> None:
        """The drifted profile is byte-identical after a preview."""
        before = _tree(self.launcher)
        stdout, _ = self._run(["c", "reconcile-permissions", "--dry-run"])
        self.assertEqual(_tree(self.launcher), before)
        # And the deny rule that a real run would prune is still there.
        data = json.loads(self.settings.read_text())
        self.assertIn("Bash(bogus:*)", data["permissions"]["deny"])
        self.assertIn("DRY RUN", stdout)

    def test_reconcile_permissions_dry_run_names_the_write(self) -> None:
        """The would-do log lists the settings file the run would rewrite."""
        stdout, _ = self._run(["c", "reconcile-permissions", "--dry-run"])
        self.assertIn("DRY RUN — no changes were made. Would do:", stdout)
        self.assertIn(f"write: {self.settings}", stdout)

    def test_deploy_hooks_dry_run_writes_nothing(self) -> None:
        """A preview of deploy-hooks creates no scripts directory at all."""
        before = _tree(self.launcher)
        stdout, _ = self._run(["c", "deploy-hooks", "--all", "--dry-run"])
        self.assertEqual(_tree(self.launcher), before)
        self.assertFalse((self.launcher / "scripts").exists())
        self.assertIn("mkdir: ", stdout)
        self.assertIn("chmod: ", stdout)

    def test_read_only_command_never_prompts_and_logs_an_empty_body(self) -> None:
        """A read_only command accepts --dry-run and records nothing.

        Its log is header-only by construction: read-only commands produce no
        recorded effects, so there is nothing for the body to list.
        """
        stdout, stderr = self._run(["c", "versions", "--dry-run"])
        self.assertNotIn("Proceed?", stderr)
        self.assertIn("DRY RUN — no changes were made. Would do:", stdout)
        self.assertNotIn("write:", stdout)


class DryRunNarrationIsConditionalTests(SandboxHomeTestCase):
    """A preview must not narrate itself in the indicative past tense.

    The framework's would-do log is always correct, but it is printed AFTER the
    handler's own output. A handler that prints "Profile 'x' deleted." above it
    has told the reader -- and an agent reading the first line of stdout -- that
    the mutation happened. ``stats``, ``mv``, ``import``, ``install`` and
    ``reconcile`` already switch verb on the preview flag; every other
    mutating handler must too.

    Each case asserts BOTH halves: the indicative claim is absent, and the
    conditional form the handler should print instead is present. Asserting
    only the absence would pass on a handler that printed nothing at all.
    """

    def run_cli(self, argv: list[str]) -> tuple[str, str, int]:
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                code = (
                    e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                )
        return out.getvalue(), err.getvalue(), code

    def seed_profile(self, name: str) -> None:
        from claudewheel.tokens import TokenExpiryDisposition, plan_by_key

        store = self.ws.profiles
        store.create(name, {"permissions": {"allow": [], "deny": [], "ask": []}})
        # Deletion delegates to saferm; the stand-in goes where the handler
        # looks first, so the preview records a real composed invocation.
        write_stub_saferm(self.ws.root / "bin")
        store.data_for(name).write_token(
            "TOKEN", expiry=TokenExpiryDisposition.TTL, plan=plan_by_key("max-20x")
        )

    # -- the three confirmed in the report --------------------------------

    def test_profile_delete(self) -> None:
        self.seed_profile("work")
        out, err, code = self.run_cli(
            [
                "c",
                "profile",
                "delete",
                "work",
                "--no-force-delete",
                "--no-force-delete-data",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Profile 'work' deleted.", out)
        self.assertIn("Would delete profile 'work'", out)

    def test_profile_delete_records_the_delegation_and_removes_nothing(self) -> None:
        """The chokepoint's whole claim, for the one command that hands a
        directory to another program: the invocation is written to the would-do
        log and the directory is still there afterwards, token included."""
        self.seed_profile("work")
        target = self.ws.profiles.path_for("work")
        out, err, code = self.run_cli(
            [
                "c",
                "profile",
                "delete",
                "work",
                "--no-force-delete",
                "--no-force-delete-data",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        # The composed invocation is named in full, flags and all.
        self.assertIn("run: ", out)
        self.assertIn("saferm delete --on-error abort", out)
        self.assertIn("--no-update-git-index", out)
        self.assertIn(str(target), out)
        # And nothing ran: the profile, its settings and its token survive.
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "settings.json").is_file())
        self.assertEqual(self.ws.profiles.data_for("work").token(), "TOKEN")
        # No handle is invented for an archival that did not happen.
        self.assertNotIn("Restore it with", out)

    def test_profile_rename(self) -> None:
        self.seed_profile("alpha")
        out, err, code = self.run_cli(
            ["c", "profile", "rename", "alpha", "beta", "--dry-run"]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Renamed profile", out)
        self.assertIn("Would rename profile 'alpha' -> 'beta'.", out)

    def test_deploy_hooks(self) -> None:
        out, err, code = self.run_cli(["c", "deploy-hooks", "--all", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("\ncreated: ", "\n" + out)
        self.assertIn("would create: ", out)

    # -- the eight the report listed as unverified -------------------------

    def test_uninstall(self) -> None:
        versions = self.home / ".local/share/claude/versions"
        versions.mkdir(parents=True)
        (versions / "9.9.9").write_text("binary")
        out, err, code = self.run_cli(["c", "uninstall", "9.9.9", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Uninstalled 9.9.9", out)
        self.assertIn("Would uninstall 9.9.9", out)
        self.assertTrue((versions / "9.9.9").is_file())

    def test_reset_options(self) -> None:
        options = self.sandbox_paths["OPTIONS_FILE"]
        out, err, code = self.run_cli(["c", "reset-options", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertNotIn(f"Deleted {options}", out)
        self.assertIn(f"Would delete {options}", out)
        self.assertTrue(options.is_file())

    def test_migrate(self) -> None:
        """`migrate` hard-coded dry_run=False, so its module never saw the preview."""
        self.seed_profile("src")
        self.seed_profile("dst")
        projects = self.sandbox_paths["SHARED_DIR"] / "projects" / "-tmp-proj"
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "abc.jsonl").write_text("{}\n")
        out, err, code = self.run_cli(["c", "migrate", "src", "dst", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("DRY RUN", out.replace("DRY RUN — no changes", ""))
        self.assertIn("[migrate] DRY RUN", out)

    def test_permission_add(self) -> None:
        self.seed_profile("work")
        out, err, code = self.run_cli(
            [
                "c",
                "permission",
                "add",
                "allow",
                "Bash",
                "--profile",
                "work",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("work: added", out)
        self.assertIn("work: would add Bash to allow", out)

    def test_permission_remove(self) -> None:
        self.seed_profile("work")
        settings = self.sandbox_paths["PROFILES_DIR"] / "work" / "settings.json"
        settings.write_text(
            json.dumps({"permissions": {"allow": ["Bash"], "deny": [], "ask": []}})
        )
        out, err, code = self.run_cli(
            [
                "c",
                "permission",
                "remove",
                "allow",
                "Bash",
                "--profile",
                "work",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("work: removed", out)
        self.assertIn("work: would remove Bash from allow", out)

    def test_profile_fix_auth(self) -> None:
        self.seed_profile("work")
        creds = self.sandbox_paths["PROFILES_DIR"] / "work" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"rateLimitTier": "max"}}))
        out, err, code = self.run_cli(["c", "profile", "fix-auth", "work", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Removed session credentials", out)
        self.assertIn("Would remove session credentials", out)

    def test_profile_set_plan(self) -> None:
        self.seed_profile("work")
        out, err, code = self.run_cli(
            ["c", "profile", "set-plan", "work", "pro", "--dry-run"]
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Declared plan", out)
        self.assertIn("Would declare plan Pro for 'work'", out)
        # The preview recorded the entry write instead of performing it: the
        # profile still declares the plan it was seeded with.
        self.assertEqual(
            self.ws.profiles.data_for("work").tier(),
            ("default_claude_max_20x", "max"),
        )

    def test_profile_create_summary(self) -> None:
        """One site the report did not list, found by sweeping the siblings.

        ``profile create`` drives an interactive wizard, so it cannot be
        exercised through ``main()`` without a real terminal -- but its summary
        is built by ``wizard.create_profile``, which is callable directly and
        is where the tense lives.
        """
        from claudewheel.wizard import WizardResult, create_profile

        def wiz(name: str) -> WizardResult:
            return WizardResult(
                name=name,
                config_dir=str(self.ws.profiles.path_for(name)),
                clone_from=None,
                wire_hooks=False,
                symlink_shared=False,
                disable_recap=False,
                cleanup_10y=False,
                disable_memory=False,
                disable_attribution=False,
            )

        summary = create_profile(self.ws, wiz("fresh"), previewing=True)
        self.assertNotIn("Created profile", summary[0])
        self.assertEqual(summary[0], "Would create profile 'fresh':")

        summary = create_profile(self.ws, wiz("real"))
        self.assertEqual(summary[0], "Created profile 'real':")

    # -- the two that were already correct, pinned so they stay that way ----

    def test_mv_was_already_conditional(self) -> None:
        proj = self.home / "proj"
        proj.mkdir()
        out, err, code = self.run_cli(
            [
                "c",
                "mv",
                str(proj),
                str(self.home / "proj2"),
                "--no-post-hoc",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("[mv] DRY RUN", out)

    def test_import_was_already_conditional(self) -> None:
        source = self.home / "external"
        (source / "projects").mkdir(parents=True)
        target = self.home / "proj"
        target.mkdir()
        out, err, code = self.run_cli(
            [
                "c",
                "import",
                str(source),
                "--from",
                "/elsewhere/proj",
                "--to",
                str(target),
                "--no-reid",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("[import] DRY RUN", out)

    def test_stats_was_already_conditional(self) -> None:
        out, err, code = self.run_cli(["c", "stats", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("[stats] DRY RUN", out)


class RealConfigDirIsAbsentTests(unittest.TestCase):
    """The suite cannot see the developer's real ``~/.claudewheel/``.

    stricttest repoints ``HOME`` at a throwaway directory before any conftest
    module is imported, so ``Workspace.default()`` -- which derives every path
    from ``Path.home()`` -- resolves into the sandbox. This pins that: if the
    floor is ever removed or misconfigured, the whole suite's blast radius goes
    back to the real profile store, and this is the test that says so.
    """

    def test_home_is_a_throwaway(self) -> None:
        home = Path(os.environ["HOME"])
        self.assertNotEqual(home, Path("/home/m"), "HOME is the real home")
        self.assertIn("stricttest", str(home).lower())

    def test_workspace_default_resolves_inside_the_throwaway_home(self) -> None:
        from claudewheel.workspace import Workspace

        env = dict(os.environ)
        env.pop("CLAUDEWHEEL_CONFIG_DIR", None)
        with mock.patch.dict("os.environ", env, clear=True):
            ws = Workspace.default()
        self.assertTrue(
            str(ws.root).startswith(os.environ["HOME"]),
            f"workspace root {ws.root} escapes the throwaway HOME",
        )

    def test_the_real_per_profile_token_files_are_unreachable(self) -> None:
        """No profile reachable from the sandbox HOME carries a stored token.

        A developer's OAuth tokens live one per profile, at
        ``<profile_dir>/.claudewheel/token.json``, so those files are what a
        leaked HOME would expose. The glob is proved against a planted file of
        exactly that shape first: a pattern that matched nothing would make the
        assertion below vacuous, which is how the previous version of this test
        (pointed at a central ``tokens.json`` that no longer exists anywhere)
        stopped protecting anything.
        """
        pattern = f"profiles/*/{PROFILE_DATA_DIRNAME}/{TOKEN_FILE_NAME}"
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "profiles" / "work" / PROFILE_DATA_DIRNAME
            planted.mkdir(parents=True)
            (planted / TOKEN_FILE_NAME).write_text("{}")
            self.assertEqual(
                list(Path(tmp).glob(pattern)),
                [planted / TOKEN_FILE_NAME],
                "the pattern no longer describes where tokens are stored",
            )

        home = Path(os.environ["HOME"])
        found = sorted(str(p) for p in (home / ".claudewheel").glob(pattern))
        self.assertEqual(found, [], f"sandbox HOME exposes real token files: {found}")
        # The built-in default profile keeps its data in the same place.
        self.assertFalse(
            (home / ".claude" / PROFILE_DATA_DIRNAME / TOKEN_FILE_NAME).exists()
        )


if __name__ == "__main__":
    unittest.main()
