"""Run user-defined hook scripts at pre-launch and other lifecycle stages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run_hooks(hooks_dir: Path, stage: str, selections: dict[str, str | None]) -> bool:
    """Run hook scripts for a given stage. Returns True if all pass.

    Scans *hooks_dir* for executable files whose names start with the stage
    prefix (e.g. "pre-launch"). Passes current selections as CL_PROFILE,
    CL_GITHUB, etc. environment variables.

    Returns False if any hook exits nonzero (its stderr is printed).
    """
    if not hooks_dir.is_dir():
        return True

    # Build env with CL_* variables from selections
    env = dict(os.environ)
    for key, val in selections.items():
        if val is not None:
            env[f"CL_{key.upper()}"] = val

    # Find and run matching hooks sorted by name
    hooks = sorted(
        [
            f
            for f in hooks_dir.iterdir()
            if f.name.startswith(stage) and os.access(f, os.X_OK)
        ],
        key=lambda f: f.name,
    )

    for hook in hooks:
        try:
            result = subprocess.run(
                [str(hook)], env=env, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                print(
                    f"Hook '{hook.name}' failed (exit {result.returncode}):",
                    file=sys.stderr,
                )
                if result.stderr.strip():
                    print(result.stderr.strip(), file=sys.stderr)
                return False
        except subprocess.TimeoutExpired:
            print(f"Hook '{hook.name}' timed out after 10s", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Hook '{hook.name}' error: {e}", file=sys.stderr)
            return False

    return True
