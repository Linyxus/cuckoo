"""Pure agent-state transition function.

The client applies every event routed to a thread through
:func:`next_state`; the imperative exceptions (``interrupt()`` setting
INTERRUPTING) are documented on :class:`cuckoo.codex.thread.Thread`.
"""

from __future__ import annotations

from cuckoo.codex.protocol.common import AgentState
from cuckoo.codex.protocol.events import (
    ApprovalRequested,
    ApprovalResolved,
    Event,
    ProcessExited,
    ThreadClosed,
    ThreadStatusChanged,
    TurnCompleted,
    TurnStarted,
)

_TERMINAL = (AgentState.CLOSED,)


def next_state(state: AgentState, event: Event) -> AgentState:
    """Compute the follow-up state for a thread-routed event."""
    match event:
        case TurnStarted():
            return AgentState.RUNNING
        case TurnCompleted():
            return AgentState.IDLE
        case ApprovalRequested():
            return (
                AgentState.AWAITING_APPROVAL
                if state is AgentState.RUNNING
                else state
            )
        case ApprovalResolved():
            return (
                AgentState.RUNNING if state is AgentState.AWAITING_APPROVAL else state
            )
        case ThreadClosed() | ProcessExited():
            return AgentState.CLOSED
        case ThreadStatusChanged(status=status):
            # Server-reported status is ground truth, but it is coarser than
            # our lifecycle: don't let "active" clobber approval/interrupt
            # sub-states.
            match status.type:
                case "idle":
                    return (
                        AgentState.IDLE
                        if state not in (AgentState.AWAITING_APPROVAL,)
                        else state
                    )
                case "notLoaded":
                    return AgentState.CLOSED
                case "active":
                    return (
                        AgentState.RUNNING
                        if state in (AgentState.IDLE, AgentState.CLOSED)
                        else state
                    )
                case _:
                    return state
    return state
