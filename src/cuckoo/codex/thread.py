"""Thread handle — the managed subagent."""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel

from cuckoo.codex.approvals import ApprovalHandler
from cuckoo.codex.errors import (
    MethodNotSupportedError,
    NoActiveTurnError,
    ProcessExitedError,
    ThreadClosedError,
    TurnFailedError,
)
from cuckoo.codex.protocol import methods
from cuckoo.codex.protocol.common import (
    AgentState,
    ApprovalPolicy,
    ReasoningEffort,
    SandboxMode,
    TurnStatus,
)
from cuckoo.codex.protocol.events import (
    AgentStateChanged,
    Event,
    ItemCompleted,
    ProcessExited,
    ThreadClosed,
    ThreadNameUpdated,
    ThreadTokenUsageUpdated,
    TurnCompleted,
    TurnStarted,
)
from cuckoo.codex.protocol.items import AgentMessageItem
from cuckoo.codex.protocol.models import ThreadInfo, TokenUsage, TurnInfo
from cuckoo.codex.state import next_state
from cuckoo.codex.turn import Turn, TurnResult

if TYPE_CHECKING:
    from cuckoo.codex._events import EventStream, Subscription
    from cuckoo.codex.client import CodexClient
    from cuckoo.codex.inputs import TurnInput

logger = logging.getLogger("cuckoo.codex.thread")

#: Turn-scoped events buffered until their Turn handle registers (covers the
#: race between a turn/start response and its notifications).
_TURN_EVENT_BUFFER = 256


class ThreadOptions(BaseModel):
    """Settings a thread was started with; reused for transparent resume."""

    cwd: str | None = None
    model: str | None = None
    sandbox: SandboxMode | str | None = None
    approval_policy: ApprovalPolicy | str | dict[str, Any] | None = None
    ephemeral: bool = False


class Thread:
    """A Codex subagent: one conversation thread on the app-server.

    State is maintained client-side via a pure transition function; the
    ``interrupt()`` call additionally forces ``INTERRUPTING`` until the
    server reports the terminal turn event.
    """

    def __init__(
        self,
        client: CodexClient,
        info: ThreadInfo,
        *,
        options: ThreadOptions | None = None,
        approval_handler: ApprovalHandler | None = None,
        auto_resume: bool = True,
    ) -> None:
        self._client = client
        self._info = info
        self._options = options or ThreadOptions()
        self.approval_handler = approval_handler
        self.auto_resume = auto_resume
        self._state: AgentState = AgentState.IDLE
        self._turns: dict[str, Turn] = {}
        self._current_turn: Turn | None = None
        self._token_usage: TokenUsage | None = None
        self._last_agent_message: str | None = None
        self._pending_turn_events: collections.deque[Event] = collections.deque(
            maxlen=_TURN_EVENT_BUFFER
        )

    # -- identity & snapshots -------------------------------------------------

    @property
    def id(self) -> str:
        return self._info.id

    @property
    def info(self) -> ThreadInfo:
        return self._info

    @property
    def name(self) -> str | None:
        return self._info.name

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def current_turn(self) -> Turn | None:
        return self._current_turn

    @property
    def token_usage(self) -> TokenUsage | None:
        return self._token_usage

    @property
    def last_agent_message(self) -> str | None:
        return self._last_agent_message

    def __repr__(self) -> str:
        return f"<Thread {self.id} state={self._state.value} name={self.name!r}>"

    # -- running turns ---------------------------------------------------------

    async def run(
        self,
        input: TurnInput,
        *,
        model: str | None = None,
        effort: ReasoningEffort | str | None = None,
        output_schema: dict[str, Any] | type[BaseModel] | None = None,
        approval_handler: ApprovalHandler | None = None,
        timeout: float | None = None,
        raise_on_failure: bool = True,
    ) -> TurnResult:
        """Start a turn and wait for its result.

        Cancelling this coroutine sends a best-effort ``turn/interrupt``
        before re-raising. With ``raise_on_failure`` a ``failed`` turn
        raises :class:`TurnFailedError` (interrupted turns do not).
        """
        turn = await self.start_turn(
            input,
            model=model,
            effort=effort,
            output_schema=output_schema,
            approval_handler=approval_handler,
        )
        try:
            result = await turn.result(timeout=timeout)
        except (asyncio.CancelledError, TimeoutError):
            try:
                await self._client._interrupt_turn(self.id, turn.id)
            except Exception:
                logger.debug("best-effort interrupt failed", exc_info=True)
            raise
        if raise_on_failure and result.status == TurnStatus.FAILED:
            message = result.error.message if result.error else "turn failed"
            raise TurnFailedError(message, result=result)
        return result

    async def start_turn(
        self,
        input: TurnInput,
        *,
        model: str | None = None,
        effort: ReasoningEffort | str | None = None,
        output_schema: dict[str, Any] | type[BaseModel] | None = None,
        approval_handler: ApprovalHandler | None = None,
    ) -> Turn:
        """Start a turn and return immediately with its handle."""
        await self._ensure_usable()
        schema: dict[str, Any] | None
        if isinstance(output_schema, type) and issubclass(output_schema, BaseModel):
            schema = output_schema.model_json_schema()
        else:
            schema = output_schema
        turn_info = await self._client._start_turn(
            thread_id=self.id,
            input=input,
            model=model,
            effort=effort,
            output_schema=schema,
        )
        turn = Turn(
            self._client,
            thread_id=self.id,
            info=turn_info,
            expects_structured_output=schema is not None,
        )
        turn.approval_handler = approval_handler
        self._register_turn(turn)
        return turn

    async def steer(self, input: TurnInput) -> None:
        """Send follow-up input into the currently running turn."""
        turn = self._current_turn
        if turn is None or not turn.is_running:
            raise NoActiveTurnError(
                "no turn is running to steer; use run()/start_turn()"
            )
        await turn.steer(input)

    async def interrupt(self) -> None:
        """Interrupt the running turn (no-op when idle)."""
        turn = self._current_turn
        if turn is None or not turn.is_running:
            return
        self._transition(AgentState.INTERRUPTING, cause="interrupt requested")
        await self._client._interrupt_turn(self.id, turn.id)

    async def wait_for(
        self,
        state: AgentState | Callable[[AgentState], bool],
        *,
        timeout: float | None = None,
    ) -> AgentState:
        """Wait until the agent reaches a state (or a predicate matches)."""
        predicate: Callable[[AgentState], bool]
        if isinstance(state, AgentState):
            predicate = lambda current: current is state  # noqa: E731
        else:
            predicate = state
        thread_id = self.id
        with self._client._bus.stream(
            kinds=(AgentStateChanged,),
            predicate=lambda event: event.routing_thread_id == thread_id,
        ) as stream:
            if predicate(self._state):
                return self._state
            async with asyncio.timeout(timeout):
                async for event in stream:
                    assert isinstance(event, AgentStateChanged)
                    if predicate(event.new_state):
                        return event.new_state
        raise ProcessExitedError("client closed while waiting for state")

    # -- management -------------------------------------------------------------

    async def set_name(self, name: str) -> None:
        """Name the thread (for discoverability in ``thread/list``). Tolerant
        of older codex versions lacking the method."""
        try:
            await self._client.request(
                methods.THREAD_NAME_SET, {"threadId": self.id, "name": name}
            )
        except MethodNotSupportedError:
            logger.debug("thread/name/set unsupported by this codex version")
            return
        self._info.name = name

    async def set_goal(self, goal: str) -> None:
        await self._client.request(
            methods.THREAD_GOAL_SET, {"threadId": self.id, "goal": goal}
        )

    async def compact(self) -> None:
        """Compact the thread's context."""
        await self._client.request(methods.THREAD_COMPACT_START, {"threadId": self.id})

    async def rollback(self, *, num_turns: int) -> ThreadInfo:
        result = await self._client.request(
            methods.THREAD_ROLLBACK, {"threadId": self.id, "numTurns": num_turns}
        )
        return self._absorb_thread_payload(result)

    async def read(self, *, include_turns: bool = False) -> ThreadInfo:
        result = await self._client.request(
            methods.THREAD_READ, {"threadId": self.id, "includeTurns": include_turns}
        )
        return self._absorb_thread_payload(result)

    async def archive(self) -> None:
        await self._client.archive_thread(self.id)

    async def delete(self) -> None:
        await self._client.delete_thread(self.id)

    # -- events -------------------------------------------------------------------

    def events(self, **stream_options: Any) -> EventStream:
        """Stream of everything happening on this thread."""
        thread_id = self.id
        return self._client._bus.stream(
            predicate=lambda event: event.routing_thread_id == thread_id,
            **stream_options,
        )

    def subscribe(self, callback: Any, *, kinds: tuple[type, ...] | None = None) -> Subscription:
        thread_id = self.id
        return self._client._bus.subscribe(
            callback,
            kinds=kinds,
            predicate=lambda event: event.routing_thread_id == thread_id,
        )

    # -- internals ------------------------------------------------------------------

    async def _ensure_usable(self) -> None:
        if self._state is AgentState.CLOSED:
            if not self.auto_resume:
                raise ThreadClosedError(
                    f"thread {self.id} is closed (auto_resume disabled)"
                )
            await self._client._resume_in_place(self)

    def _register_turn(self, turn: Turn) -> None:
        self._turns[turn.id] = turn
        self._current_turn = turn
        # Drain notifications that arrived before the handle existed.
        buffered = [
            event
            for event in self._pending_turn_events
            if event.routing_turn_id == turn.id
        ]
        for event in buffered:
            self._pending_turn_events.remove(event)
            turn._apply(event)
        if not turn.is_running:
            # The whole turn played out before the handle registered.
            self._turns.pop(turn.id, None)
            if self._current_turn is turn:
                self._current_turn = None

    def _transition(self, new_state: AgentState, *, cause: str) -> None:
        if new_state is self._state:
            return
        old_state, self._state = self._state, new_state
        self._client._route(
            AgentStateChanged(
                thread_id=self.id,
                old_state=old_state,
                new_state=new_state,
                cause=cause,
            )
        )

    def _apply(self, event: Event) -> list[Event]:
        """Apply a routed event; returns synthesized follow-up events.

        Called by the client in arrival order, before bus fan-out.
        """
        synthesized: list[Event] = []

        match event:
            case TurnStarted(turn=turn_info):
                self._forward_turn_event(event, turn_info.id)
            case TurnCompleted(turn=turn_info):
                self._forward_turn_event(event, turn_info.id)
                if self._current_turn is not None and self._current_turn.id == turn_info.id:
                    self._current_turn = None
                self._turns.pop(turn_info.id, None)
            case ThreadTokenUsageUpdated(token_usage=usage):
                self._token_usage = usage
                self._forward_turn_event(event, event.routing_turn_id)
            case ItemCompleted(item=item):
                if isinstance(item, AgentMessageItem) and item.text:
                    self._last_agent_message = item.text
                self._forward_turn_event(event, event.routing_turn_id)
            case ThreadNameUpdated(name=name):
                self._info.name = name
            case ThreadClosed():
                for turn in self._turns.values():
                    turn._fail(ThreadClosedError(f"thread {self.id} was unloaded mid-turn"))
                self._turns.clear()
                self._current_turn = None
            case ProcessExited(message=message):
                for turn in self._turns.values():
                    turn._fail(ProcessExitedError(message))
                self._turns.clear()
                self._current_turn = None
            case _ if event.routing_turn_id is not None:
                self._forward_turn_event(event, event.routing_turn_id)

        new_state = next_state(self._state, event)
        if new_state is not self._state:
            old_state, self._state = self._state, new_state
            synthesized.append(
                AgentStateChanged(
                    thread_id=self.id,
                    old_state=old_state,
                    new_state=new_state,
                    cause=type(event).__name__,
                )
            )
        return synthesized

    def _forward_turn_event(self, event: Event, turn_id: str | None) -> None:
        if turn_id is None:
            return
        turn = self._turns.get(turn_id)
        if turn is not None:
            turn._apply(event)
        else:
            self._pending_turn_events.append(event)

    def _absorb_thread_payload(self, result: Any) -> ThreadInfo:
        payload = result.get("thread") if isinstance(result, dict) else None
        if payload is not None:
            self._info = ThreadInfo.model_validate(payload)
        return self._info
