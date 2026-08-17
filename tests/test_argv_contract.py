"""The launch argv contract, pinned against the last release that predates it.

Turning the four session flags into one member-spelled selection changed how
the decision is *declared*; it was not allowed to change a single spelling an
operator types or a single byte of the argv that spelling produces.  Nothing in
the suite could see that: every other test here reads the new shape, so a
spelling silently dropped by the conversion would take its test with it.  The
short forms did exactly that -- ``-c``, ``-r`` and ``-p`` disappeared for one
framework version and nothing failed.

So the reference is not this repository at all.  ``REFERENCE`` below is the
output of ``scripts/argv-sweep`` run against **claudewheel 0.26.0 installed
from PyPI**, the last release built on the pre-selection declarations: for each
argv form, the exact ``extra_flags`` that release handed the launch sequence,
or ``REFUSED`` where it exited non-zero.  Re-probe it with

    uv venv build/v0260-env
    uv pip install --python build/v0260-env/bin/python \\
        claudewheel==0.26.0 strictcli==0.40.0
    (cd build && HOME=$PWD/fakehome ./v0260-env/bin/python ../scripts/argv-sweep)

Two entries are worth reading twice, because they are refusals the released
version also produced and the conversion had to keep producing:

* ``-r=<id>`` and ``-p=<prompt>`` are refused.  A short form takes its value as
  the next argument; the ``=`` spelling belongs to the long form alone, and it
  never worked here.
* a bare ``-r`` / ``--resume`` is refused.  ``--resume`` carries a value, and
  the empty picker spelling is ``--resume ""``.

``SINCE`` names the cases whose spelling the selection *added*, which the
released version can only refuse; they are asserted against this repository's
own expectation instead.
"""

from __future__ import annotations

import unittest
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

#: The sweep engine and its case table, shared with the standalone script so a
#: case exists once.  The script is deliberately importable-by-path rather than
#: a package module: it has to run against an *installed* claudewheel too, with
#: this repository nowhere on the import path.
_SWEEP_PATH = Path(__file__).resolve().parents[1] / "scripts" / "argv-sweep"
_SPEC = importlib.util.spec_from_loader(
    "argv_sweep", SourceFileLoader("argv_sweep", str(_SWEEP_PATH))
)
assert _SPEC is not None and _SPEC.loader is not None
sweep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sweep)

#: Marks a case the reference release exited non-zero on.
REFUSED = "refused"

#: claudewheel 0.26.0's answer to every case, verbatim from the probe.
REFERENCE: dict[str, Any] = {
    "bare": [],
    "bare-passthrough": ["--verbose"],
    "cont-long": ["--continue"],
    "cont-passthrough": ["--continue", "--output-format", "json"],
    "cont-short": ["--continue"],
    "cont-short-passthrough": ["--continue", "--output-format", "json"],
    "decline-cont": [],
    "new-session": REFUSED,
    "picker": ["--resume"],
    "picker-passthrough": ["--resume", "--verbose"],
    "print-empty": ["--print", ""],
    "print-long-equals": ["--print", "hello"],
    "print-long-space": ["--print", "hello"],
    "print-passthrough": ["--print", "hi", "--output-format", "json"],
    "print-short-equals": REFUSED,
    "print-short-space": ["--print", "hello"],
    "refuse-all-four": REFUSED,
    "refuse-cont-picker": REFUSED,
    "refuse-cont-print": REFUSED,
    "refuse-cont-resume": REFUSED,
    "refuse-print-picker": REFUSED,
    "refuse-resume-picker": REFUSED,
    "refuse-resume-print": REFUSED,
    "refuse-shorts": REFUSED,
    "resume-bare-long": REFUSED,
    "resume-bare-short": REFUSED,
    "resume-empty-equals": ["--resume"],
    "resume-empty-space": ["--resume"],
    "resume-long-equals": ["--resume", sweep.UUID],
    "resume-long-space": ["--resume", sweep.UUID],
    "resume-passthrough": ["--resume", sweep.UUID, "--verbose"],
    "resume-short-equals": REFUSED,
    "resume-short-space": ["--resume", sweep.UUID],
}

#: Spellings the declared selection added, so the reference release refuses
#: them: what they must produce *here* is stated instead.
SINCE: dict[str, Any] = {
    "new-session": [],
}


class ArgvContractTests(unittest.TestCase):
    """Every session spelling, against what the last pre-selection release did."""

    def _launch_argv(self, case: dict[str, Any]) -> Any:
        result = sweep.probe(list(case["argv"]), list(case.get("passthrough", [])))
        if result["outcome"] == "launch":
            return result["extra_flags"]
        self.assertNotEqual(result["exit"], 0, "a refusal must exit non-zero")
        return REFUSED

    def test_the_sweep_covers_every_case_the_reference_holds(self) -> None:
        """A case dropped from the table is a case that stops being checked."""
        self.assertEqual(
            sorted(c["id"] for c in sweep.CASES), sorted(REFERENCE), "case table drift"
        )

    def test_every_spelling_reproduces_the_released_behavior(self) -> None:
        for case in sweep.CASES:
            cid = case["id"]
            if cid in SINCE:
                continue
            with self.subTest(case=cid, argv=case["argv"]):
                self.assertEqual(self._launch_argv(case), REFERENCE[cid])

    def test_the_shorts_still_elect_their_members(self) -> None:
        """The three shorts, named: they were lost once and nothing noticed."""
        self.assertEqual(self._launch_argv({"argv": ["-c"]}), ["--continue"])
        self.assertEqual(
            self._launch_argv({"argv": ["-r", sweep.UUID]}), ["--resume", sweep.UUID]
        )
        self.assertEqual(self._launch_argv({"argv": ["-p", "hello"]}), ["--print", "hello"])

    def test_the_added_members_launch_as_this_release_declares(self) -> None:
        """--new-session names, in the invocation, what the default elects."""
        for case in sweep.CASES:
            if case["id"] not in SINCE:
                continue
            with self.subTest(case=case["id"]):
                self.assertEqual(self._launch_argv(case), SINCE[case["id"]])


if __name__ == "__main__":
    unittest.main()
