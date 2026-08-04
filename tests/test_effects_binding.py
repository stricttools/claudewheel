"""Command classification and effects binding, pinned.

Three guarantees this file holds:

1. **Every command carries the classification it was deliberately given.**
   strictcli makes ``effect=`` mandatory, so a missing one is a registration
   error and can never reach here -- but a *wrong* one is silent. The table
   below is the reviewed judgement, and changing a row has to be a deliberate
   edit to this file. ``read_only`` means the command performs no user-visible
   or consequential mutation: it may read the filesystem, issue declared reads
   over the network, and nothing else.

2. **Every command handler is bound to the effects chokepoint.** Without the
   binding a handler's effects execute in every mode, including ``--dry-run``
   -- silently, since nothing else would fail.  The test walks the registered
   commands rather than the source, so a command added later without going
   through ``cli._bind`` fails here.

3. **No command redeclares a framework-reserved flag name.** strictcli bans
   dry-run/yes/quiet/verbose at every level, so a collision is a registration
   error; this pins that claudewheel's five former ``--dry-run`` flags and
   ``reconcile-permissions``' ``--apply`` stay gone rather than reappearing
   under a near-miss spelling.
"""

import unittest
from typing import Any

from claudewheel.binaries import BinaryLocator
from claudewheel.cli import _build_app
from claudewheel.workspace import Workspace


# Reviewed classification. The comment on each mutating row names the mutation.
EFFECTS = {
    # reads profiles, tokens, disk usage and settings, and prints a report;
    # the one write it can make (pruning stale inode entries) is repair of its
    # own bookkeeping file, not a user-visible change
    "health": "read_only",
    # spawns $EDITOR on the config directory
    "config": "mutating",
    # lists the installed version binaries
    "versions": "read_only",
    # downloads a release binary from the Claude Code bucket and installs it
    "install": "mutating",
    # deletes an installed version binary
    "uninstall": "mutating",
    # deletes options.json
    "reset-options": "mutating",
    # prints the current selections, theme and recent directories
    "show": "read_only",
    # moves session data files between profile directories
    "migrate": "mutating",
    # deletes the legacy sentinels directory from the shared store
    "stats": "mutating",
    # renames a project directory and rewrites every profile's session data
    "mv": "mutating",
    # copies session data, artifacts and the paste cache into the shared store
    "import": "mutating",
    # writes hook scripts into ~/.claudewheel/scripts/ and chmods them 0755
    "deploy-hooks": "mutating",
    # rewrites every managed profile's settings.json to exact canonical
    "patch-profiles": "mutating",
    # the same reconciliation, plus shared-settings.json
    "reconcile-permissions": "mutating",
    # persists the selections and replaces this process with the client binary
    "launch": "mutating",
    # creates the profile directory, symlinks the shared store, drives an
    # interactive OAuth login and writes the resulting token
    "profile.create": "mutating",
    # deletes the profile directory, its token entry and its options entry
    "profile.delete": "mutating",
    # gathers and prints one profile's configuration and auth status
    "profile.show": "read_only",
    # renames the profile directory, its token key and its session data
    "profile.rename": "mutating",
    # removes shadowing session credentials or a stale token entry
    "profile.fix-auth": "mutating",
    # probes each stored token against the Anthropic API and prints the verdict
    "profile.check-tokens": "read_only",
    # writes a rule into a profile's settings.json
    "permission.add": "mutating",
    # deletes a rule from a profile's settings.json
    "permission.remove": "mutating",
    # prints the rules
    "permission.list": "read_only",
}

RESERVED_QUARTET = {"dry-run", "yes", "quiet", "verbose"}


def _walk(app: Any) -> dict[str, Any]:
    """Map dotted command path -> Command for every registered command."""
    found: dict[str, Any] = {}

    def visit(container: Any, prefix: str) -> None:
        registry = getattr(container, "_commands", None) or container.commands
        for name, cmd in registry.items():
            found[prefix + name] = cmd
        for name, group in container._groups.items():
            visit(group, prefix + name + ".")

    visit(app, "")
    return found


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Workspace.default() and BinaryLocator.default() are pure value
        # construction -- no filesystem or terminal I/O -- so building the app
        # here touches nothing.
        self.commands = _walk(_build_app(Workspace.default(), BinaryLocator.default()))

    def test_classification_table(self) -> None:
        """Every command carries its reviewed classification."""
        for path, effect in EFFECTS.items():
            with self.subTest(command=path):
                self.assertIn(path, self.commands, f"command '{path}' is gone")
                self.assertEqual(
                    self.commands[path].effect,
                    effect,
                    f"'{path}' is classified {self.commands[path].effect!r}, "
                    f"the reviewed table says {effect!r}",
                )

    def test_no_command_escapes_the_table(self) -> None:
        """A new command must be classified in the table above."""
        unreviewed = set(self.commands) - set(EFFECTS)
        self.assertFalse(
            unreviewed,
            f"unreviewed commands: {sorted(unreviewed)} -- add each to EFFECTS "
            "with the mutation it performs, or read_only",
        )

    def test_every_handler_is_bound_to_the_chokepoint(self) -> None:
        """Every command handler is wrapped by cli._bind.

        An unbound handler's effects execute in every mode -- including under
        ``--dry-run``, where nothing is supposed to run and nothing would fail
        to signal it.
        """
        unbound = [
            path
            for path, cmd in self.commands.items()
            if not getattr(cmd.handler, "__claudewheel_effects_bound__", False)
        ]
        self.assertFalse(
            unbound, f"command handlers not routed through cli._bind: {sorted(unbound)}"
        )

    def test_reserved_quartet_is_not_redeclared(self) -> None:
        """No command redeclares a framework-reserved flag name."""
        for path, cmd in self.commands.items():
            names = {f.name for f in cmd.flags}
            with self.subTest(command=path):
                self.assertFalse(
                    names & RESERVED_QUARTET,
                    f"'{path}' declares reserved flag(s) "
                    f"{sorted(names & RESERVED_QUARTET)}",
                )

    def test_deprecated_commands_are_classification_exempt(self) -> None:
        """The three retired top-level names carry no classification.

        Deprecated entries have no handler and execute nothing -- they print
        their message and exit 1 -- so the contract makes ``effect=`` a
        registration error on them. This pins that they stay registered as
        deprecations rather than being resurrected as commands.
        """
        app = _build_app(Workspace.default(), BinaryLocator.default())
        deprecated = set(app._deprecated)
        self.assertEqual(deprecated, {"new-profile", "delete-profile", "show-profile"})
        self.assertFalse(deprecated & set(self.commands))


if __name__ == "__main__":
    unittest.main()
