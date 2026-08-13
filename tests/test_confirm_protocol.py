"""The framework confirm protocol, end to end through ``cli.main()``.

strictcli prompts before dispatching a command that declares itself
``consequential`` -- and for no other command. In claudewheel that is
``profile delete`` plus the two spellings of the exact reconciliation,
``reconcile-permissions`` and ``patch-profiles`` (see
``tests/test_effects_binding.py`` for the reviewed set and the reasoning).
These tests drive the real CLI entry point so they pin the protocol as a user
meets it, not as the registry declares it:

- each consequential command refuses on a non-interactive stdin with the
  contract's pinned message, and destroys nothing;
- ``--approve-consequential`` is the only thing that consents, and it works;
- ``--dry-run`` suppresses the gate and records instead of writing;
- routine mutating commands -- including ``launch``, which every session
  starts with -- run straight through with no prompt and nothing appended to
  their argv.

That last point is the one this file exists for. Under the superseded regime
the prompt was INFERRED from ``mutating``, which put a blind ``Proceed? [y/N]``
in front of every bare ``claudewheel``; claudewheel worked around it by
rewriting its own argv to auto-supply the skip flag to ``launch``. The
workaround is gone and nothing replaced it: ``launch`` is simply not
consequential.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

from claudewheel import cli
from claudewheel.tokens import TokenExpiryDisposition, plan_by_key
from claudewheel.workspace import Workspace
from tests.wheelhelpers import SandboxHomeTestCase

# Contract §12.6. Pinned verbatim: a consumer that stops matching this string
# has stopped being refused for the reason it thinks it is.
NON_INTERACTIVE = (
    "error: stdin is not interactive; pass --approve-consequential to confirm"
)


class _CliCase(SandboxHomeTestCase):
    """A sandboxed home plus a runner for the real CLI entry point."""

    _SETTINGS: dict[str, Any] = {"permissions": {"allow": [], "deny": [], "ask": []}}

    def setUp(self) -> None:
        super().setUp()
        self.store = Workspace.default().profiles
        self.profiles_dir = self.sandbox_paths["PROFILES_DIR"]

    def run_cli(self, argv: list[str]) -> tuple[str, str, int]:
        """Run cli.main() with argv and return (stdout, stderr, exit code)."""
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
        """Create a fully registered profile: dir, settings, options entry, token."""
        self.store.create(name, dict(self._SETTINGS))
        self.store.data_for(name).write_token(
            "TOKEN", expiry=TokenExpiryDisposition.TTL, plan=plan_by_key("max-20x")
        )


class ConsequentialProfileDeleteTests(_CliCase):
    """`profile delete` is the one command the framework interrupts."""

    # --force-delete and --force-delete-data are bools with no default, so
    # strictcli requires an explicit --flag/--no-flag for each. The confirm
    # gate runs after parsing, so these are the minimum tokens that reach it.
    DELETE = [
        "c",
        "profile",
        "delete",
        "work",
        "--no-force-delete",
        "--no-force-delete-data",
    ]

    def test_non_tty_refuses_with_the_pinned_message_and_deletes_nothing(self) -> None:
        self.seed_profile("work")
        _, err, code = self.run_cli(self.DELETE)

        self.assertNotEqual(code, 0)
        self.assertIn(NON_INTERACTIVE, err)
        # The refusal is BEFORE dispatch: the profile is entirely intact.
        self.assertTrue((self.profiles_dir / "work" / "settings.json").is_file())

    def test_approve_consequential_consents_and_the_delete_runs(self) -> None:
        self.seed_profile("work")
        out, err, code = self.run_cli(self.DELETE + ["--approve-consequential"])

        self.assertEqual(code, 0, err)
        self.assertNotIn(NON_INTERACTIVE, err)
        self.assertIn("deleted", out)
        self.assertFalse((self.profiles_dir / "work").exists())

    def test_dry_run_suppresses_the_gate_and_writes_nothing(self) -> None:
        """--dry-run is a preview, so it neither prompts nor refuses.

        Contract §8.1.1: the gate fires only when --dry-run was NOT passed.
        A preview that had to be consented to would be unusable as the way to
        find out what the real run would do.
        """
        self.seed_profile("work")
        out, err, code = self.run_cli(self.DELETE + ["--dry-run"])

        self.assertEqual(code, 0, err)
        self.assertNotIn(NON_INTERACTIVE, err)
        self.assertNotIn("Proceed?", err)
        # Recorded, not performed: the would-do log names the removal and the
        # profile is still on disk with its settings and its token entry.
        self.assertIn("would", out.lower())
        self.assertTrue((self.profiles_dir / "work" / "settings.json").is_file())
        self.assertEqual(self.store.data_for("work").token(), "TOKEN")


class ConsequentialReconcileTests(_CliCase):
    """The exact reconciliation is gated under both of its names.

    ``reconcile-permissions`` and ``patch-profiles`` are two spellings of one
    operation (both delegate to ``reconcile.run_reconcile``). It rewrites every
    managed profile's ``settings.json`` and ``shared-settings.json`` to EXACTLY
    canonical, pruning hand-authored permission rules, hook entries and
    ``disallowedTools`` drift, with nothing backed up and nothing that
    reconstructs them. The gate is the framework's now; it used to be the
    hand-rolled ``--dry-run``/``--apply`` pair.
    """

    NAMES = ("reconcile-permissions", "patch-profiles")

    def test_each_refuses_on_a_non_interactive_stdin(self) -> None:
        for name in self.NAMES:
            with self.subTest(command=name):
                _, err, code = self.run_cli(["c", name])
                self.assertNotEqual(code, 0)
                self.assertIn(NON_INTERACTIVE, err)

    def test_each_runs_with_approve_consequential(self) -> None:
        for name in self.NAMES:
            with self.subTest(command=name):
                _, err, code = self.run_cli(["c", name, "--approve-consequential"])
                self.assertEqual(code, 0, err)
                self.assertNotIn(NON_INTERACTIVE, err)

    def test_dry_run_suppresses_the_gate(self) -> None:
        """The preview must not need consent -- it is how you decide to consent."""
        for name in self.NAMES:
            with self.subTest(command=name):
                _, err, code = self.run_cli(["c", name, "--dry-run"])
                self.assertEqual(code, 0, err)
                self.assertNotIn(NON_INTERACTIVE, err)
                self.assertNotIn("Proceed?", err)


class RoutineMutatingCommandsNeverPromptTests(_CliCase):
    """Everything else is mutating-but-routine and runs straight through."""

    def test_stats_runs_without_a_prompt(self) -> None:
        out, err, code = self.run_cli(["c", "stats"])

        self.assertEqual(code, 0, err)
        self.assertNotIn(NON_INTERACTIVE, err)
        self.assertNotIn("Proceed?", err)
        self.assertIn("[stats] done", out)

    def test_bare_launch_prompts_for_nothing(self) -> None:
        """`claudewheel` with no arguments is the invocation every session starts with.

        _inject_launch turns it into `c launch`; the framework must then
        dispatch it with no prompt and no refusal, without claudewheel
        appending a skip flag to its own argv.
        """
        launch_mock = mock.MagicMock(return_value=0)
        with mock.patch.object(cli, "_do_launch_sequence", launch_mock):
            _, err, code = self.run_cli(["c", "-p", "hello"])

        self.assertEqual(code, 0, err)
        self.assertNotIn(NON_INTERACTIVE, err)
        self.assertNotIn("Proceed?", err)
        launch_mock.assert_called_once()


class InjectLaunchStepsOverTheReservedQuartetTests(SandboxHomeTestCase):
    """_inject_launch must not swallow a framework-reserved flag.

    The quartet may appear before the command token, so the injection walks
    past it -- otherwise `c --dry-run stats` would become
    `c launch --dry-run stats` and preview the TUI instead of the stats
    cleanup. Membership matters: the skip flag is --approve-consequential now,
    and --yes is not reserved by anything.
    """

    def test_each_quartet_member_is_stepped_over(self) -> None:
        for flag in ("--dry-run", "--approve-consequential", "--quiet", "--verbose"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    cli._inject_launch(["c", flag, "stats"]), ["c", flag, "stats"]
                )
                self.assertEqual(cli._inject_launch(["c", flag]), ["c", flag, "launch"])

    def test_yes_is_not_a_reserved_name(self) -> None:
        """--yes owns no framework flag any more, so it routes like any token.

        It is still banned as a consumer flag NAME (pinned in
        tests/test_effects_binding.py), so this argv is a user error either
        way -- but the injection must treat it as an unrecognized leading
        token and hand it to `launch`, not silently walk past it as though
        the framework were going to consume it.
        """
        self.assertEqual(
            cli._inject_launch(["c", "--yes", "stats"]),
            ["c", "launch", "--yes", "stats"],
        )
