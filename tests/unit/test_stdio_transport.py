import asyncio
import sys
import time
from pathlib import Path

import pytest

from cuckoo.codex._rpc.connection import RpcConnection
from cuckoo.codex._rpc.transport import StdioProcessTransport
from cuckoo.codex.config import ShutdownPolicy
from cuckoo.codex.errors import ProcessExitedError, ProcessSpawnError

FAKE_CODEX = str(Path(__file__).parent.parent / "fake_codex.py")


async def spawn_fake(mode: str = "echo", **kwargs) -> StdioProcessTransport:
    return await StdioProcessTransport.spawn(
        [sys.executable, FAKE_CODEX, mode], **kwargs
    )


@pytest.fixture
async def connection():
    transport = await spawn_fake()
    conn = RpcConnection(transport)
    conn.start()
    yield conn
    await conn.aclose()


async def test_echo_over_real_pipes(connection):
    result = await connection.request("echo", {"hello": "world"}, timeout=5)
    assert result == {"hello": "world"}


async def test_payload_larger_than_default_stream_limit(connection):
    size = 200_000  # > asyncio's 64 KiB default readline limit
    result = await connection.request("big", {"size": size}, timeout=10)
    assert len(result["blob"]) == size


async def test_notification_from_process(connection):
    seen: list[str] = []
    connection._on_notification = lambda frame: seen.append(frame.method)
    await connection.request("notify-back", {"method": "thread/started"}, timeout=5)
    assert seen == ["thread/started"]


async def test_process_death_fails_pending_with_stderr_tail():
    transport = await spawn_fake()
    conn = RpcConnection(transport)
    conn.start()
    try:
        with pytest.raises(ProcessExitedError) as exc_info:
            await conn.request("die", {"code": 3}, timeout=5)
        assert exc_info.value.exit_code == 3
        assert "dying" in exc_info.value.stderr_tail
    finally:
        await conn.aclose()


async def test_spawn_failure():
    with pytest.raises(ProcessSpawnError):
        await StdioProcessTransport.spawn(["/nonexistent/binary-xyz"])


async def test_clean_close_on_stdin_eof():
    transport = await spawn_fake("echo")
    started = time.monotonic()
    await transport.aclose()
    assert time.monotonic() - started < 2.0
    assert transport.returncode == 0


async def test_close_escalates_to_sigterm():
    transport = await spawn_fake(
        "ignore-eof", shutdown=ShutdownPolicy(stdin_grace=0.2, term_grace=2.0)
    )
    await transport.aclose()
    assert transport.returncode == -15  # SIGTERM


@pytest.mark.timeout(15)
async def test_close_escalates_to_sigkill():
    transport = await spawn_fake(
        "ignore-sigterm", shutdown=ShutdownPolicy(stdin_grace=0.2, term_grace=0.3)
    )
    # Give the child a moment to install its SIGTERM handler.
    await asyncio.sleep(0.3)
    await transport.aclose()
    assert transport.returncode == -9  # SIGKILL
