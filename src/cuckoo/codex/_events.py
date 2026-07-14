"""EventBus: synchronous fan-out to callbacks and async-iterator streams.

``emit`` is called from the connection's reader task, so it must never
block: streams are backed by queues that are unbounded by default, or
bounded with an explicit drop policy for monitor-style consumers.
Callback exceptions are logged, never propagated into the reader loop.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any, Awaitable, Callable

from cuckoo.codex.protocol.events import Event

logger = logging.getLogger("cuckoo.codex.events")

EventCallback = Callable[[Event], None | Awaitable[None]]
EventPredicate = Callable[[Event], bool]

_CLOSED = object()


class OverflowPolicy(enum.Enum):
    """What a bounded stream does when its consumer falls behind."""

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"


class Subscription:
    """Handle for a callback registration; also a context manager."""

    def __init__(self, bus: EventBus, entry: _CallbackEntry) -> None:
        self._bus = bus
        self._entry = entry

    def unsubscribe(self) -> None:
        self._bus._callbacks.discard(self._entry)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.unsubscribe()


class _CallbackEntry:
    __slots__ = ("callback", "kinds", "predicate")

    def __init__(
        self,
        callback: EventCallback,
        kinds: tuple[type[Event], ...] | None,
        predicate: EventPredicate | None,
    ) -> None:
        self.callback = callback
        self.kinds = kinds
        self.predicate = predicate

    def matches(self, event: Event) -> bool:
        if self.kinds is not None and not isinstance(event, self.kinds):
            return False
        return self.predicate is None or self.predicate(event)


class EventStream:
    """Async iterator over matching events; subscribes at construction so
    events between creation and first iteration are not lost."""

    def __init__(
        self,
        bus: EventBus,
        *,
        kinds: tuple[type[Event], ...] | None = None,
        predicate: EventPredicate | None = None,
        queue_size: int | None = None,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        stop_after: EventPredicate | None = None,
    ) -> None:
        self._bus = bus
        self._kinds = kinds
        self._predicate = predicate
        self._stop_after = stop_after
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_size or 0)
        self._overflow = overflow
        self._closed = False
        bus._streams.add(self)

    def _offer(self, event: Event) -> None:
        if self._closed:
            return
        if self._kinds is not None and not isinstance(event, self._kinds):
            return
        if self._predicate is not None and not self._predicate(event):
            return
        self._put(event)
        if self._stop_after is not None and self._stop_after(event):
            self.close()

    def _put(self, entry: Any) -> None:
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            if self._overflow is OverflowPolicy.DROP_NEWEST:
                logger.debug("event stream full; dropping newest event")
                return
            try:
                self._queue.get_nowait()
                logger.debug("event stream full; dropped oldest event")
            except asyncio.QueueEmpty:  # pragma: no cover - race guard
                pass
            try:
                self._queue.put_nowait(entry)
            except asyncio.QueueFull:  # pragma: no cover - race guard
                pass

    def close(self) -> None:
        """Detach from the bus; iteration ends after draining queued events."""
        if not self._closed:
            self._closed = True
            self._bus._streams.discard(self)
            # Wake a blocked consumer. When the queue is full the consumer
            # is not blocked; it observes ``_closed`` after draining.
            try:
                self._queue.put_nowait(_CLOSED)
            except asyncio.QueueFull:
                pass

    def __aiter__(self) -> EventStream:
        return self

    async def __anext__(self) -> Event:
        while True:
            try:
                entry = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._closed:
                    raise StopAsyncIteration from None
                entry = await self._queue.get()
            if entry is _CLOSED:
                raise StopAsyncIteration
            return entry

    async def next(self, *, timeout: float | None = None) -> Event:
        """Await the next matching event; :class:`TimeoutError` on timeout,
        :class:`StopAsyncIteration` if the stream closed."""
        async with asyncio.timeout(timeout):
            return await self.__anext__()

    def __enter__(self) -> EventStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._callbacks: set[_CallbackEntry] = set()
        self._streams: set[EventStream] = set()
        self._closed = False

    def emit(self, event: Event) -> None:
        for entry in tuple(self._callbacks):
            if not entry.matches(event):
                continue
            try:
                result = entry.callback(event)
                if asyncio.iscoroutine(result):
                    task = asyncio.ensure_future(result)
                    task.add_done_callback(_log_callback_task_error)
            except Exception:
                logger.exception("event callback failed for %r", type(event).__name__)
        for stream in tuple(self._streams):
            stream._offer(event)

    def subscribe(
        self,
        callback: EventCallback,
        *,
        kinds: tuple[type[Event], ...] | None = None,
        predicate: EventPredicate | None = None,
    ) -> Subscription:
        entry = _CallbackEntry(callback, kinds, predicate)
        self._callbacks.add(entry)
        return Subscription(self, entry)

    def stream(
        self,
        *,
        kinds: tuple[type[Event], ...] | None = None,
        predicate: EventPredicate | None = None,
        queue_size: int | None = None,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
        stop_after: EventPredicate | None = None,
    ) -> EventStream:
        return EventStream(
            self,
            kinds=kinds,
            predicate=predicate,
            queue_size=queue_size,
            overflow=overflow,
            stop_after=stop_after,
        )

    def close(self) -> None:
        """Terminate all streams (used when the client shuts down)."""
        self._closed = True
        for stream in tuple(self._streams):
            stream.close()
        self._callbacks.clear()


def _log_callback_task_error(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("async event callback failed", exc_info=task.exception())
