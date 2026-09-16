#!/usr/bin/env python3
"""Type the demo recording's keystrokes, on a clock.

This is the person in the recording. It writes keys to stdout, which
scripts/record-demo-cast.sh pipes into the asciinema recorder, which forwards
them to the TUI's pty -- so the recording carries a real session driven by real
input, with the pauses a reader needs between beats.

The choreography, and what each beat shows:

1. the client step: move the cursor to miniclaude, back to claude, select it
2. the segment bar: walk right to the version segment
3. cycle the version segment's value down twice and back up once, which draws
   the fan-out of the other options above and below the bar
4. walk right to the model segment and cycle it, drawing the model list
5. ``S``: the machine-wide sessions overview, a framed table
6. move down the rows, then ``a`` to reveal the finished sessions too
7. ``q`` to close the overview, ``q`` to leave the launcher

RETURN is pressed exactly once, on the client step, where it selects a client
and moves on. It is never pressed on the segment bar: there, RETURN launches
Claude Code. The two exits are ``q``.

USAGE
    demo-drive.py [--scale FACTOR]
"""

from __future__ import annotations

import sys
import time

UP = "\x1b[A"
DOWN = "\x1b[B"
RIGHT = "\x1b[C"
ENTER = "\r"

# (seconds to wait before sending, keys to send)
STEPS: list[tuple[float, str]] = [
    (1.4, DOWN),  # client step: look at miniclaude
    (0.9, UP),  # and back to claude
    (0.8, ENTER),  # select it -- the one RETURN in the recording
    (1.3, RIGHT),  # bar: profile -> github
    (0.9, RIGHT),  # github -> version
    (0.9, DOWN),  # cycle the version segment
    (0.8, DOWN),
    (0.9, UP),
    (0.9, RIGHT),  # version -> model
    (0.8, DOWN),  # cycle the model segment
    (0.8, DOWN),
    (1.0, "S"),  # the sessions overview
    (1.6, DOWN),  # move down the rows
    (0.9, DOWN),
    (1.2, "a"),  # reveal the finished sessions too
    (2.0, "q"),  # close the overview
    (1.2, "q"),  # leave the launcher, launching nothing
]

# Held after the last keypress so the recording does not cut on the exit.
TAIL = 1.2


def main(argv: list[str]) -> int:
    scale = 1.0
    args = argv[1:]
    while args:
        if args[0] == "--scale":
            if len(args) < 2:
                sys.stderr.write("demo-drive.py: --scale needs a number\n")
                return 2
            scale = float(args[1])
            args = args[2:]
        else:
            sys.stderr.write(f"demo-drive.py: unknown argument: {args[0]}\n")
            return 2
    if scale <= 0:
        sys.stderr.write("demo-drive.py: --scale needs a positive number\n")
        return 2

    for delay, keys in STEPS:
        time.sleep(delay * scale)
        sys.stdout.write(keys)
        sys.stdout.flush()
    time.sleep(TAIL * scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
