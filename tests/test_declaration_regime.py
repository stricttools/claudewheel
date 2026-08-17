"""The declaration regime, pinned: presence, selectors and constraints.

strictcli enforces the regime's *rules* at registration time -- a flag with no
presence declaration, a value default on a mutating command, a bare `choices`
entry or an unnamed constraint all fail to build.  What no registration guard
can see is whether claudewheel's own answers to those rules are the reviewed
ones, and that is what this file holds:

1. **The two selectors' member spellings are the argv contract.**  ``--profile
   <name>`` / ``--all-profiles`` and ``--cont`` / ``--resume`` /
   ``--print-prompt`` / ``--picker`` / ``--new-session`` are what operators,
   scripts and this project's own docs type.  A rename inside a selector is
   invisible to every other test here -- the framework is equally happy with
   any member name -- so the names are listed and compared.

2. **The session selector's default is the new-session member.**  An absent
   selection has to resolve to plain launch, the thing a bare ``claudewheel``
   has always done.  Losing the default would turn every bare launch into an
   unsatisfied-selector refusal; changing which member it names would silently
   change what a bare launch does.

3. **An optional flag on a mutating command says what its absence means.**
   The mutating-default ban leaves exactly one legal way to keep an opt-in
   switch: declare ``presence="optional"`` and apply the fallback in the
   handler, *saying so in the help*.  The saying-so is the half a registration
   guard cannot check, and dropping it turns a documented fallback into an
   undocumented one.
"""

from __future__ import annotations

import unittest
from typing import Any

from claudewheel.binaries import BinaryLocator
from claudewheel.cli import _build_app
from claudewheel.workspace import Workspace

#: Selector name -> (elected-by spelling, member names in declaration order).
SELECTORS = {
    "purge-plugins": ("target", ["profile", "all-profiles"]),
    "permission.add": ("target", ["profile", "all-profiles"]),
    "permission.remove": ("target", ["profile", "all-profiles"]),
    "permission.list": ("target", ["profile", "all-profiles"]),
    "launch": (
        "session",
        ["cont", "resume", "print-prompt", "picker", "new-session"],
    ),
}

#: Every optional flag of a mutating command whose absence the handler resolves
#: to a fallback, with the phrase its help must carry to say so.  A flag whose
#: absence is genuinely absence (``--profile`` on reconcile-permissions, the
#: launch segment presets) is not here: nothing substitutes a value for it.
DOCUMENTED_FALLBACKS = {
    ("mv", "post-hoc"): "when omitted",
    ("import", "reid"): "when omitted",
    ("deploy-hooks", "all"): "when omitted",
    ("deploy-hooks", "force-overwrite"): "when omitted",
}

#: The declared co-occurrence constraints, by command.
CONSTRAINTS = {
    "import": ["path-mapping"],
    "deploy-hooks": ["deploy-target"],
}


def _schema() -> dict[str, Any]:
    app = _build_app(Workspace.default(), BinaryLocator.default())
    return app.dump_schema_dict()


def _commands(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every registered command, keyed by dotted path (``permission.add``)."""
    out: dict[str, dict[str, Any]] = dict(schema.get("commands", {}))
    for group_name, group in schema.get("groups", {}).items():
        for cmd_name, cmd in group.get("commands", {}).items():
            out[f"{group_name}.{cmd_name}"] = cmd
    return out


class SelectorSpellingTests(unittest.TestCase):
    """The member spellings are the argv, so they are pinned by name."""

    def setUp(self) -> None:
        self.commands = _commands(_schema())

    def _selector(self, path: str, name: str) -> dict[str, Any]:
        for flag in self.commands[path]["flags"]:
            if flag["name"] == name:
                return dict(flag)
        self.fail(f"{path} declares no selector named {name!r}")

    def test_every_selector_declares_its_reviewed_members(self) -> None:
        for path, (sel_name, members) in SELECTORS.items():
            with self.subTest(command=path):
                sel = self._selector(path, sel_name)
                self.assertEqual(sel["elect_by"], "member-flags")
                self.assertEqual([c["name"] for c in sel["choices"]], members)

    def test_no_command_declares_a_selector_that_is_not_reviewed(self) -> None:
        for path, cmd in self.commands.items():
            for flag in cmd.get("flags", []):
                if "choices" in flag and "elect_by" in flag:
                    with self.subTest(command=path):
                        self.assertIn(path, SELECTORS)
                        self.assertEqual(SELECTORS[path][0], flag["name"])

    def test_the_session_selector_defaults_to_the_new_session_member(self) -> None:
        """A bare launch elects new-session, which is what it has always done."""
        sel = self._selector("launch", "session")
        self.assertEqual(sel["presence"], "default")
        self.assertEqual(sel["default"], {"choice": "new-session"})

    def test_the_profile_target_selector_is_required_everywhere(self) -> None:
        """Naming no profile is the framework's refusal on all four commands."""
        for path, (sel_name, _) in SELECTORS.items():
            if sel_name != "target":
                continue
            with self.subTest(command=path):
                self.assertEqual(self._selector(path, sel_name)["presence"], "required")


class PresenceTests(unittest.TestCase):
    """What the declarations say about absence, on every command."""

    def setUp(self) -> None:
        self.commands = _commands(_schema())

    def test_every_flag_and_arg_declares_a_presence(self) -> None:
        for path, cmd in self.commands.items():
            for entry in list(cmd.get("flags", [])) + list(cmd.get("args", [])):
                with self.subTest(command=path, entry=entry["name"]):
                    self.assertIn(
                        entry.get("presence"),
                        ("required", "optional", "default"),
                    )

    def test_no_mutating_command_defaults_a_value(self) -> None:
        """The ban, restated over the dump: only empty collections survive."""
        for path, cmd in self.commands.items():
            if cmd["effect"] != "mutating":
                continue
            for entry in list(cmd.get("flags", [])) + list(cmd.get("args", [])):
                if entry.get("presence") != "default":
                    continue
                with self.subTest(command=path, entry=entry["name"]):
                    default = entry.get("default")
                    self.assertIn(
                        default,
                        ([], {}, {"choice": "new-session"}),
                        "a mutating command may default only an empty "
                        "collection or a selector's own election",
                    )

    def test_every_documented_fallback_says_so_in_its_help(self) -> None:
        for (path, flag_name), phrase in DOCUMENTED_FALLBACKS.items():
            with self.subTest(command=path, flag=flag_name):
                flags = {f["name"]: f for f in self.commands[path]["flags"]}
                self.assertIn(flag_name, flags)
                self.assertEqual(flags[flag_name]["presence"], "optional")
                self.assertIn(phrase, flags[flag_name]["help"])


class ChoicesAndConstraintTests(unittest.TestCase):
    """Constrained values carry help; co-occurrence rules carry names."""

    def setUp(self) -> None:
        self.commands = _commands(_schema())

    def test_every_choices_entry_carries_help(self) -> None:
        """A `choices` entry is a record now, and claudewheel fills the help."""
        seen = 0
        for path, cmd in self.commands.items():
            for entry in list(cmd.get("flags", [])) + list(cmd.get("args", [])):
                if "elect_by" in entry:
                    continue  # a selector's choices are a different construct
                for choice in entry.get("choices", []) or []:
                    seen += 1
                    with self.subTest(command=path, entry=entry["name"]):
                        self.assertTrue(choice.get("help"))
        self.assertGreater(seen, 0, "no `choices` declaration found at all")

    def test_the_plan_choices_are_the_declarable_plans(self) -> None:
        from claudewheel.tokens import plan_keys

        args = {a["name"]: a for a in self.commands["profile.set-plan"]["args"]}
        declared = [c["value"] for c in args["plan"]["choices"]]
        self.assertEqual(declared, plan_keys())

    def test_the_declared_constraints_are_the_reviewed_ones(self) -> None:
        for path, cmd in self.commands.items():
            names = [c["name"] for c in cmd.get("constraints", []) or []]
            with self.subTest(command=path):
                self.assertEqual(names, CONSTRAINTS.get(path, []))


if __name__ == "__main__":
    unittest.main()
