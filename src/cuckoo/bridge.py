#!/usr/bin/env python3
"""Cuckoo bridge: the MCP server injected INTO Codex subagents.

Codex spawns one instance per thread (configured via ``mcp_servers.*``
overrides). It exposes two tools to the Codex agent:

- ``send_message`` — fire-and-forget update to the Claude Code orchestrator
- ``ask`` — blocks until the orchestrator answers, returns the answer

Both relay over a unix socket (``CUCKOO_BRIDGE_SOCKET``) to the cuckoo host
process, attributing calls via the ``x-codex-turn-metadata`` that Codex
attaches to every ``tools/call`` (session/thread/turn ids).

Deliberately stdlib-only and single-file: it must run under any ``python3``
without the cuckoo package or its virtualenv.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

ASK_TIMEOUT = float(os.environ.get("CUCKOO_ASK_TIMEOUT", "3300"))
MESSAGE_TIMEOUT = 15.0

TOOLS = [
    {
        "name": "send_message",
        "description": (
            "Send a message to the Claude Code orchestrator that manages you. "
            "Use it to report significant findings, progress milestones, or "
            "blockers while you keep working. Fire-and-forget: it does not "
            "return an answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message to deliver."},
                "kind": {
                    "type": "string",
                    "enum": ["progress", "finding", "blocker", "info"],
                    "description": "What kind of update this is (default: info).",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "ask",
        "description": (
            "Ask the Claude Code orchestrator a question and WAIT for the "
            "answer. Only use this when you genuinely cannot proceed without "
            "a decision; prefer making reasonable assumptions. The reply may "
            "take a while."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to ask."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional multiple-choice answers.",
                },
            },
            "required": ["question"],
        },
    },
]


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _reply(msg_id: Any, result: Any = None, *, error: dict[str, Any] | None = None) -> None:
    if error is not None:
        _send({"jsonrpc": "2.0", "id": msg_id, "error": error})
    else:
        _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _relay(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One request/response round trip to the cuckoo host."""
    path = os.environ.get("CUCKOO_BRIDGE_SOCKET", "")
    if not path:
        return {"error": "CUCKOO_BRIDGE_SOCKET is not set"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps(payload) + "\n").encode())
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        return json.loads(buffer) if buffer.strip() else {"error": "empty reply"}
    except (OSError, json.JSONDecodeError, TimeoutError) as exc:
        return {"error": f"orchestrator unreachable: {exc}"}


def _turn_meta(params: dict[str, Any]) -> dict[str, Any]:
    meta = params.get("_meta") or {}
    turn_meta = meta.get("x-codex-turn-metadata") or {}
    return turn_meta if isinstance(turn_meta, dict) else {}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    meta = _turn_meta(params)
    if name == "send_message":
        reply = _relay(
            {
                "op": "message",
                "meta": meta,
                "text": arguments.get("text", ""),
                "kind": arguments.get("kind", "info"),
            },
            MESSAGE_TIMEOUT,
        )
        if reply.get("ok"):
            return _text_result("Delivered to the orchestrator.")
        return _text_result(f"Delivery failed: {reply.get('error', reply)}", is_error=True)
    if name == "ask":
        reply = _relay(
            {
                "op": "ask",
                "meta": meta,
                "question": arguments.get("question", ""),
                "options": arguments.get("options"),
            },
            ASK_TIMEOUT,
        )
        answer = reply.get("answer")
        if answer is not None:
            return _text_result(str(answer))
        return _text_result(
            f"No answer received: {reply.get('error', 'timed out')}. "
            "Proceed with your best judgment.",
            is_error=True,
        )
    return _text_result(f"Unknown tool: {name}", is_error=True)


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            _reply(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cuckoo_bridge", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            _reply(msg_id, _handle_tool_call(params))
        elif method == "ping":
            _reply(msg_id, {})
        elif msg_id is not None:
            _reply(msg_id, error={"code": -32601, "message": f"unsupported method: {method}"})
        # notifications (no id) are ignored


if __name__ == "__main__":
    main()
