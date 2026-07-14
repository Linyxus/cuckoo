"""Typed event vocabulary: wire notifications plus synthesized events.

Wire events are parsed through a method-name registry; anything unknown
degrades to :class:`UnknownEvent` and a known method whose payload fails
validation degrades to :class:`MalformedEvent` — parsing never raises.

Synthesized events (no ``METHOD``) are emitted by the client itself:
state transitions, approval lifecycle, process death. Together these are
everything a monitor needs to observe a fleet of subagents.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel

from .common import AgentState, Decision
from .items import ThreadItem, parse_item
from .models import ThreadInfo, ThreadStatusInfo, TokenUsage, TurnError, TurnInfo
from .server_requests import ApprovalRequest

logger = logging.getLogger("cuckoo.codex.protocol")


class Event(BaseModel):
    """Base event. ``METHOD`` is the wire notification name; ``None`` for
    events synthesized by this library."""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, alias_generator=to_camel
    )

    METHOD: ClassVar[str | None] = None

    @property
    def routing_thread_id(self) -> str | None:
        thread_id = getattr(self, "thread_id", None)
        return thread_id if isinstance(thread_id, str) else None

    @property
    def routing_turn_id(self) -> str | None:
        turn_id = getattr(self, "turn_id", None)
        return turn_id if isinstance(turn_id, str) else None

    @property
    def raw(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


# --- thread lifecycle -------------------------------------------------------


class ThreadStarted(Event):
    METHOD = "thread/started"
    thread: ThreadInfo

    @property
    def routing_thread_id(self) -> str | None:
        return self.thread.id


class ThreadStatusChanged(Event):
    METHOD = "thread/status/changed"
    thread_id: str
    status: ThreadStatusInfo = Field(default_factory=ThreadStatusInfo)


class ThreadNameUpdated(Event):
    METHOD = "thread/name/updated"
    thread_id: str
    name: str | None = None


class ThreadClosed(Event):
    """The server unloaded the thread (e.g. ~30 min idle auto-unload)."""

    METHOD = "thread/closed"
    thread_id: str


class ThreadArchived(Event):
    METHOD = "thread/archived"
    thread_id: str


class ThreadUnarchived(Event):
    METHOD = "thread/unarchived"
    thread_id: str


class ThreadDeleted(Event):
    METHOD = "thread/deleted"
    thread_id: str


class ThreadCompacted(Event):
    METHOD = "thread/compacted"
    thread_id: str | None = None


class ThreadGoalUpdated(Event):
    METHOD = "thread/goal/updated"
    thread_id: str | None = None
    goal: Any = None


class ThreadTokenUsageUpdated(Event):
    METHOD = "thread/tokenUsage/updated"
    thread_id: str
    turn_id: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


# --- turn lifecycle ---------------------------------------------------------


class TurnStarted(Event):
    METHOD = "turn/started"
    thread_id: str
    turn: TurnInfo

    @property
    def routing_turn_id(self) -> str | None:
        return self.turn.id


class TurnCompleted(Event):
    """Terminal turn event; ``turn.status`` is completed/interrupted/failed."""

    METHOD = "turn/completed"
    thread_id: str
    turn: TurnInfo

    @property
    def routing_turn_id(self) -> str | None:
        return self.turn.id


class TurnDiffUpdated(Event):
    METHOD = "turn/diff/updated"
    thread_id: str | None = None
    turn_id: str | None = None
    diff: Any = None


class TurnPlanUpdated(Event):
    METHOD = "turn/plan/updated"
    thread_id: str | None = None
    turn_id: str | None = None
    plan: Any = None


# --- items and deltas -------------------------------------------------------


class _ItemEvent(Event):
    thread_id: str
    turn_id: str
    item: ThreadItem

    @field_validator("item", mode="before")
    @classmethod
    def _parse_item(cls, value: Any) -> Any:
        return parse_item(value) if isinstance(value, dict) else value


class ItemStarted(_ItemEvent):
    METHOD = "item/started"
    started_at_ms: int | None = None


class ItemCompleted(_ItemEvent):
    METHOD = "item/completed"
    completed_at_ms: int | None = None


class _DeltaEvent(Event):
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    delta: Any = None


class AgentMessageDelta(_DeltaEvent):
    METHOD = "item/agentMessage/delta"
    delta: str = ""


class ReasoningTextDelta(_DeltaEvent):
    METHOD = "item/reasoning/textDelta"


class ReasoningSummaryTextDelta(_DeltaEvent):
    METHOD = "item/reasoning/summaryTextDelta"


class ReasoningSummaryPartAdded(_DeltaEvent):
    METHOD = "item/reasoning/summaryPartAdded"


class PlanDelta(_DeltaEvent):
    METHOD = "item/plan/delta"


class CommandExecutionOutputDelta(_DeltaEvent):
    METHOD = "item/commandExecution/outputDelta"


class FileChangeOutputDelta(_DeltaEvent):
    METHOD = "item/fileChange/outputDelta"


class FileChangePatchUpdated(_DeltaEvent):
    METHOD = "item/fileChange/patchUpdated"


class McpToolCallProgress(_DeltaEvent):
    METHOD = "item/mcpToolCall/progress"


# --- system -----------------------------------------------------------------


class ServerError(Event):
    """A (possibly retryable) error surfaced by the server for a turn."""

    METHOD = "error"
    error: TurnError = Field(default_factory=TurnError)
    thread_id: str | None = None
    turn_id: str | None = None
    will_retry: bool = False


class ModelRerouted(Event):
    METHOD = "model/rerouted"
    thread_id: str | None = None


class AccountRateLimitsUpdated(Event):
    METHOD = "account/rateLimits/updated"


class McpServerStartupStatusUpdated(Event):
    METHOD = "mcpServer/startupStatus/updated"


class ServerRequestResolved(Event):
    METHOD = "serverRequest/resolved"
    thread_id: str | None = None


# --- fallbacks --------------------------------------------------------------


class UnknownEvent(Event):
    """A notification method this library does not model."""

    method: str = ""
    params: Any = None

    @property
    def routing_thread_id(self) -> str | None:
        if isinstance(self.params, dict):
            thread_id = self.params.get("threadId")
            return thread_id if isinstance(thread_id, str) else None
        return None

    @property
    def routing_turn_id(self) -> str | None:
        if isinstance(self.params, dict):
            turn_id = self.params.get("turnId")
            return turn_id if isinstance(turn_id, str) else None
        return None


class MalformedEvent(Event):
    """A known method whose payload failed validation (protocol drift)."""

    method: str = ""
    params: Any = None
    validation_error: str = ""

    routing_thread_id = UnknownEvent.routing_thread_id
    routing_turn_id = UnknownEvent.routing_turn_id


# --- synthesized by the client ----------------------------------------------


class AgentStateChanged(Event):
    """A thread's client-side lifecycle state machine transitioned."""

    thread_id: str
    old_state: AgentState
    new_state: AgentState
    cause: str = ""


class ApprovalRequested(Event):
    """The server asked for an approval decision; resolution pending."""

    request: ApprovalRequest

    @property
    def routing_thread_id(self) -> str | None:
        return self.request.thread_id

    @property
    def routing_turn_id(self) -> str | None:
        return self.request.turn_id


class ApprovalResolved(Event):
    """An approval request was answered (by a handler, timeout, or default)."""

    request: ApprovalRequest
    decision: Decision
    source: str = ""  # e.g. "turn", "thread", "client", "timeout", "default"

    routing_thread_id = ApprovalRequested.routing_thread_id
    routing_turn_id = ApprovalRequested.routing_turn_id


class ProcessExited(Event):
    """The codex app-server process died; all threads on it are dead."""

    message: str = ""
    exit_code: int | None = None
    signal: int | None = None
    stderr_tail: str = ""


# --- registry ---------------------------------------------------------------

EVENT_REGISTRY: dict[str, type[Event]] = {
    cls.METHOD: cls
    for cls in [
        ThreadStarted,
        ThreadStatusChanged,
        ThreadNameUpdated,
        ThreadClosed,
        ThreadArchived,
        ThreadUnarchived,
        ThreadDeleted,
        ThreadCompacted,
        ThreadGoalUpdated,
        ThreadTokenUsageUpdated,
        TurnStarted,
        TurnCompleted,
        TurnDiffUpdated,
        TurnPlanUpdated,
        ItemStarted,
        ItemCompleted,
        AgentMessageDelta,
        ReasoningTextDelta,
        ReasoningSummaryTextDelta,
        ReasoningSummaryPartAdded,
        PlanDelta,
        CommandExecutionOutputDelta,
        FileChangeOutputDelta,
        FileChangePatchUpdated,
        McpToolCallProgress,
        ServerError,
        ModelRerouted,
        AccountRateLimitsUpdated,
        McpServerStartupStatusUpdated,
        ServerRequestResolved,
    ]
    if cls.METHOD is not None
}


def parse_event(method: str, params: Any) -> Event:
    """Parse a wire notification into a typed event; never raises."""
    event_type = EVENT_REGISTRY.get(method)
    if event_type is None:
        return UnknownEvent(method=method, params=params)
    try:
        return event_type.model_validate(params or {})
    except ValidationError as exc:
        logger.warning("malformed %r notification: %s", method, exc)
        return MalformedEvent(method=method, params=params, validation_error=str(exc))
