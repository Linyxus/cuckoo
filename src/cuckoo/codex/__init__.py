"""cuckoo.codex — programmatic interface for managing Codex subagents.

Quick start::

    from cuckoo.codex import CodexClient

    async with await CodexClient.connect() as client:
        agent = await client.start_thread(cwd="/path/to/repo")
        result = await agent.run("Summarize this repository.")
        print(result.final_message)
"""

from cuckoo.codex._events import EventStream, OverflowPolicy, Subscription
from cuckoo.codex.approvals import (
    APPROVE_ALL,
    DENY_ALL,
    ApprovalHandler,
    RuleBasedApprovals,
    chain,
)
from cuckoo.codex.client import CodexClient
from cuckoo.codex.config import ClientInfo, RetryPolicy, ShutdownPolicy
from cuckoo.codex.errors import (
    ClientClosedError,
    CodexNotFoundError,
    CodexVersionError,
    ConnectionClosedError,
    CuckooError,
    MethodNotSupportedError,
    NoActiveTurnError,
    ProcessExitedError,
    ProcessSpawnError,
    ProtocolDecodeError,
    RequestTimeoutError,
    RpcError,
    ServerOverloadedError,
    ThreadClosedError,
    TransportError,
    TurnFailedError,
)
from cuckoo.codex.inputs import Image, InputItem, LocalImage, Text, TurnInput
from cuckoo.codex.protocol import (
    AgentMessageDelta,
    AgentMessageItem,
    AgentState,
    AgentStateChanged,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalRequested,
    ApprovalResolved,
    AuthStatus,
    CommandApproval,
    CommandExecutionItem,
    Decision,
    Event,
    FileChangeApproval,
    FileChangeItem,
    ItemCompleted,
    ItemStarted,
    MalformedEvent,
    ModelInfo,
    ProcessExited,
    ReasoningEffort,
    ReasoningItem,
    SandboxMode,
    ServerError,
    ThreadClosed,
    ThreadInfo,
    ThreadItem,
    ThreadPage,
    ThreadStarted,
    ThreadTokenUsageUpdated,
    TokenUsage,
    TokenUsageBreakdown,
    TurnCompleted,
    TurnError,
    TurnStarted,
    TurnStatus,
    UnknownEvent,
    UnknownItem,
)
from cuckoo.codex.thread import Thread, ThreadOptions
from cuckoo.codex.turn import Turn, TurnResult

__all__ = [
    # client & handles
    "CodexClient",
    "Thread",
    "ThreadOptions",
    "Turn",
    "TurnResult",
    # events infrastructure
    "EventStream",
    "OverflowPolicy",
    "Subscription",
    # approvals
    "APPROVE_ALL",
    "DENY_ALL",
    "ApprovalHandler",
    "RuleBasedApprovals",
    "chain",
    # config
    "ClientInfo",
    "RetryPolicy",
    "ShutdownPolicy",
    # inputs
    "Image",
    "InputItem",
    "LocalImage",
    "Text",
    "TurnInput",
    # errors
    "ClientClosedError",
    "CodexNotFoundError",
    "CodexVersionError",
    "ConnectionClosedError",
    "CuckooError",
    "MethodNotSupportedError",
    "NoActiveTurnError",
    "ProcessExitedError",
    "ProcessSpawnError",
    "ProtocolDecodeError",
    "RequestTimeoutError",
    "RpcError",
    "ServerOverloadedError",
    "ThreadClosedError",
    "TransportError",
    "TurnFailedError",
    # protocol re-exports
    "AgentMessageDelta",
    "AgentMessageItem",
    "AgentState",
    "AgentStateChanged",
    "ApprovalPolicy",
    "ApprovalRequest",
    "ApprovalRequested",
    "ApprovalResolved",
    "AuthStatus",
    "CommandApproval",
    "CommandExecutionItem",
    "Decision",
    "Event",
    "FileChangeApproval",
    "FileChangeItem",
    "ItemCompleted",
    "ItemStarted",
    "MalformedEvent",
    "ModelInfo",
    "ProcessExited",
    "ReasoningEffort",
    "ReasoningItem",
    "SandboxMode",
    "ServerError",
    "ThreadClosed",
    "ThreadInfo",
    "ThreadItem",
    "ThreadPage",
    "ThreadStarted",
    "ThreadTokenUsageUpdated",
    "TokenUsage",
    "TokenUsageBreakdown",
    "TurnCompleted",
    "TurnError",
    "TurnStarted",
    "TurnStatus",
    "UnknownEvent",
    "UnknownItem",
]
