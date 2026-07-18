"""Tests for the deploy-hooks CLI command."""

from __future__ import annotations

import io
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claudewheel import cli
from claudewheel.hook_scripts import HOOK_SCRIPTS


class DeployHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # main() builds Workspace.default(), which reads CLAUDEWHEEL_CONFIG_DIR;
        # point it at a sandbox so the deploy-hooks handler writes ws.scripts_dir
        # under the tmp root, never the real ~/.claudewheel.
        self._launcher = Path(self._tmp.name) / "cw"
        self.scripts_dir = self._launcher / "scripts"
        self._env_patch = mock.patch.dict(
            "os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(self._launcher)}
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _run_deploy(self, argv: list[str]) -> tuple[str, str, bool]:
        """Run deploy-hooks with the given argv.

        Returns (stdout, stderr, exited).
        """
        out = io.StringIO()
        err = io.StringIO()
        exited = False
        with (
            mock.patch("sys.argv", argv),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            try:
                cli.main()
            except SystemExit:
                exited = True
        return out.getvalue(), err.getvalue(), exited

    def test_deploy_all_creates_all_scripts(self) -> None:
        """deploy-hooks --all creates every known script."""
        stdout, _, _ = self._run_deploy(["c", "deploy-hooks", "--all"])

        for name in HOOK_SCRIPTS:
            dest = self.scripts_dir / name
            self.assertTrue(dest.exists(), f"{name} should exist after --all")
            self.assertIn(f"created: {dest}", stdout)

    def test_deploy_single_creates_one_script(self) -> None:
        """deploy-hooks <name> creates only the named script."""
        name = "hook-timestamp"
        stdout, _, _ = self._run_deploy(["c", "deploy-hooks", name])

        dest = self.scripts_dir / name
        self.assertTrue(dest.exists(), f"{name} should exist")
        self.assertIn(f"created: {dest}", stdout)
        # Other scripts should not exist
        for other in HOOK_SCRIPTS:
            if other != name:
                self.assertFalse(
                    (self.scripts_dir / other).exists(),
                    f"{other} should NOT exist when deploying only {name}",
                )

    def test_deploy_skips_existing(self) -> None:
        """deploy-hooks <name> skips if the script already exists."""
        name = "hook-timestamp"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        existing = self.scripts_dir / name
        existing.write_text("existing content")

        stdout, _, _ = self._run_deploy(["c", "deploy-hooks", name])

        self.assertIn(f"already exists: {existing}", stdout)
        # Content must be preserved (not overwritten)
        self.assertEqual(existing.read_text(), "existing content")

    def test_deploy_unknown_name_errors(self) -> None:
        """deploy-hooks with an unknown script name prints an error."""
        _, stderr, exited = self._run_deploy(["c", "deploy-hooks", "nonexistent"])

        self.assertTrue(exited, "should exit with error")
        self.assertIn("unknown hook script", stderr)
        self.assertIn("nonexistent", stderr)

    def test_scripts_are_executable(self) -> None:
        """Deployed scripts have the executable bit set."""
        self._run_deploy(["c", "deploy-hooks", "--all"])

        for name in HOOK_SCRIPTS:
            dest = self.scripts_dir / name
            mode = dest.stat().st_mode
            self.assertTrue(
                mode & stat.S_IXUSR,
                f"{name} should be owner-executable (mode={oct(mode)})",
            )

    def test_no_args_no_all_errors(self) -> None:
        """deploy-hooks with neither a name nor --all prints an error."""
        _, stderr, exited = self._run_deploy(["c", "deploy-hooks"])

        self.assertTrue(exited, "should exit with error")
        self.assertIn("provide a script name or --all", stderr)

    def test_name_and_all_mutually_exclusive(self) -> None:
        """deploy-hooks <name> --all is rejected."""
        _, stderr, exited = self._run_deploy(
            ["c", "deploy-hooks", "hook-timestamp", "--all"]
        )

        self.assertTrue(exited, "should exit with error")
        self.assertIn("mutually exclusive", stderr)

    def test_creates_scripts_dir_if_missing(self) -> None:
        """SCRIPTS_DIR is created automatically when it doesn't exist."""
        self.assertFalse(self.scripts_dir.exists())
        self._run_deploy(["c", "deploy-hooks", "hook-timestamp"])
        self.assertTrue(self.scripts_dir.exists())

    def test_script_content_matches_registry(self) -> None:
        """Deployed script content matches the registry template."""
        name = "hook-timestamp"
        self._run_deploy(["c", "deploy-hooks", name])

        dest = self.scripts_dir / name
        self.assertEqual(dest.read_text(), HOOK_SCRIPTS[name])

    def test_force_overwrite_overwrites_existing(self) -> None:
        """deploy-hooks --force-overwrite overwrites an existing script."""
        name = "hook-timestamp"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        existing = self.scripts_dir / name
        existing.write_text("old content")

        stdout, _, _ = self._run_deploy(
            ["c", "deploy-hooks", "--force-overwrite", name]
        )

        self.assertIn(f"overwritten: {existing}", stdout)
        # Content must be replaced with the registry template
        self.assertEqual(existing.read_text(), HOOK_SCRIPTS[name])


class HookBlockUnsafeCommandsTests(unittest.TestCase):
    """Tests for the hook-block-unsafe-commands script content."""

    def test_script_in_registry(self) -> None:
        """hook-block-unsafe-commands exists in HOOK_SCRIPTS."""
        self.assertIn("hook-block-unsafe-commands", HOOK_SCRIPTS)

    def test_script_valid_bash_syntax(self) -> None:
        """The script passes bash -n (syntax check)."""
        import subprocess
        import tempfile
        import os

        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write(script)
            f.flush()
            try:
                result = subprocess.run(
                    ["bash", "-n", f.name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(
                    result.returncode, 0, f"bash -n failed: {result.stderr}"
                )
            finally:
                os.unlink(f.name)

    def test_script_starts_with_shebang(self) -> None:
        """Script begins with #!/usr/bin/env bash."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_script_uses_jq_to_extract_fields(self) -> None:
        """Script uses jq to extract tool_name and tool_input.command."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn(".tool_name", script)
        self.assertIn(".tool_input.command", script)

    def test_script_exits_early_for_non_bash_tool(self) -> None:
        """Script has an early exit for non-Bash tool names."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn('"Bash"', script)
        # The pattern: [[ "$tool_name" != "Bash" ]] && exit 0
        self.assertIn('!= "Bash"', script)

    def test_script_matches_git_add_patterns(self) -> None:
        """Script contains grep patterns that match git add variants."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("git\\s+add", script)
        # Should reference the replacement
        self.assertIn("safegit commit", script)

    def test_script_matches_git_stash(self) -> None:
        """Script contains a pattern for git stash."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("git\\s+stash", script)

    def test_script_matches_git_restore(self) -> None:
        """Script contains a pattern for git restore."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("git\\s+restore", script)

    def test_script_matches_git_checkout_doubledash(self) -> None:
        """Script contains a pattern for 'git checkout -- ' (destructive form)."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("git\\s+checkout\\s+--\\s", script)

    def test_script_matches_rm(self) -> None:
        """Script contains a pattern for rm with arguments."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        # The pattern checks for 'rm' followed by whitespace or end-of-string.
        self.assertIn("rm(\\s|$)", script)
        # Should reference saferm
        self.assertIn("saferm delete", script)

    def test_script_deny_json_format(self) -> None:
        """Script outputs the correct deny JSON structure."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("hookSpecificOutput", script)
        self.assertIn("permissionDecision", script)
        self.assertIn("deny", script)
        self.assertIn("permissionDecisionReason", script)

    def test_script_extracts_agent_id(self) -> None:
        """The tier-aware script extracts agent_id to tell subagents apart."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn(".agent_id", script)
        # HARD_DENY rules branch on a non-empty agent_id (subagent vs main).
        self.assertIn('[[ -n "$agent_id" ]]', script)

    def test_script_has_escalate_subagent_only_branches(self) -> None:
        """ESCALATE rules deny only subagents (guarded by non-empty agent_id)."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("ESCALATE, subagent-only", script)
        # The subagent report/escalate tails must be present in the sources.
        self.assertIn(
            "report to your parent agent why you attempted this command", script
        )
        self.assertIn("Explain in detail to your parent agent", script)

    def test_script_is_generated_from_model(self) -> None:
        """The blocker source is generated, not hand-maintained."""
        script = HOOK_SCRIPTS["hook-block-unsafe-commands"]
        self.assertIn("GENERATED by claudewheel.guardrail", script)

    def test_script_deployed_by_all(self) -> None:
        """deploy-hooks --all creates the hook-block-unsafe-commands script."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        launcher = Path(tmp.name) / "cw"
        scripts_dir = launcher / "scripts"
        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(launcher)}),
            mock.patch("sys.argv", ["c", "deploy-hooks", "--all"]),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        dest = scripts_dir / "hook-block-unsafe-commands"
        self.assertTrue(
            dest.exists(), "hook-block-unsafe-commands should be deployed by --all"
        )
        self.assertEqual(dest.read_text(), HOOK_SCRIPTS["hook-block-unsafe-commands"])


class HookAdviseCommandsTests(unittest.TestCase):
    """Tests for the generated hook-advise-commands (PostToolUse) script."""

    def test_script_in_registry(self) -> None:
        self.assertIn("hook-advise-commands", HOOK_SCRIPTS)

    def test_script_valid_bash_syntax(self) -> None:
        import subprocess
        import tempfile
        import os

        script = HOOK_SCRIPTS["hook-advise-commands"]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write(script)
            f.flush()
            try:
                result = subprocess.run(
                    ["bash", "-n", f.name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(
                    result.returncode, 0, f"bash -n failed: {result.stderr}"
                )
            finally:
                os.unlink(f.name)

    def test_script_starts_with_shebang(self) -> None:
        script = HOOK_SCRIPTS["hook-advise-commands"]
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_script_is_generated_from_model(self) -> None:
        script = HOOK_SCRIPTS["hook-advise-commands"]
        self.assertIn("GENERATED by claudewheel.guardrail", script)

    def test_script_uses_jq_to_extract_fields(self) -> None:
        script = HOOK_SCRIPTS["hook-advise-commands"]
        self.assertIn(".tool_name", script)
        self.assertIn(".tool_input.command", script)

    def test_script_exits_early_for_non_bash_tool(self) -> None:
        script = HOOK_SCRIPTS["hook-advise-commands"]
        self.assertIn('!= "Bash"', script)

    def test_script_posttooluse_additional_context_format(self) -> None:
        """Advice is emitted as a PostToolUse additionalContext payload."""
        script = HOOK_SCRIPTS["hook-advise-commands"]
        self.assertIn("PostToolUse", script)
        self.assertIn("additionalContext", script)

    def test_script_matches_kill(self) -> None:
        script = HOOK_SCRIPTS["hook-advise-commands"]
        self.assertIn("p?kill", script)
        self.assertIn("graceful stop", script)

    def test_script_deployed_by_all(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        launcher = Path(tmp.name) / "cw"
        scripts_dir = launcher / "scripts"
        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"CLAUDEWHEEL_CONFIG_DIR": str(launcher)}),
            mock.patch("sys.argv", ["c", "deploy-hooks", "--all"]),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            try:
                cli.main()
            except SystemExit:
                pass
        dest = scripts_dir / "hook-advise-commands"
        self.assertTrue(
            dest.exists(), "hook-advise-commands should be deployed by --all"
        )
        self.assertEqual(dest.read_text(), HOOK_SCRIPTS["hook-advise-commands"])


class HookPatternDocumentation(unittest.TestCase):
    """Documented pattern-match expectations for hook-block-unsafe-commands.

    These tests document which commands SHOULD match forbidden patterns
    and which should NOT. Since the patterns are bash grep -qE expressions,
    we verify them through documented expectations (not execution).
    """

    def test_git_add_should_match(self) -> None:
        """Commands that should be caught by the git add pattern."""
        # The pattern: (^|[;&|]|&&|\|\|)\s*git\s+add\s+(-[AuU]|--all|\.)
        pattern = r"(^|[;&|]|&&|\|\|)\s*git\s+add\s+(-[AuU]|--all|\.)"
        # Should match
        self.assertRegex("git add .", pattern)
        self.assertRegex("git add -A", pattern)
        self.assertRegex("git add --all", pattern)
        self.assertRegex("git add -u", pattern)
        self.assertRegex("cd foo && git add .", pattern)
        self.assertRegex("echo done; git add -A", pattern)

    def test_git_add_should_not_match_specific_files(self) -> None:
        """git add <specific-file> should NOT be caught (safegit might not be needed)."""
        import re

        pattern = r"(^|[;&|]|&&|\|\|)\s*git\s+add\s+(-[AuU]|--all|\.)"
        # Should NOT match -- specific file
        self.assertIsNone(re.search(pattern, "git add myfile.txt"))
        self.assertIsNone(re.search(pattern, "git add src/main.py"))

    def test_git_checkout_branch_should_not_match(self) -> None:
        """git checkout <branch> should NOT be caught."""
        import re

        pattern = r"(^|[;&|]|&&|\|\|)\s*git\s+checkout\s+--\s"
        # Should NOT match -- branch switching
        self.assertIsNone(re.search(pattern, "git checkout main"))
        self.assertIsNone(re.search(pattern, "git checkout -b new-branch"))
        self.assertIsNone(re.search(pattern, "git checkout feature/xyz"))

    def test_git_checkout_doubledash_should_match(self) -> None:
        """git checkout -- <file> should be caught."""
        pattern = r"(^|[;&|]|&&|\|\|)\s*git\s+checkout\s+--\s"
        # Should match
        self.assertRegex("git checkout -- file.txt", pattern)
        self.assertRegex("git checkout -- .", pattern)
        self.assertRegex("cd repo && git checkout -- src/main.py", pattern)

    def test_git_stash_all_forms_match(self) -> None:
        """All git stash subcommands should match."""
        pattern = r"(^|[;&|]|&&|\|\|)\s*git\s+stash"
        self.assertRegex("git stash", pattern)
        self.assertRegex("git stash push", pattern)
        self.assertRegex("git stash pop", pattern)
        self.assertRegex("git stash drop", pattern)
        self.assertRegex("cd foo && git stash", pattern)

    def test_git_restore_matches(self) -> None:
        """git restore in any form should match."""
        pattern = r"(^|[;&|]|&&|\|\|)\s*git\s+restore"
        self.assertRegex("git restore file.txt", pattern)
        self.assertRegex("git restore --staged file.txt", pattern)
        self.assertRegex("git restore .", pattern)

    def test_rm_with_args_matches(self) -> None:
        """rm with arguments should match."""
        pattern = r"(^|[;&|]|&&|\|\|)\s*rm\s"
        self.assertRegex("rm file.txt", pattern)
        self.assertRegex("rm -rf dir/", pattern)
        self.assertRegex("rm -f old.log", pattern)
        self.assertRegex("cd /tmp && rm leftover.txt", pattern)

    def test_rm_in_variable_does_not_match(self) -> None:
        """Strings containing 'rm' as part of another word should not match."""
        import re

        pattern = r"(^|[;&|]|&&|\|\|)\s*rm\s"
        # Should NOT match -- rm is part of a word
        self.assertIsNone(re.search(pattern, "echo inform the user"))
        self.assertIsNone(re.search(pattern, "firmware update"))


if __name__ == "__main__":
    unittest.main()
