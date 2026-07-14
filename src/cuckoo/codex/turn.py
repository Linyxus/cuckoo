"""Turn handle: stream a running turn, steer it, interrupt it, await it."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Generator, TypeVar

from pydantic import BaseModel, ConfigDict

from cuckoo.codex.errors import TurnFailedError
from cuckoo.codex.protocol.common import TurnStatus
from cuckoo.codex.protocol.events import (
    Event,
    ItemCompleted,
    ItemStarted,
    ServerError,
    ThreadTokenUsageUpdated,
    TurnCompleted,
)
from cuckoo.codex.protocol.items import (
    AgentMessageItem,
    CommandExecutionItem,
    FileChangeItem,
    ThreadItem,
)
from cuckoo.codex.protocol.models import TokenUsageBreakdown, TurnError, TurnInfo

if TYPE_CHECKING:
    from cuckoo.codex._events import EventStream
    from cuckoo.codex.client import CodexClient
    from cuckoo.codex.inputs import TurnInput

logger = logging.getLogger("cuckoo.codex.turn")

ModelT = TypeVar("ModelT", bound=BaseModel)


class TurnResult(BaseModel):
    """Snapshot of a finished (or interrupted/failed) turn."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    turn_id: str
    thread_id: str
    status: TurnStatus | str
    final_message: str | None = None
    structured_output: Any = None
    items: list[ThreadItem] = []
    token_usage: TokenUsageBreakdown | None = None
    error: TurnError | None = None

    @property
    def file_changes(self) -> list[FileChangeItem]:
        return [item for item in self.items if isinstance(item, FileChangeItem)]

    @property
    def command_executions(self) -> list[CommandExecutionItem]:
        return [item for item in self.items if isinstance(item, CommandExecutionItem)]

    @property
    def succeeded(self) -> bool:
        return self.status == TurnStatus.COMPLETED

    def require_final_message(self) -> str:
        if self.final_message is None:
            raise TurnFailedError(
                f"turn {self.turn_id} produced no final message "
                f"(status: {self.status})",
                result=self,
            )
        return self.final_message

    def output_as(self, model: type[ModelT]) -> ModelT:
        """Validate the structured output (or final message) as ``model``."""
        payload = self.structured_output
        if payload is None:
            payload = json.loads(self.require_final_message())
        return model.model_validate(payload)


class Turn:
    """A single agent turn. Await it (or call :meth:`result`) for the
    outcome; iterate :meth:`events` to stream its progress."""

    def __init__(
        self,
        client: CodexClient,
        *,
        thread_id: str,
        info: TurnInfo,
        expects_structured_output: bool = False,
    ) -> None:
        self._client = client
        self._info = info
        self._thread_id = thread_id
        self._expects_structured_output = expects_structured_output
        #: Optional per-turn approval handler (first in the scope chain).
        self.approval_handler: Any = None
        self._items: list[ThreadItem] = []
        self._item_index: dict[str, int] = {}
        self._status: TurnStatus | str = info.status
        self._error: TurnError | None = info.error
        self._final_message: str | None = None
        self._last_agent_text: str | None = None
        self._usage: TokenUsageBreakdown | None = None
        self._done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    # -- identity & snapshots ----------------------------------------------

    @property
    def id(self) -> str:
        return self._info.id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def status(self) -> TurnStatus | str:
        return self._status

    @property
    def is_running(self) -> bool:
        return not self._done.done()

    @property
    def items(self) -> list[ThreadItem]:
        return list(self._items)

    @property
    def final_message(self) -> str | None:
        return self._final_message if self._final_message is not None else self._last_agent_text

    # -- control -------------------------------------------------------------

    async def steer(self, input: TurnInput) -> None:
        """Append input to this turn while it is still running."""
        await self._client._steer_turn(self._thread_id, self.id, input)

    async def interrupt(self) -> None:
        await self._client._interrupt_turn(self._thread_id, self.id)

    def events(self, **stream_options: Any) -> EventStream:
        """Stream of this turn's events from this moment on; ends after the
        terminal ``TurnCompleted`` (or when the client shuts down).

        Subscribe to ``thread.events()`` *before* starting a turn to
        guarantee nothing is missed; accumulated items are always available
        via :attr:`items` and the :class:`TurnResult`.
        """
        turn_id = self.id
        stream = self._client._bus.stream(
            predicate=lambda event: event.routing_turn_id == turn_id,
            stop_after=lambda event: isinstance(event, TurnCompleted),
            **stream_options,
        )
        if self._done.done():
            stream.close()
        return stream

    async def result(self, *, timeout: float | None = None) -> TurnResult:
        """Wait for the turn to finish and return its result. Cancelling
        this wait does not affect the running turn."""
        async with asyncio.timeout(timeout):
            await asyncio.shield(self._done)
        return self._build_result()

    def __await__(self) -> Generator[Any, None, TurnResult]:
        return self.result().__await__()

    # -- event application (called by Thread, in arrival order) --------------

    def _apply(self, event: Event) -> None:
        match event:
            case ItemStarted(item=item) | ItemCompleted(item=item):
                self._upsert_item(item)
                if isinstance(event, ItemCompleted) and isinstance(
                    item, AgentMessageItem
                ):
                    self._last_agent_text = item.text
                    if item.is_final_answer:
                        self._final_message = item.text
            case ThreadTokenUsageUpdated(token_usage=usage):
                self._usage = usage.last
            case ServerError(error=error, will_retry=will_retry):
                if not will_retry:
                    self._error = error
            case TurnCompleted(turn=turn):
                self._finalize(turn)

    def _upsert_item(self, item: ThreadItem) -> None:
        key = item.id
        if key and key in self._item_index:
            self._items[self._item_index[key]] = item
        else:
            self._items.append(item)
            if key:
                self._item_index[key] = len(self._items) - 1

    def _finalize(self, turn: TurnInfo) -> None:
        self._info = turn
        self._status = turn.status
        if turn.error is not None:
            self._error = turn.error
        if turn.items and not self._items:
            # We may have missed item events (e.g. attached late); the
            # terminal payload is authoritative when it carries items.
            for item in turn.items:
                self._upsert_item(item)
        if not self._done.done():
            self._done.set_result(None)

    def _fail(self, error: BaseException) -> None:
        if not self._done.done():
            self._done.set_exception(error)

    def _build_result(self) -> TurnResult:
        structured_output = None
        final = self.final_message
        if self._expects_structured_output and final:
            try:
                structured_output = json.loads(final)
            except json.JSONDecodeError:
                logger.warning(
                    "turn %s: final message is not valid JSON despite output schema",
                    self.id,
                )
        return TurnResult(
            turn_id=self.id,
            thread_id=self._thread_id,
            status=self._status,
            final_message=final,
            structured_output=structured_output,
            items=self.items,
            token_usage=self._usage,
            error=self._error,
        )
