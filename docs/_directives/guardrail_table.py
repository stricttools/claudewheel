"""selfdoc custom directive: render the guardrail rule table from the model.

Registered in selfdoc.json as ``"table-guardrails"``. selfdoc importlib-loads
this file and calls ``resolve(attrs, config, body) -> str``; the returned
markdown is spliced into the page in place of the ``:-: table-guardrails``
directive line.

The rule set is the single source of truth in ``claudewheel/guardrail.py``.
This directive imports that module directly (it depends only on the stdlib, so
loading it has no package side effects) and emits one table row per rule so the
generated reference can never drift from the model.

Any error is allowed to propagate: selfdoc turns an exception into a visible
``> *[selfdoc: custom directive 'table-guardrails' failed: ...]*`` sentinel that
``selfdoc check`` flags as a FAILED directive.
"""

from __future__ import annotations

import importlib.util
import os
import sys


def _load_guardrail_module():
    """Load ``claudewheel/guardrail.py`` from the repo root and return it.

    The repo root is two directories above this file
    (``docs/_directives/guardrail_table.py`` -> repo root).
    """
    here = os.path.abspath(__file__)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    guardrail_path = os.path.join(repo_root, "claudewheel", "guardrail.py")
    module_name = "claudewheel_guardrail_for_docs"
    spec = importlib.util.spec_from_file_location(module_name, guardrail_path)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass type resolution can find the module in
    # sys.modules (Python 3.12+ dataclasses look up cls.__module__ there).
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _vendored_render(headers, rows):
    """Minimal pipe-escaping markdown table renderer.

    Fallback used only when ``selfdoc_core.tables.render_markdown_table`` is not
    importable in the process resolving this directive.
    """

    def esc(text):
        return str(text).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(esc(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)


def _advice_for(rule, mod):
    """Return the human-facing advice/note for *rule*.

    HARD_DENY and ADVISE rules carry ``main_advice``. ESCALATE rules have no
    main advice (the hook is silent for the main agent), so recover the
    rule-specific lead sentence by stripping the shared ESCALATE tail from the
    subagent message. ASK rules carry no advice text at all.
    """
    if rule.main_advice:
        return rule.main_advice
    if rule.tier is mod.Tier.ESCALATE:
        message = rule.subagent_advice or ""
        tail = mod.ESCALATE_TAIL
        if message.endswith(tail):
            message = message[: -len(tail)].strip()
        return message or "Subagents are denied; the main agent is prompted via settings."
    if rule.tier is mod.Tier.ASK:
        return "Prompted via the settings ask rule (no hook)."
    return ""


def resolve(attrs, config, body):
    """Render the guardrail rule set as a markdown table.

    Columns: Key, Tier, Settings coverage (FULL/PARTIAL/NONE, or "n/a" when the
    tier has no settings backstop), and Advice.
    """
    mod = _load_guardrail_module()

    headers = ["Key", "Tier", "Settings coverage", "Advice"]
    rows = []
    for rule in mod.RULES:
        coverage = rule.settings_coverage
        coverage_str = coverage.name if coverage is not None else "n/a"
        rows.append(
            [
                f"`{rule.key}`",
                rule.tier.name,
                coverage_str,
                _advice_for(rule, mod),
            ]
        )

    try:
        from selfdoc_core.tables import render_markdown_table
    except ImportError:
        return _vendored_render(headers, rows)
    return render_markdown_table(headers, rows)
