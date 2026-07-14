"""Bridge subprocess tests: speak MCP to it over stdio, catch relays on a
unix socket acting as the cuckoo host."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

BRIDGE = str(Path(__file__).parents[2] / "src" / "cuckoo" / "bridge.py")


class FakeHost:
    """Unix socket endpoint standing in for the AgentManager."""

    def __init__(self):
        self.received: list[dict] = []
        self.answer = "use pytest"
        directory = tempfile.mkdtemp(dir="/tmp", prefix="cuckoo-test-")
        self.path = str(Path(directory) / "host.sock")
        self._server = None

    async def start(self):
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)

    async def _handle(self, reader, writer):
        line = await reader.readline()
        if not line:
            return
        message = json.loads(line)
        self.received.append(message)
        if message["op"] == "message":
            reply = {"ok": True}
        elif message["op"] == "ask":
            reply = {"answer": self.answer}
        else:
            reply = {"error": "unknown"}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


class BridgeProcess:
    def __init__(self, process):
        self.process = process
        self._next_id = 1

    async def request(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        line = json.dumps(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        )
        self.process.stdin.write((line + "\n").encode())
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), timeout=10)
        reply = json.loads(raw)
        assert reply["id"] == msg_id
        return reply


TURN_META = {
    "_meta": {
        "x-codex-turn-metadata": {
            "session_id": "s1",
            "thread_id": "thread-42",
            "turn_id": "turn-7",
        }
    }
}


@pytest.fixture
async def rig():
    host = FakeHost()
    await host.start()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        BRIDGE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"CUCKOO_BRIDGE_SOCKET": host.path, "PATH": "/usr/bin:/bin"},
    )
    bridge = BridgeProcess(process)
    yield host, bridge
    process.kill()
    await process.wait()
    await host.stop()


async def test_initialize_and_tools_list(rig):
    _, bridge = rig
    reply = await bridge.request(
        "initialize",
        {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex-mcp-client"}},
    )
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["serverInfo"]["name"] == "cuckoo_bridge"

    reply = await bridge.request("tools/list")
    names = [tool["name"] for tool in reply["result"]["tools"]]
    assert names == ["send_message", "ask"]


async def test_send_message_relays_with_turn_metadata(rig):
    host, bridge = rig
    reply = await bridge.request(
        "tools/call",
        {
            "name": "send_message",
            "arguments": {"text": "half way there", "kind": "progress"},
            **TURN_META,
        },
    )
    assert reply["result"]["isError"] is False
    assert "Delivered" in reply["result"]["content"][0]["text"]
    assert host.received == [
        {
            "op": "message",
            "meta": {"session_id": "s1", "thread_id": "thread-42", "turn_id": "turn-7"},
            "text": "half way there",
            "kind": "progress",
        }
    ]


async def test_ask_blocks_and_returns_answer(rig):
    host, bridge = rig
    reply = await bridge.request(
        "tools/call",
        {
            "name": "ask",
            "arguments": {"question": "unit test framework?", "options": ["pytest", "unittest"]},
            **TURN_META,
        },
    )
    assert reply["result"]["isError"] is False
    assert reply["result"]["content"][0]["text"] == "use pytest"
    assert host.received[0]["op"] == "ask"
    assert host.received[0]["options"] == ["pytest", "unittest"]


async def test_unreachable_host_degrades_gracefully(rig):
    host, bridge = rig
    await host.stop()
    reply = await bridge.request(
        "tools/call",
        {"name": "send_message", "arguments": {"text": "anyone there?"}, **TURN_META},
    )
    assert reply["result"]["isError"] is True
    assert "unreachable" in reply["result"]["content"][0]["text"]


async def test_unknown_method_and_ping(rig):
    _, bridge = rig
    reply = await bridge.request("ping")
    assert reply["result"] == {}
    reply = await bridge.request("resources/list")
    assert reply["error"]["code"] == -32601
