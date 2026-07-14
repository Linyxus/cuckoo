"""JSON-RPC connection: correlation, dispatch, retries, lifecycle."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Awaitable, Callable

from cuckoo.codex.config import UNSET, DEFAULT_REQUEST_TIMEOUT, RetryPolicy, _Unset
from cuckoo.codex.errors import (
    ConnectionClosedError,
    MethodNotSupportedError,
    ProtocolDecodeError,
    RequestTimeoutError,
    RpcError,
    ServerOverloadedError,
    TransportError,
)

from .codec import (
    RpcErrorResponse,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    decode_line,
    encode,
)
from .transport import Transport

logger = logging.getLogger("cuckoo.codex.rpc")

#: The app-server rejects work with this code when overloaded/busy;
#: the request was not processed, so retrying is safe.
OVERLOADED_CODE = -32001
METHOD_NOT_FOUND_CODE = -32601
INTERNAL_ERROR_CODE = -32603

_UNSUPPORTED_METHOD_RE = re.compile(r"unknown (variant|method)", re.IGNORECASE)

NotificationHandler = Callable[[RpcNotification], None]
ServerRequestHandler = Callable[[RpcRequest], Awaitable[Any]]
ClosedHandler = Callable[[TransportError | None], None]


class RpcHandlerError(Exception):
    """Raised by a server-request handler to produce a JSON-RPC error reply."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def classify_rpc_error(
    code: int, message: str, data: Any = None, *, method: str | None = None
) -> RpcError:
    """Map a JSON-RPC error response onto the library's error taxonomy."""
    if code == METHOD_NOT_FOUND_CODE or _UNSUPPORTED_METHOD_RE.search(message):
        return MethodNotSupportedError(message, code=code, data=data, method=method)
    if code == OVERLOADED_CODE:
        return ServerOverloadedError(message, code=code, data=data, method=method)
    return RpcError(message, code=code, data=data, method=method)


class RpcConnection:
    """Bidirectional JSON-RPC 2.0 endpoint over a :class:`Transport`.

    Outgoing requests are correlated by monotonically increasing integer
    ids. Incoming notifications are dispatched synchronously, in arrival
    order, on the reader task. Incoming server requests (approvals — they
    may take minutes) run as separate tasks so they never block the reader.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry: RetryPolicy | None = None,
        on_notification: NotificationHandler | None = None,
        on_server_request: ServerRequestHandler | None = None,
        on_closed: ClosedHandler | None = None,
    ) -> None:
        self._transport = transport
        self._request_timeout = request_timeout
        self._retry = retry or RetryPolicy()
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_closed = on_closed
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[RpcResponse | RpcErrorResponse]] = {}
        self._send_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._server_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._close_reason: TransportError | None = None

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="cuckoo-rpc-reader"
            )

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def close_reason(self) -> TransportError | None:
        return self._close_reason

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None | _Unset = UNSET,
    ) -> Any:
        """Send a request and await its result.

        Retries transparently on -32001 (overloaded) per the retry policy;
        raises the classified :class:`RpcError` subclass on other errors.
        ``timeout=None`` disables the per-request timeout.
        """
        effective_timeout = self._request_timeout if timeout is UNSET else timeout
        last_error: ServerOverloadedError | None = None
        for attempt in range(self._retry.max_attempts):
            if attempt:
                delay = self._retry.delay_for(attempt - 1)
                delay *= 1 + random.uniform(-self._retry.jitter, self._retry.jitter)
                await asyncio.sleep(delay)
            try:
                return await self._request_once(method, params, effective_timeout)
            except ServerOverloadedError as exc:
                last_error = exc
                logger.debug(
                    "%s rejected with -32001 (attempt %d/%d)",
                    method,
                    attempt + 1,
                    self._retry.max_attempts,
                )
        assert last_error is not None
        raise last_error

    async def _request_once(
        self, method: str, params: Any, timeout: float | None
    ) -> Any:
        self._ensure_open()
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[RpcResponse | RpcErrorResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            await self._send(RpcRequest(id=request_id, method=method, params=params))
            async with asyncio.timeout(timeout):
                frame = await future
        except TimeoutError:
            raise RequestTimeoutError(
                f"no response to {method!r} within {timeout}s", method=method
            ) from None
        finally:
            self._pending.pop(request_id, None)
        if isinstance(frame, RpcErrorResponse):
            raise classify_rpc_error(
                frame.code, frame.message, frame.data, method=method
            )
        return frame.result

    async def notify(self, method: str, params: Any = None) -> None:
        self._ensure_open()
        await self._send(RpcNotification(method=method, params=params))

    async def aclose(self) -> None:
        """Close the connection and its transport; idempotent."""
        self._finish(None)
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        await self._transport.aclose()

    async def _send(self, frame: RpcRequest | RpcNotification | RpcResponse | RpcErrorResponse) -> None:
        async with self._send_lock:
            await self._transport.send_line(encode(frame))

    def _ensure_open(self) -> None:
        if self._closed:
            raise self._close_reason or ConnectionClosedError("connection is closed")

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._transport.receive_line()
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    frame = decode_line(line)
                except ProtocolDecodeError as exc:
                    logger.warning("dropping undecodable line: %s", exc)
                    continue
                self._dispatch(frame)
        except ProtocolDecodeError as exc:
            # Oversized line: the pipe state is unrecoverable.
            self._finish(TransportError(str(exc)))
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("rpc reader loop crashed")
            self._finish(TransportError("rpc reader loop crashed"))
            return
        self._finish(self._transport.exit_error())

    def _dispatch(
        self, frame: RpcRequest | RpcNotification | RpcResponse | RpcErrorResponse
    ) -> None:
        match frame:
            case RpcResponse(id=frame_id) | RpcErrorResponse(id=frame_id):
                future = self._pending.pop(frame_id, None)
                if future is None:
                    logger.debug("response for unknown request id %r", frame_id)
                elif not future.done():
                    future.set_result(frame)
            case RpcRequest():
                task = asyncio.create_task(
                    self._handle_server_request(frame),
                    name=f"cuckoo-server-request-{frame.method}",
                )
                self._server_tasks.add(task)
                task.add_done_callback(self._server_tasks.discard)
            case RpcNotification():
                if self._on_notification is not None:
                    try:
                        self._on_notification(frame)
                    except Exception:
                        logger.exception(
                            "notification handler failed for %s", frame.method
                        )

    async def _handle_server_request(self, request: RpcRequest) -> None:
        reply: RpcResponse | RpcErrorResponse
        if self._on_server_request is None:
            reply = RpcErrorResponse(
                id=request.id,
                code=METHOD_NOT_FOUND_CODE,
                message=f"Unsupported server request: {request.method}",
            )
        else:
            try:
                result = await self._on_server_request(request)
                reply = RpcResponse(id=request.id, result=result)
            except RpcHandlerError as exc:
                reply = RpcErrorResponse(
                    id=request.id, code=exc.code, message=str(exc), data=exc.data
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("server request handler failed for %s", request.method)
                reply = RpcErrorResponse(
                    id=request.id, code=INTERNAL_ERROR_CODE, message=str(exc)
                )
        try:
            await self._send(reply)
        except TransportError:
            logger.debug("could not reply to %s: transport closed", request.method)

    def _finish(self, reason: TransportError | None) -> None:
        """Mark closed and fail everything in flight. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason
        error = reason or ConnectionClosedError("connection closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for task in self._server_tasks:
            task.cancel()
        if self._on_closed is not None:
            try:
                self._on_closed(reason)
            except Exception:
                logger.exception("on_closed handler failed")
