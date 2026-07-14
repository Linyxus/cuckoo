import asyncio

import pytest

from cuckoo.codex._rpc.codec import RpcNotification, RpcRequest
from cuckoo.codex._rpc.connection import RpcConnection, RpcHandlerError
from cuckoo.codex.config import RetryPolicy
from cuckoo.codex.errors import (
    ConnectionClosedError,
    MethodNotSupportedError,
    ProcessExitedError,
    RequestTimeoutError,
    RpcError,
    ServerOverloadedError,
)
from tests.fake_server import ErrorReply, FakeAppServer

FAST_RETRY = RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=0.02)


@pytest.fixture
async def rig():
    """A started (fake server, connection) pair, plus captured callbacks."""
    fake, client_transport = FakeAppServer.create()
    notifications: list[RpcNotification] = []
    closed: list[object] = []
    connection = RpcConnection(
        client_transport,
        request_timeout=5.0,
        retry=FAST_RETRY,
        on_notification=notifications.append,
        on_closed=closed.append,
    )
    fake.start()
    connection.start()
    try:
        yield fake, connection, notifications, closed
    finally:
        await connection.aclose()
        await fake.stop()


async def test_request_response(rig):
    fake, connection, _, _ = rig
    fake.on("echo", lambda _fake, req: {"got": req.params})
    result = await connection.request("echo", {"x": 1})
    assert result == {"got": {"x": 1}}


async def test_concurrent_requests_correlate(rig):
    fake, connection, _, _ = rig

    async def slow_then_fast(fake_server, request):
        if request.params["which"] == "slow":
            await asyncio.sleep(0.05)
        return {"which": request.params["which"]}

    fake.on("job", slow_then_fast)
    slow, fast = await asyncio.gather(
        connection.request("job", {"which": "slow"}),
        connection.request("job", {"which": "fast"}),
    )
    assert slow == {"which": "slow"}
    assert fast == {"which": "fast"}


async def test_notifications_dispatch_in_order(rig):
    fake, connection, notifications, _ = rig
    fake.on("ping")
    for index in range(5):
        await fake.notify("counter", {"n": index})
    await connection.request("ping")  # fence: all prior lines processed
    assert [n.params["n"] for n in notifications] == [0, 1, 2, 3, 4]


async def test_rpc_error_classification(rig):
    fake, connection, _, _ = rig
    fake.on("plain-error", ErrorReply(-32000, "boom"))
    fake.on("not-found", ErrorReply(-32601, "method not found"))
    fake.on("old-cli", ErrorReply(-32602, "unknown variant `thread/name/set`"))

    with pytest.raises(RpcError) as exc_info:
        await connection.request("plain-error")
    assert exc_info.value.code == -32000
    assert exc_info.value.method == "plain-error"

    with pytest.raises(MethodNotSupportedError):
        await connection.request("not-found")
    with pytest.raises(MethodNotSupportedError):
        await connection.request("old-cli")


async def test_overloaded_retries_then_succeeds(rig):
    fake, connection, _, _ = rig
    attempts = 0

    def flaky(fake_server, request):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return ErrorReply(-32001, "Server overloaded; retry later")
        return {"ok": True}

    fake.on("flaky", flaky)
    assert await connection.request("flaky") == {"ok": True}
    assert attempts == 3


async def test_overloaded_exhausts_retries(rig):
    fake, connection, _, _ = rig
    fake.on("busy", ErrorReply(-32001, "Server overloaded; retry later"))
    with pytest.raises(ServerOverloadedError):
        await connection.request("busy")
    assert len([r for r in fake.requests if r.method == "busy"]) == FAST_RETRY.max_attempts


async def test_request_timeout(rig):
    fake, connection, _, _ = rig
    fake.on("black-hole", lambda _fake, _req: ...)
    with pytest.raises(RequestTimeoutError):
        await connection.request("black-hole", timeout=0.05)


async def test_server_request_dispatched_to_handler():
    fake, client_transport = FakeAppServer.create()

    async def approve(request: RpcRequest):
        assert request.method == "execCommandApproval"
        return {"decision": "approved"}

    connection = RpcConnection(client_transport, on_server_request=approve)
    fake.start()
    connection.start()
    try:
        reply = await fake.request("execCommandApproval", {"command": ["ls"]})
        assert reply.result == {"decision": "approved"}
    finally:
        await connection.aclose()
        await fake.stop()


async def test_server_request_without_handler_gets_32601(rig):
    fake, _, _, _ = rig
    reply = await fake.request("execCommandApproval", {})
    assert reply.code == -32601


async def test_server_request_handler_error_codes():
    fake, client_transport = FakeAppServer.create()

    async def handler(request: RpcRequest):
        if request.method == "custom-error":
            raise RpcHandlerError(-32099, "handled badly")
        raise ValueError("unexpected")

    connection = RpcConnection(client_transport, on_server_request=handler)
    fake.start()
    connection.start()
    try:
        reply = await fake.request("custom-error", {})
        assert (reply.code, reply.message) == (-32099, "handled badly")
        reply = await fake.request("anything-else", {})
        assert reply.code == -32603
    finally:
        await connection.aclose()
        await fake.stop()


async def test_peer_close_fails_pending_and_reports(rig):
    fake, connection, _, closed = rig
    fake.on("hang", lambda _fake, _req: ...)
    pending = asyncio.ensure_future(connection.request("hang"))
    await fake.wait_for_request("hang")
    await fake.transport.aclose()
    with pytest.raises(ConnectionClosedError):
        await pending
    assert closed == [None]
    with pytest.raises(ConnectionClosedError):
        await connection.request("anything")


async def test_process_death_surfaces_exit_error():
    fake, client_transport = FakeAppServer.create()
    death = ProcessExitedError("gone", exit_code=1, stderr_tail="panic!")
    client_transport.set_exit_error(death)
    closed: list[object] = []
    connection = RpcConnection(client_transport, on_closed=closed.append)
    fake.start()
    fake.on("hang", lambda _fake, _req: ...)
    connection.start()
    try:
        pending = asyncio.ensure_future(connection.request("hang"))
        await fake.wait_for_request("hang")
        await fake.transport.aclose()
        with pytest.raises(ProcessExitedError) as exc_info:
            await pending
        assert exc_info.value.stderr_tail == "panic!"
        assert closed == [death]
    finally:
        await connection.aclose()
        await fake.stop()
