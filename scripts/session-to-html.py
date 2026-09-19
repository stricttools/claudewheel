#!/usr/bin/env python3
"""Render a Claude Code session transcript as a simple, readable HTML page.

Only the conversation is kept: the person's messages and the assistant's
written replies. Tool calls, tool output, thinking blocks, attachments, and
the harness bookkeeping lines are dropped.

    scripts/session-to-html.py <session.jsonl> --out <page.html>
    scripts/session-to-html.py <session.jsonl> --out <page.html> --include-subagents

A session's subagent transcripts, when they exist, live in a directory beside
the file and named after the session. With --include-subagents each one is
nested inside the message that spawned it, found by the agent identifier the
transcript records, so the page is a tree rather than a flat list. A subagent
whose spawning message cannot be found is appended at the end, named.
"""

import argparse
import html
import json
import pathlib
import sys

CSS = """
:root { color-scheme: dark; }
body { background:#0d0f12; color:#e6e6e6; font:16px/1.6 system-ui,sans-serif;
       margin:0 auto; padding:2rem 1.25rem 6rem; max-width:52rem; }
h1 { font-size:1.35rem; margin:0 0 .25rem; }
.meta { color:#8b949e; font:13px/1.5 ui-monospace,monospace; margin-bottom:2rem; }
details { border:1px solid #262b31; border-left-width:3px; margin:.5rem 0;
          background:#12151a; }
details[open] { background:#11141a; }
summary { cursor:pointer; padding:.5rem .75rem; font:13px/1.4 ui-monospace,monospace;
          color:#8b949e; user-select:none; }
summary:hover { color:#e6e6e6; }
.user { border-left-color:#4f9eff; }
.assistant { border-left-color:#3fb950; }
.user > summary::before { content:"you  "; color:#4f9eff; }
.assistant > summary::before { content:"claude  "; color:#3fb950; }
.body { padding:0 .75rem .75rem; white-space:pre-wrap; word-wrap:break-word; }
.agent { border-left-color:#d29922; margin-left:1.25rem; }
.agent > summary::before { content:"agent  "; color:#d29922; }
.nest { padding:0 .75rem .75rem; }
.orphans { margin-top:3rem; border-top:1px solid #262b31; padding-top:1rem; }
.count { color:#6e7681; }
.controls { position:sticky; top:0; background:#0d0f12; padding:.5rem 0 1rem; }
button { background:#12151a; color:#e6e6e6; border:1px solid #30363d;
         padding:.35rem .7rem; font:13px ui-monospace,monospace; cursor:pointer; }
button:hover { border-color:#4f9eff; }
"""

JS = """
function setAll(open){document.querySelectorAll('details').forEach(d=>d.open=open);}
"""


def texts(entry):
    """The written text of one transcript line, or None when it has none."""
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    joined = "\n".join(p for p in parts if p).strip()
    return joined or None


def conversation(path):
    """Yield (role, timestamp, text) for every written message in a transcript."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue
            text = texts(entry)
            if text is None:
                continue
            # Harness-injected user turns: tool results, reminders, caveats.
            if role == "user" and text.startswith(("<local-command", "<system-reminder", "Caveat:")):
                continue
            yield role, entry.get("timestamp", ""), text


def block(role, stamp, text, extra=""):
    head = html.escape(stamp[:19].replace("T", " "))
    first = html.escape(text.strip().splitlines()[0][:110])
    return (
        f'<details class="{role}"><summary>{head} &nbsp; {first}{extra}</summary>'
        f'<div class="body">{html.escape(text)}</div>'
    )


def render_messages(path):
    return [block(r, s, t) + "</details>" for r, s, t in conversation(path)]


def subagent_owners(path, sub_dir):
    """Map each agent identifier to the index of the message that spawned it.

    The identifier is announced in the spawn's tool result, which is not a
    written message, so the transcript is walked line by line: the owner of an
    agent is the last written message emitted before the line that first names
    it. That keeps the mapping correct even though the announcement itself is
    filtered out of the page.
    """
    ids = [p.stem[len("agent-"):] for p in sorted(sub_dir.glob("agent-*.jsonl"))]
    pending = set(ids)
    owners = {}
    messages = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            for agent_id in list(pending):
                if agent_id in line:
                    owners[agent_id] = len(messages) - 1
                    pending.discard(agent_id)
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue
            text = texts(entry)
            if text is None:
                continue
            if role == "user" and text.startswith(("<local-command", "<system-reminder", "Caveat:")):
                continue
            messages.append((role, entry.get("timestamp", ""), text))
    owners = {a: i for a, i in owners.items() if i >= 0}
    return owners, messages, ids


def render_tree(path, sub_dir):
    """The conversation, with each subagent nested inside its spawning message."""
    owners, messages, ids = subagent_owners(path, sub_dir)
    nested = {}
    for agent_id, index in owners.items():
        nested.setdefault(index, []).append(agent_id)

    def describe(agent_id):
        meta = sub_dir / f"agent-{agent_id}.meta.json"
        if meta.is_file():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return agent_id
            return data.get("description") or data.get("agentType") or agent_id
        return agent_id

    out = []
    for index, (role, stamp, text) in enumerate(messages):
        children = nested.get(index, [])
        extra = f' <span class="count">[{len(children)} agent]</span>' if children else ""
        piece = block(role, stamp, text, extra)
        if children:
            piece += '<div class="nest">'
            for agent_id in children:
                inner = "\n".join(render_messages(sub_dir / f"agent-{agent_id}.jsonl"))
                piece += (
                    f'<details class="agent"><summary>{html.escape(str(describe(agent_id)))}'
                    f'</summary><div class="nest">{inner}</div></details>'
                )
            piece += "</div>"
        out.append(piece + "</details>")

    orphans = [i for i in ids if i not in owners]
    return out, orphans, describe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", help="path to a session .jsonl file")
    parser.add_argument("--out", required=True, help="path of the HTML page to write")
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="append each subagent transcript as its own section",
    )
    args = parser.parse_args()

    source = pathlib.Path(args.transcript)
    if not source.is_file():
        sys.exit(f"no such transcript: {source}")

    sub_dir = source.with_suffix("") / "subagents"
    if args.include_subagents and sub_dir.is_dir():
        blocks, orphans, describe = render_tree(source, sub_dir)
        body = "\n".join(blocks)
        if orphans:
            body += '<div class="orphans"><h2>Agents whose caller was not found</h2>'
            for agent_id in orphans:
                inner = "\n".join(render_messages(sub_dir / f"agent-{agent_id}.jsonl"))
                body += (
                    f'<details class="agent"><summary>{html.escape(str(describe(agent_id)))}'
                    f'</summary><div class="nest">{inner}</div></details>'
                )
            body += "</div>"
    else:
        blocks = render_messages(source)
        body = "\n".join(blocks)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(source.stem)}</title>
<style>{CSS}</style></head><body>
<h1>{html.escape(source.stem)}</h1>
<p class="meta">{html.escape(str(source))} &mdash; {len(blocks)} messages</p>
<div class="controls">
<button onclick="setAll(true)">expand all</button>
<button onclick="setAll(false)">collapse all</button>
</div>
{body}
<script>{JS}</script>
</body></html>
"""
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out} ({len(blocks)} messages)")


if __name__ == "__main__":
    main()
