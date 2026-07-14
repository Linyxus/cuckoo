"""Pydantic models for protocol data structures.

All models tolerate protocol drift: unknown fields are kept (``extra="allow"``),
wire names are camelCase via alias generation, and enum-valued fields fall
back to plain strings for values this library does not know yet.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .common import ApprovalPolicy, ReasoningEffort, SandboxMode, TurnStatus
from .items import ThreadItem, parse_item

LENIENT = ConfigDict(
    extra="allow",
    populate_by_name=True,
    alias_generator=to_camel,
    use_attribute_docstrings=True,
)


class ProtocolModel(BaseModel):
    model_config = LENIENT

    @property
    def raw(self) -> dict[str, Any]:
        """The full wire payload, including fields this model doesn't declare."""
        return self.model_dump(by_alias=True)


class TokenUsageBreakdown(ProtocolModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0


class TokenUsage(ProtocolModel):
    """Per-thread usage: ``last`` covers the most recent turn, ``total`` the thread."""

    total: TokenUsageBreakdown = Field(default_factory=TokenUsageBreakdown)
    last: TokenUsageBreakdown = Field(default_factory=TokenUsageBreakdown)
    model_context_window: int | None = None


class ThreadStatusInfo(ProtocolModel):
    """Runtime thread status: notLoaded | idle | active | systemError."""

    type: str = "idle"
    active_flags: list[Any] | None = None


class TurnError(ProtocolModel):
    message: str = ""
    additional_details: str | None = None
    codex_error_info: Any = None


class TurnInfo(ProtocolModel):
    """A turn as reported by the server (in responses and notifications)."""

    id: str
    status: TurnStatus | str = TurnStatus.IN_PROGRESS
    items: list[ThreadItem] = Field(default_factory=list)
    error: TurnError | None = None
    started_at: int | None = None
    completed_at: int | None = None
    duration_ms: int | None = None

    def model_post_init(self, _context: Any, /) -> None:
        # Item parsing is lenient by hand: unknown item types must not
        # invalidate the whole turn.
        self.items = [
            item if not isinstance(item, dict) else parse_item(item)
            for item in self.items
        ]


class ThreadInfo(ProtocolModel):
    """A thread as reported by the server. ``turns`` is only populated on
    resume/fork/rollback and ``thread/read`` with ``includeTurns=True``."""

    id: str
    name: str | None = None
    preview: str = ""
    cwd: str = ""
    status: ThreadStatusInfo = Field(default_factory=ThreadStatusInfo)
    turns: list[TurnInfo] = Field(default_factory=list)
    ephemeral: bool = False
    session_id: str | None = None
    parent_thread_id: str | None = None
    forked_from_id: str | None = None
    model_provider: str | None = None
    cli_version: str | None = None
    created_at: int | None = None
    updated_at: int | None = None


class ThreadPage(ProtocolModel):
    data: list[ThreadInfo] = Field(default_factory=list)
    next_cursor: str | None = None
    backwards_cursor: str | None = None


class ThreadStartResult(ProtocolModel):
    """Response of thread/start, thread/resume and thread/fork."""

    thread: ThreadInfo
    model: str | None = None
    model_provider: str | None = None
    cwd: str | None = None
    sandbox: Any = None
    approval_policy: ApprovalPolicy | str | dict[str, Any] | None = None
    reasoning_effort: ReasoningEffort | str | None = None


class TurnStartResult(ProtocolModel):
    turn: TurnInfo


class AuthStatus(ProtocolModel):
    auth_method: str | None = None
    auth_token: str | None = None
    requires_openai_auth: bool | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.auth_method is not None


class ModelInfo(ProtocolModel):
    id: str
    model: str | None = None
    display_name: str | None = None
    description: str | None = None
    is_default: bool = False
    hidden: bool = False
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[Any] = Field(default_factory=list)


class InitializeResult(ProtocolModel):
    user_agent: str | None = None
    codex_home: str | None = None
    platform_family: str | None = None
    platform_os: str | None = None


__all__ = [
    "ApprovalPolicy",
    "AuthStatus",
    "InitializeResult",
    "ModelInfo",
    "ProtocolModel",
    "ReasoningEffort",
    "SandboxMode",
    "ThreadInfo",
    "ThreadPage",
    "ThreadStartResult",
    "ThreadStatusInfo",
    "TokenUsage",
    "TokenUsageBreakdown",
    "TurnError",
    "TurnInfo",
    "TurnStartResult",
]
