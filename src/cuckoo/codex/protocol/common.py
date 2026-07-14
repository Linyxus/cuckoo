"""Enums and shared scalar types from the app-server protocol.

Wire values are verified against the ``codex app-server generate-json-schema``
snapshot in ``docs/protocol/`` (codex-cli 0.144.1). Fields that carry these
enums are typed ``Enum | str`` in models so an unrecognized future value
degrades to a plain string instead of failing validation.
"""

from __future__ import annotations

import enum


class SandboxMode(enum.StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalPolicy(enum.StrEnum):
    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"


class ReasoningEffort(enum.StrEnum):
    """Known reasoning-effort values; the wire type is an open string."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class TurnStatus(enum.StrEnum):
    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ItemStatus(enum.StrEnum):
    """Execution status shared by command/fileChange/mcpToolCall items."""

    IN_PROGRESS = "inProgress"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"


class MessagePhase(enum.StrEnum):
    """Phase of an agentMessage item; absent means "phase unknown"."""

    COMMENTARY = "commentary"
    FINAL_ANSWER = "final_answer"


class AgentState(enum.StrEnum):
    """Lifecycle state of a managed subagent (thread), maintained client-side."""

    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    INTERRUPTING = "interrupting"
    CLOSED = "closed"


class Decision(enum.Enum):
    """Normalized approve/deny decision across both approval generations.

    Serialized per-request: the legacy ``execCommandApproval`` generation
    uses approved/denied/abort wording, the ``item/*/requestApproval``
    generation uses accept/decline/cancel.
    """

    APPROVED = "approved"
    APPROVED_FOR_SESSION = "approved_for_session"
    DENIED = "denied"
    ABORT = "abort"
