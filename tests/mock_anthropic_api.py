"""A scripted stand-in for the Anthropic Messages API, for the pty harness.

Usage: mock_anthropic_api.py PORT_FILE LOG_FILE RULES_FILE

It serves on an ephemeral loopback port, which it writes to PORT_FILE once it
listens. RULES_FILE holds a JSON list of rules, tried in order against the
text of the LAST message of each request (its text blocks and tool results
joined). A rule is {"contains": "...", "tool": NAME, "input": {...}} (answer
with that tool call, when the request offers the tool) or {"contains": "...",
"text": "..."} (answer with that text); each fires at most "times" times (1
unless given). A request no rule answers gets the text "ok". Every request is
appended to LOG_FILE as one JSON line: its arrival time, how many messages it
carried, the last message's text, and the whole request.

It runs as its own process, started and stopped by claude_pty_harness.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT_FILE, LOG, RULES_FILE = sys.argv[1], sys.argv[2], sys.argv[3]
with open(RULES_FILE) as fh:
    RULES: list[dict[str, Any]] = json.load(fh)
for rule in RULES:
    rule.setdefault("times", 1)
IDS = itertools.count(1)


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def start() -> bytes:
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": f"msg_{next(IDS)}",
                "type": "message",
                "role": "assistant",
                "model": "claude-mock",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    )


def end(reason: str) -> bytes:
    return sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
    ) + sse("message_stop", {"type": "message_stop"})


def text(t: str) -> bytes:
    return (
        start()
        + sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        + sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": t},
            },
        )
        + sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        + end("end_turn")
    )


def tool(name: str, inp: dict[str, Any]) -> bytes:
    return (
        start()
        + sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": f"toolu_mock{next(IDS)}",
                    "name": name,
                    "input": {},
                },
            },
        )
        + sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)},
            },
        )
        + sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        + end("tool_use")
    )


def flat(content: Any) -> str:
    if isinstance(content, str):
        return content
    out: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            out.append(flat(block.get("content")))
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            req = json.loads(body)
        except ValueError:
            req = {}
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens": 1}')
            return
        msgs = req.get("messages", [])
        last = flat(msgs[-1].get("content")) if msgs else ""
        tools = [t.get("name") for t in req.get("tools", []) or []]
        payload, fired = None, None
        for rule in RULES:
            if (
                rule["times"] > 0
                and rule["contains"] in last
                and ("tool" not in rule or rule["tool"] in tools)
            ):
                rule["times"] -= 1
                fired = rule["contains"]
                payload = (
                    tool(rule["tool"], rule["input"])
                    if "tool" in rule
                    else text(rule["text"])
                )
                break
        if payload is None:
            payload = text("ok")
        with open(LOG, "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "t": time.time(),
                        "nmsg": len(msgs),
                        "last": last,
                        "rule": fired,
                        "request": req,
                    }
                )
                + "\n"
            )
        if req.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "id": "msg_x",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-mock",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode()
        )


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(PORT_FILE + ".tmp", "w") as fh:
    fh.write(str(server.server_address[1]))
os.replace(PORT_FILE + ".tmp", PORT_FILE)
server.serve_forever()
