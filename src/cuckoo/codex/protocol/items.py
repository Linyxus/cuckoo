"""ThreadItem union — the pieces of work streamed inside a turn."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel

from .common import ItemStatus, MessagePhase

logger = logging.getLogger("cuckoo.codex.protocol")

_LENIENT = ConfigDict(extra="allow", populate_by_name=True, alias_generator=to_camel)


class _ItemBase(BaseModel):
    model_config = _LENIENT

    id: str = ""

    @property
    def raw(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class AgentMessageItem(_ItemBase):
    type: Literal["agentMessage"] = "agentMessage"
    text: str = ""
    phase: MessagePhase | str | None = None

    @property
    def is_final_answer(self) -> bool:
        return self.phase == MessagePhase.FINAL_ANSWER


class ReasoningItem(_ItemBase):
    type: Literal["reasoning"] = "reasoning"
    summary: list[str] = Field(default_factory=list)
    content: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.content or self.summary)


class PlanItem(_ItemBase):
    type: Literal["plan"] = "plan"
    text: str = ""


class CommandExecutionItem(_ItemBase):
    type: Literal["commandExecution"] = "commandExecution"
    command: str = ""
    cwd: str | None = None
    status: ItemStatus | str = ItemStatus.IN_PROGRESS
    aggregated_output: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None


class FileUpdateChange(BaseModel):
    model_config = _LENIENT

    path: str = ""
    kind: str = "update"  # add | delete | update (wire: PatchChangeKind)
    diff: str | None = None


class FileChangeItem(_ItemBase):
    type: Literal["fileChange"] = "fileChange"
    changes: list[FileUpdateChange] = Field(default_factory=list)
    status: ItemStatus | str = ItemStatus.IN_PROGRESS


class McpToolCallItem(_ItemBase):
    type: Literal["mcpToolCall"] = "mcpToolCall"
    server: str = ""
    tool: str = ""
    arguments: Any = None
    result: Any = None
    error: Any = None
    status: ItemStatus | str = ItemStatus.IN_PROGRESS
    duration_ms: int | None = None


class WebSearchItem(_ItemBase):
    type: Literal["webSearch"] = "webSearch"
    query: str = ""


class UserMessageItem(_ItemBase):
    type: Literal["userMessage"] = "userMessage"
    content: list[Any] = Field(default_factory=list)


class CollabAgentToolCallItem(_ItemBase):
    """A Codex-internal multi-agent (collab) tool call; tracks subagent turns."""

    type: Literal["collabAgentToolCall"] = "collabAgentToolCall"
    tool: str = ""
    status: str = "inProgress"
    sender_thread_id: str | None = None
    receiver_thread_ids: list[str] = Field(default_factory=list)
    prompt: str | None = None
    model: str | None = None
    agents_states: dict[str, Any] = Field(default_factory=dict)


class EnteredReviewModeItem(_ItemBase):
    type: Literal["enteredReviewMode"] = "enteredReviewMode"


class ExitedReviewModeItem(_ItemBase):
    type: Literal["exitedReviewMode"] = "exitedReviewMode"


class ContextCompactionItem(_ItemBase):
    type: Literal["contextCompaction"] = "contextCompaction"


class UnknownItem(_ItemBase):
    """Fallback for item types this library does not model (yet)."""

    type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def raw(self) -> dict[str, Any]:
        return self.payload


KnownItem = Union[
    AgentMessageItem,
    ReasoningItem,
    PlanItem,
    CommandExecutionItem,
    FileChangeItem,
    McpToolCallItem,
    WebSearchItem,
    UserMessageItem,
    CollabAgentToolCallItem,
    EnteredReviewModeItem,
    ExitedReviewModeItem,
    ContextCompactionItem,
]

ThreadItem = Union[KnownItem, UnknownItem]

_KNOWN_ITEM_ADAPTER: TypeAdapter[KnownItem] = TypeAdapter(
    Annotated[KnownItem, Field(discriminator="type")]
)


def parse_item(data: dict[str, Any]) -> ThreadItem:
    """Parse an item payload; degrade to :class:`UnknownItem`, never raise."""
    try:
        return _KNOWN_ITEM_ADAPTER.validate_python(data)
    except ValidationError as exc:
        item_type = str(data.get("type", ""))
        logger.debug("unmodeled %r item: %s", item_type, exc)
        return UnknownItem(
            id=str(data.get("id", "")), type=item_type, payload=data
        )
