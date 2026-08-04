"""The effects chokepoint is the only authorized effect surface.

``claudewheel.effects`` is the single module in which claudewheel production
code may call ``subprocess``, ``open(path, "w")``, ``Path.write_text``,
``Path.mkdir``, ``os.rename``, ``shutil.rmtree``, ``urlopen`` and their
siblings.  Everything else routes through it, so strictcli's ``--dry-run``
regime is adapted in one file rather than at ~70 call sites -- and so no future
call site can quietly reintroduce a bare write that a preview would perform for
real against the user's ``~/.claudewheel/``.

This is an AST scan, not a grep: it sees the call target rather than the
spelling, so ``os . rename(...)`` and a multi-line ``subprocess.run(`` are both
caught.

Two exemption mechanisms, both deliberately narrow:

* ``_EXEMPT_MODULES`` -- the chokepoint itself, which holds the primitives, and
  ``pty_runner``, whose ``os.execvpe`` sits in the post-``pty.fork`` child
  branch: that code runs in a different process than the one holding the
  effects handle, and there is nothing there to record.
* an inline ``# effects: exempt -- <reason>`` comment on the offending line,
  for self-owned scratch files and for method names this scan cannot tell apart
  from a same-named domain method.  A reason is mandatory; the marker without
  one does not count.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

PRODUCTION_PACKAGE = "claudewheel"

_EXEMPT_MODULES = {
    "claudewheel/effects.py",
    "claudewheel/pty_runner.py",
}

# Dotted call targets that mutate the world.
_BANNED_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.makedirs",
    "os.mkdir",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.removedirs",
    "os.rename",
    "os.renames",
    "os.replace",
    "os.chmod",
    "os.symlink",
    "os.link",
    "os.truncate",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "shutil.rmtree",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copytree",
    "shutil.move",
    "urllib.request.urlopen",
}

# Method names that are unambiguously filesystem mutations on a Path.  ``rename``
# is in the set even though claudewheel also has a domain method by that name
# (``TokenStore.rename``): a scan that dropped it would stop seeing
# ``Path.rename``, which is the commit seam of every atomic write.  The two
# domain call sites carry the inline marker.
_BANNED_METHODS = {
    "write_text",
    "write_bytes",
    "mkdir",
    "unlink",
    "rmdir",
    "touch",
    "chmod",
    "rename",
    "symlink_to",
    "hardlink_to",
}

_EXEMPT_MARKER = "# effects: exempt --"


def _production_files():
    files = []
    for path in sorted((REPO_ROOT / PRODUCTION_PACKAGE).rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _EXEMPT_MODULES or "/__pycache__/" in rel:
            continue
        files.append((rel, path))
    return files


def _dotted(node):
    """Return the dotted spelling of a call target, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _write_mode(call):
    """True when an ``open(...)`` call opens the path for writing."""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    else:
        mode = None
        for kw in call.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
    if not isinstance(mode, str):
        return False
    return any(ch in mode for ch in "wax")


def _violations(rel, path):
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        target = dotted
        banned = dotted in _BANNED_CALLS or (
            dotted == "open" and _write_mode(node)
        )
        if not banned and isinstance(node.func, ast.Attribute):
            # ``effects.mkdir(...)`` IS the routed call, not a bypass.
            through_chokepoint = dotted is not None and dotted.startswith(
                "effects."
            )
            if node.func.attr in _BANNED_METHODS and not through_chokepoint:
                banned = True
                target = f".{node.func.attr}"
        if not banned:
            continue
        line = lines[node.lineno - 1]
        if _EXEMPT_MARKER in line and line.split(_EXEMPT_MARKER, 1)[1].strip():
            continue
        found.append(f"{rel}:{node.lineno}: {target}")
    return found


@pytest.mark.parametrize(
    "rel,path", _production_files(), ids=lambda v: v if isinstance(v, str) else "",
)
def test_no_effect_bypass(rel, path):
    """No production module calls an effectful primitive outside the chokepoint."""
    violations = _violations(rel, path)
    assert not violations, (
        "effectful call outside claudewheel.effects:\n  "
        + "\n  ".join(violations)
        + "\n\nRoute it through claudewheel.effects, or mark a self-owned "
        "scratch operation with '# effects: exempt -- <reason>'."
    )


def test_exempt_module_list_stays_two():
    """The wholesale exemption list stays short -- a widening must be deliberate."""
    assert _EXEMPT_MODULES == {
        "claudewheel/effects.py",
        "claudewheel/pty_runner.py",
    }
