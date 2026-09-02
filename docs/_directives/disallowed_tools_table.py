"""selfdoc custom directive: render the stripped-tool table from the model.

Registered in selfdoc.json as ``"table-disallowed-tools"``. selfdoc
importlib-loads this file and calls ``resolve(attrs, config, body) -> str``; the
returned markdown is spliced into the page in place of the
``:-: table-disallowed-tools`` directive line.

The strip list is the single source of truth in ``claudewheel/guardrail.py``
(``DISALLOWED_TOOL_ENTRIES``). This directive loads that module directly by file
path (it depends only on the stdlib, so loading it has no package side effects)
and emits one table row per entry so the generated reference can never drift
from the model.

Any error is allowed to propagate: selfdoc turns an exception into a visible
``> *[selfdoc: custom directive 'table-disallowed-tools' failed: ...]*``
sentinel that ``selfdoc check`` flags as a FAILED directive.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def _load_guardrail_module() -> ModuleType:
    """Load ``claudewheel/guardrail.py`` from the repo root and return it.

    The repo root is two directories above this file
    (``docs/_directives/disallowed_tools_table.py`` -> repo root).
    """
    here = os.path.abspath(__file__)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    guardrail_path = os.path.join(repo_root, "claudewheel", "guardrail.py")
    module_name = "claudewheel_guardrail_for_disallowed_docs"
    spec = importlib.util.spec_from_file_location(module_name, guardrail_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a module spec for {guardrail_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass type resolution can find the module in
    # sys.modules (Python 3.12+ dataclasses look up cls.__module__ there).
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _vendored_render(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Minimal pipe-escaping markdown table renderer.

    Fallback used only when ``selfdoc_core.tables.render_markdown_table`` is not
    importable in the process resolving this directive.
    """

    def esc(text: object) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(esc(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)


def resolve(attrs: dict[str, str], config: dict[str, Any], body: list[str]) -> str:
    """Render the stripped-tool list as a markdown table.

    selfdoc's resolver calls every custom directive with this exact triple:
    the directive's parsed ``key="value"`` attributes, the validated
    ``selfdoc.json`` config, and the directive's block body lines. This
    directive takes no attributes and no body, so all three go unused.

    Columns: Tool (the Claude Code tool name stripped via ``--disallowedTools``)
    and Why (the reason it is banned).
    """
    mod = _load_guardrail_module()

    headers = ["Tool", "Why"]
    rows: list[list[str]] = [
        [f"`{entry.name}`", entry.why] for entry in mod.DISALLOWED_TOOL_ENTRIES
    ]

    # selfdoc_core is deliberately NOT a claudewheel dependency: it exists only
    # in the selfdoc process that resolves this directive. Import it by name so
    # the absence is a plain runtime ImportError rather than an unresolvable
    # static import, and cast the untyped callable to the shape used here.
    try:
        tables = importlib.import_module("selfdoc_core.tables")
    except ImportError:
        return _vendored_render(headers, rows)
    render_markdown_table = cast(
        "Callable[[Sequence[str], Sequence[Sequence[str]]], str]",
        tables.render_markdown_table,
    )
    return render_markdown_table(headers, rows)
