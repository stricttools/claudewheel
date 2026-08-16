# The archiver's envelope test stubs still build `interface_version: 1`

## Context

`claudewheel/archiver.py` delegates profile deletion to an external CLI and
reads its answer out of a machine-mode envelope on stdout. Two test helpers
build a fake envelope for those tests:

- `tests/test_archiver.py`'s `envelope()`
- `tests/wheelhelpers.py`'s `envelope()`

Both hard-code `"interface_version": 1`.

## Problem

That CLI's framework advanced its envelope contract to version **2**. A real
answer now carries `"interface_version": 2` and one additional member,
`"writes"` (always `null` there -- it names the properties an update command
wrote, and that tool declares none). The stubs describe a document the tool no
longer emits.

**Nothing is broken today.** `archiver._payload()` parses the envelope and reads
exactly one member, `payload`; it never looks at `interface_version` and never
enumerates keys, so both the real version-2 answer and the stubbed version-1 one
parse identically. The tests pass, and the shipped code path works against the
real binary.

What is wrong is that the doubles no longer stand in for the thing they double.
A test that asserts claudewheel copes with the tool's output is only worth what
its fixture's fidelity is worth, and this fixture is now a version behind. The
next time the envelope's shape does something claudewheel cares about, the
stub's drift is what will hide it.

## What to do

Update both helpers to the version-2 shape: bump `interface_version` to `2` and
add `"writes": None` beside `dry_run`. Keep the parser's indifference to the
version -- reading `payload` and nothing else is the correct posture, and
pinning a version in `_payload()` would turn a compatible envelope into a
refusal for no gain.

Consider whether the two near-identical `envelope()` helpers should be one
shared helper while touching them; they have drifted in `app_version` already.

## Affected files

- `tests/test_archiver.py`
- `tests/wheelhelpers.py`

## Effort

Tiny -- two literals per helper, plus the optional consolidation.
