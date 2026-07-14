import asyncio

import pytest

from cuckoo.codex._events import EventBus, OverflowPolicy
from cuckoo.codex.protocol.events import ThreadClosed, TurnStarted, UnknownEvent


def closed(thread_id="t1"):
    return ThreadClosed(thread_id=thread_id)


async def test_callback_filtering_by_kind_and_predicate():
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append, kinds=(ThreadClosed,))
    bus.emit(UnknownEvent(method="x"))
    bus.emit(closed())
    assert [type(e) for e in seen] == [ThreadClosed]

    filtered = []
    bus.subscribe(
        filtered.append,
        kinds=(ThreadClosed,),
        predicate=lambda e: e.thread_id == "t2",
    )
    bus.emit(closed("t1"))
    bus.emit(closed("t2"))
    assert [e.thread_id for e in filtered] == ["t2"]


async def test_callback_exception_does_not_break_emit():
    bus = EventBus()
    seen = []

    def bad(event):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(seen.append)
    bus.emit(closed())
    assert len(seen) == 1


async def test_async_callback_scheduled():
    bus = EventBus()
    seen = []

    async def handler(event):
        seen.append(event)

    bus.subscribe(handler)
    bus.emit(closed())
    await asyncio.sleep(0)
    assert len(seen) == 1


async def test_stream_receives_events_emitted_after_creation():
    bus = EventBus()
    stream = bus.stream(kinds=(ThreadClosed,))
    bus.emit(closed("t1"))
    bus.emit(closed("t2"))
    assert (await stream.next(timeout=1)).thread_id == "t1"
    assert (await stream.next(timeout=1)).thread_id == "t2"
    stream.close()


async def test_stream_ends_on_bus_close():
    bus = EventBus()
    stream = bus.stream()
    bus.emit(closed())
    bus.close()
    collected = [event async for event in stream]
    assert len(collected) == 1


async def test_stream_stop_after_terminal_event():
    bus = EventBus()
    stream = bus.stream(stop_after=lambda e: isinstance(e, ThreadClosed))
    bus.emit(UnknownEvent(method="a"))
    bus.emit(closed())
    bus.emit(UnknownEvent(method="after-terminal"))
    collected = [event async for event in stream]
    assert [type(e).__name__ for e in collected] == ["UnknownEvent", "ThreadClosed"]


async def test_bounded_stream_drop_oldest():
    bus = EventBus()
    stream = bus.stream(queue_size=2, overflow=OverflowPolicy.DROP_OLDEST)
    for tid in ("t1", "t2", "t3"):
        bus.emit(closed(tid))
    stream.close()
    collected = [e.thread_id async for e in stream]
    assert collected == ["t2", "t3"]


async def test_bounded_stream_drop_newest():
    bus = EventBus()
    stream = bus.stream(queue_size=2, overflow=OverflowPolicy.DROP_NEWEST)
    for tid in ("t1", "t2", "t3"):
        bus.emit(closed(tid))
    stream.close()
    collected = [e.thread_id async for e in stream]
    assert collected == ["t1", "t2"]


async def test_stream_next_timeout():
    bus = EventBus()
    stream = bus.stream()
    with pytest.raises(TimeoutError):
        await stream.next(timeout=0.05)


async def test_unsubscribe_and_context_managers():
    bus = EventBus()
    seen = []
    subscription = bus.subscribe(seen.append)
    subscription.unsubscribe()
    bus.emit(closed())
    assert seen == []

    with bus.stream() as stream:
        bus.emit(closed())
        assert (await stream.next(timeout=1)) is not None
    # closed on exit: emitting more does not grow the queue
    bus.emit(closed())
    remaining = [event async for event in stream]
    assert remaining == []
