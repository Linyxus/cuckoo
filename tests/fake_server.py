"""In-process scripted codex app-server for hermetic tests.

Sits on the peer end of a ``MemoryTransport.pair()`` and answers JSON-RPC
requests from registered handlers. Unregistered methods get ``-32601`` so
unexpected traffic surfaces as a test failure rather than a hang.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cuckoo.codex._rpc.codec import (
    Frame,
    RpcErrorResponse,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    decode_line,
    encode,
)
from cuckoo.codex._rpc.transport import MemoryTransport


@dataclass(frozen=True)
class ErrorReply:
    """Register this as a handler value to answer with a JSON-RPC error."""

    code: int
    message: str
    data: Any = None


Handler = Callable[["FakeAppServer", RpcRequest], Any]
HandlerValue = dict[str, Any] | ErrorReply | Handler | None


DEFAULT_INITIALIZE_RESULT = {
    "userAgent": "codex/0.144.1-fake",
    "codexHome": "/tmp/fake-codex-home",
}


def thread_payload(thread_id: str = "thread-1", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": thread_id,
        "sessionId": thread_id,
        "preview": "",
        "cwd": "/tmp/workdir",
        "status": {"type": "idle"},
        "ephemeral": False,
        "turns": [],
        "cliVersion": "0.144.1-fake",
        "modelProvider": "openai",
        "source": "appServer",
        "createdAt": 1,
        "updatedAt": 1,
        "name": None,
    }
    payload.update(overrides)
    return payload


def thread_start_result(thread_id: str = "thread-1", **overrides: Any) -> dict[str, Any]:
    return {
        "thread": thread_payload(thread_id, **overrides),
        "model": "gpt-5.6-fake",
        "modelProvider": "openai",
        "cwd": "/tmp/workdir",
        "sandbox": {"mode": "read-only"},
        "approvalPolicy": "never",
        "approvalsReviewer": "client",
    }


def turn_payload(
    turn_id: str = "turn-1",
    status: str = "inProgress",
    items: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"id": turn_id, "items": items or [], "status": status, "error": error}


def agent_message_item(
    item_id: str = "item-msg", text: str = "Done!", phase: str | None = "final_answer"
) -> dict[str, Any]:
    return {"type": "agentMessage", "id": item_id, "text": text, "phase": phase}


def command_item(
    item_id: str = "item-cmd", status: str = "completed", **overrides: Any
) -> dict[str, Any]:
    payload = {
        "type": "commandExecution",
        "id": item_id,
        "command": "ls -la",
        "cwd": "/tmp/workdir",
        "commandActions": [],
        "status": status,
        "aggregatedOutput": "file.txt\n" if status == "completed" else "",
        "exitCode": 0 if status == "completed" else None,
    }
    payload.update(overrides)
    return payload


def token_usage_payload(total: int = 100) -> dict[str, Any]:
    breakdown = {
        "inputTokens": total - 20,
        "cachedInputTokens": 0,
        "outputTokens": 20,
        "reasoningOutputTokens": 5,
        "totalTokens": total,
    }
    return {"total": breakdown, "last": breakdown, "modelContextWindow": 128000}


class FakeAppServer:
    def __init__(self, transport: MemoryTransport) -> None:
        self.transport = transport
        self.requests: list[RpcRequest] = []
        self.notifications: list[RpcNotification] = []
        self._handlers: dict[str, HandlerValue] = {}
        self._request_waiters: list[tuple[str | None, asyncio.Future[RpcRequest]]] = []
        self._pending: dict[int | str, asyncio.Future[Frame]] = {}
        self._next_id = 9000
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def create(cls) -> tuple["FakeAppServer", MemoryTransport]:
        """Return a started-ready fake and the client-side transport."""
        client_side, server_side = MemoryTransport.pair()
        return cls(server_side), client_side

    def with_handshake(self) -> "FakeAppServer":
        """Register the standard initialize/initialized handshake."""
        self.on("initialize", DEFAULT_INITIALIZE_RESULT)
        return self

    def on(self, method: str, handler: HandlerValue = None) -> None:
        """Register a response for ``method``.

        ``dict``/``None`` → static result; :class:`ErrorReply` → error
        response; callable → invoked with ``(fake, request)`` and may return
        a result dict, an :class:`ErrorReply`, ``None``, or an awaitable of
        those. A callable returning ``...`` (Ellipsis) sends no reply at all
        (for timeout tests).
        """
        self._handlers[method] = handler

    def start(self) -> None:
        assert self._task is None
        self._task = asyncio.create_task(self._read_loop(), name="fake-app-server")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.transport.aclose()

    # -- scripted actions -------------------------------------------------

    async def notify(self, method: str, params: Any = None) -> None:
        await self.transport.send_line(encode(RpcNotification(method, params)))

    async def play_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        final_text: str = "Done!",
        final_phase: str | None = "final_answer",
        include_command: bool = True,
        status: str = "completed",
        error: dict[str, Any] | None = None,
    ) -> None:
        """Emit the canonical notification sequence of one turn."""
        await self.notify(
            "turn/started", {"threadId": thread_id, "turn": turn_payload(turn_id)}
        )
        items: list[dict[str, Any]] = []
        if include_command:
            await self.notify(
                "item/started",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": command_item(status="inProgress"),
                    "startedAtMs": 1,
                },
            )
            command = command_item(status="completed")
            items.append(command)
            await self.notify(
                "item/completed",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": command,
                    "completedAtMs": 2,
                },
            )
        if final_text is not None:
            message = agent_message_item(text=final_text, phase=final_phase)
            items.append(message)
            await self.notify(
                "item/completed",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": message,
                    "completedAtMs": 3,
                },
            )
        await self.notify(
            "thread/tokenUsage/updated",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "tokenUsage": token_usage_payload(),
            },
        )
        await self.notify(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": turn_payload(turn_id, status=status, items=items, error=error),
            },
        )

    async def request(self, method: str, params: Any = None, *, timeout: float = 5.0) -> Frame:
        """Send a server->client request; returns the reply frame."""
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.transport.send_line(
            encode(RpcRequest(id=request_id, method=method, params=params))
        )
        async with asyncio.timeout(timeout):
            return await future

    async def wait_for_request(
        self, method: str | None = None, *, timeout: float = 5.0
    ) -> RpcRequest:
        """Return the next (or a past) client request, optionally by method."""
        for seen in self.requests:
            if method is None or seen.method == method:
                return seen
        future: asyncio.Future[RpcRequest] = asyncio.get_running_loop().create_future()
        self._request_waiters.append((method, future))
        async with asyncio.timeout(timeout):
            return await future

    # -- internals ---------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            line = await self.transport.receive_line()
            if line is None:
                return
            frame = decode_line(line)
            match frame:
                case RpcRequest():
                    self.requests.append(frame)
                    self._wake_waiters(frame)
                    await self._respond(frame)
                case RpcNotification():
                    self.notifications.append(frame)
                case RpcResponse(id=frame_id) | RpcErrorResponse(id=frame_id):
                    pending = self._pending.pop(frame_id, None)
                    if pending is not None and not pending.done():
                        pending.set_result(frame)

    def _wake_waiters(self, request: RpcRequest) -> None:
        remaining = []
        for method, future in self._request_waiters:
            if not future.done() and (method is None or method == request.method):
                future.set_result(request)
            else:
                remaining.append((method, future))
        self._request_waiters = remaining

    async def _respond(self, request: RpcRequest) -> None:
        if request.method not in self._handlers:
            await self.transport.send_line(
                encode(
                    RpcErrorResponse(
                        id=request.id,
                        code=-32601,
                        message=f"fake server: unhandled method {request.method!r}",
                    )
                )
            )
            return
        value = self._handlers[request.method]
        if callable(value):
            value = value(self, request)
            if isinstance(value, Awaitable):
                value = await value
        if value is ...:
            return  # deliberately no reply
        if isinstance(value, ErrorReply):
            reply: Frame = RpcErrorResponse(
                id=request.id, code=value.code, message=value.message, data=value.data
            )
        else:
            reply = RpcResponse(id=request.id, result=value if value is not None else {})
        await self.transport.send_line(encode(reply))
