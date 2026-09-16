#!/usr/bin/env python3
"""Turn a freshly generated claudewheel config directory into a demo fixture.

The recording in scripts/record-demo-cast.sh drives the real TUI, so the
workspace it drives has to be a real one -- but one that names nothing on this
machine and reaches nothing off it. This script takes the config directory
claudewheel itself just created from its own defaults (so the fixture inherits
segment definitions, themes and the schema version rather than restating them)
and edits three things into it:

* Every ``discovery`` block is removed from options.json. Discovery is the only
  part of a launch that touches the world: it enumerates real profiles under
  ``~``, asks ``gh auth status`` for real GitHub accounts, fetches the npm
  version list, and reads the Anthropic models endpoint with a stored OAuth
  token. With the blocks gone, every segment offers exactly the invented values
  written here and the TUI makes no network call and no scan of a real home.

* Invented option values are pinned. Which collection a value goes in matters:
  the profile and github segments merge ``pinned`` and ``discovered`` only, so
  a value placed in ``values`` would never appear on the bar.

* A handful of invented sessions are written into the lifecycle store, so the
  sessions overview (the ``S`` screen) has rows to show. Each is one JSONL file
  of append-only events, the shape ``.strictspec/lifecycle-event.schema.toml``
  defines. Their timestamps are relative to now, so the "Started" column reads
  the same however long after the fixture was built the recording happens.

The two states hidden until the viewer presses ``a`` (an exited session and one
marked done) are there so that keypress has something to reveal.

USAGE
    demo-fixture.py <config-dir>
"""

from __future__ import annotations

import json
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Invented option values, per segment key. The profile and github segments read
# `pinned`; the rest read `values` as their defaults collection.
PINNED = {
    "profile": ["work", "personal", "oss"],
    "github": ["octocat", "acme-bot"],
    "version": ["2.1.14", "2.1.9", "2.0.32"],
    "directory": ["~/Projects/myapp", "~/Projects/site", "~/work/api"],
}

# The selections the bar opens with.
LAST_CONFIG = {
    "profile": "work",
    "github": "octocat",
    "version": "2.1.14",
    "model": "claude-opus-5",
    "directory": "~/Projects/myapp",
    "mcp": "default",
    "permissions": "bypass",
}

# name, hours ago, cwd, claude version, model, profile, final event.
# A session with no final event has a `started` and no `ended`, which the
# overview shows as `crashed` once the sweep grace window has passed.
SESSIONS = [
    (
        "api-refactor",
        3.0,
        "~/Projects/myapp",
        "2.1.14",
        "claude-opus-5",
        "work",
        ("mark", "on-hold"),
    ),
    (
        "site-redesign",
        5.5,
        "~/Projects/site",
        "2.1.14",
        "claude-sonnet-5",
        "personal",
        ("mark", "blocked"),
    ),
    ("docs-sweep", 2.0, "~/Projects/site", "2.1.9", "claude-opus-4-8", "oss", None),
    (
        "nightly-eval",
        9.0,
        "~/work/api",
        "2.1.14",
        "claude-fable-5",
        "work",
        ("ended", "exited"),
    ),
    (
        "release-check",
        26.0,
        "~/Projects/myapp",
        "2.0.32",
        "claude-opus-5",
        "work",
        ("mark", "done"),
    ),
]


def patch_options(root: pathlib.Path) -> None:
    path = root / "options.json"
    options = json.loads(path.read_text())
    for key, entry in options.items():
        entry.pop("discovery", None)
        if key in PINNED:
            entry["pinned"] = list(PINNED[key])
    path.write_text(json.dumps(options, indent=2) + "\n")


def patch_config(root: pathlib.Path) -> None:
    path = root / "config.json"
    config = json.loads(path.read_text())
    # A fixed theme, because "auto" would probe the recording terminal's
    # background; no health check, because it inspects tokens and symlinks.
    config["theme"] = "dark"
    config["health_check_on_launch"] = False
    path.write_text(json.dumps(config, indent=2) + "\n")

    state_path = root / "state.json"
    state = json.loads(state_path.read_text())
    state["last_config"] = dict(LAST_CONFIG)
    state_path.write_text(json.dumps(state, indent=2) + "\n")


def seed_lifecycle(root: pathlib.Path) -> int:
    directory = root / "shared" / "lifecycle"
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    def at(hours: float) -> str:
        return (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    for index, (name, age, cwd, version, model, profile, final) in enumerate(SESSIONS):
        session = str(uuid.UUID(int=(0xC1A0DE << 96) + index))
        events: list[dict[str, object]] = [
            {
                "format_version": 1,
                "id": f"{session}-1",
                "at": at(age),
                "session": session,
                "source": "hook",
                "kind": "started",
                "cwd": cwd,
                "config_dir": f"~/.claudewheel/profiles/{profile}",
                "profile": profile,
                "claude_version": version,
                "model": model,
                "permissions": "bypass",
                "entry": "startup",
                "transcript": None,
                "pid": 10000 + index,
            },
            {
                "format_version": 1,
                "id": f"{session}-2",
                "at": at(age),
                "session": session,
                "source": "hook",
                "kind": "named",
                "name": name,
                "name_source": "user",
            },
        ]
        if final is not None and final[0] == "ended":
            events.append(
                {
                    "format_version": 1,
                    "id": f"{session}-3",
                    "at": at(age - 0.5),
                    "session": session,
                    "source": "hook",
                    "kind": "ended",
                    "outcome": final[1],
                    "reason": "prompt_input_exit",
                    "detail": None,
                }
            )
        if final is not None and final[0] == "mark":
            events.append(
                {
                    "format_version": 1,
                    "id": f"{session}-3",
                    "at": at(age - 0.5),
                    "session": session,
                    "source": "user",
                    "kind": "mark",
                    "state": final[1],
                    "note": None,
                }
            )
        (directory / f"{session}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
    return len(SESSIONS)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(__doc__.split("USAGE")[1].strip() + "\n")
        return 2
    root = pathlib.Path(argv[1])
    if not (root / "options.json").is_file():
        sys.stderr.write(f"demo-fixture.py: {root} holds no options.json\n")
        return 1
    patch_options(root)
    patch_config(root)
    count = seed_lifecycle(root)
    print(f"demo-fixture.py: patched {root}, seeded {count} sessions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
