from cuckoo.codex.protocol.common import AgentState, Decision
from cuckoo.codex.protocol.events import (
    ApprovalRequested,
    ApprovalResolved,
    ProcessExited,
    ThreadClosed,
    ThreadStatusChanged,
    TurnCompleted,
    TurnStarted,
    UnknownEvent,
)
from cuckoo.codex.protocol.models import ThreadStatusInfo, TurnInfo
from cuckoo.codex.protocol.server_requests import CommandApproval
from cuckoo.codex.state import next_state


def turn_started(turn_id="u1"):
    return TurnStarted(thread_id="t1", turn=TurnInfo(id=turn_id))


def turn_completed(status="completed"):
    return TurnCompleted(thread_id="t1", turn=TurnInfo(id="u1", status=status))


def approval_requested():
    return ApprovalRequested(request=CommandApproval(thread_id="t1"))


def approval_resolved():
    return ApprovalResolved(
        request=CommandApproval(thread_id="t1"), decision=Decision.APPROVED
    )


def status_changed(status_type, **kw):
    return ThreadStatusChanged(
        thread_id="t1", status=ThreadStatusInfo(type=status_type, **kw)
    )


def test_happy_path_lifecycle():
    state = AgentState.IDLE
    state = next_state(state, turn_started())
    assert state is AgentState.RUNNING
    state = next_state(state, approval_requested())
    assert state is AgentState.AWAITING_APPROVAL
    state = next_state(state, approval_resolved())
    assert state is AgentState.RUNNING
    state = next_state(state, turn_completed())
    assert state is AgentState.IDLE


def test_turn_completed_resolves_any_state():
    for start in (
        AgentState.RUNNING,
        AgentState.AWAITING_APPROVAL,
        AgentState.INTERRUPTING,
    ):
        assert next_state(start, turn_completed("interrupted")) is AgentState.IDLE


def test_approval_events_ignored_outside_running_flow():
    assert next_state(AgentState.IDLE, approval_requested()) is AgentState.IDLE
    assert next_state(AgentState.RUNNING, approval_resolved()) is AgentState.RUNNING


def test_closure_events():
    assert next_state(AgentState.IDLE, ThreadClosed(thread_id="t1")) is AgentState.CLOSED
    assert next_state(AgentState.RUNNING, ProcessExited()) is AgentState.CLOSED


def test_server_status_is_ground_truth_but_coarse():
    assert next_state(AgentState.RUNNING, status_changed("idle")) is AgentState.IDLE
    assert (
        next_state(AgentState.AWAITING_APPROVAL, status_changed("idle"))
        is AgentState.AWAITING_APPROVAL
    )
    assert next_state(AgentState.IDLE, status_changed("notLoaded")) is AgentState.CLOSED
    assert (
        next_state(AgentState.IDLE, status_changed("active", active_flags=[]))
        is AgentState.RUNNING
    )
    assert (
        next_state(AgentState.INTERRUPTING, status_changed("active", active_flags=[]))
        is AgentState.INTERRUPTING
    )
    assert (
        next_state(AgentState.RUNNING, status_changed("someFutureStatus"))
        is AgentState.RUNNING
    )


def test_unrelated_events_keep_state():
    assert (
        next_state(AgentState.RUNNING, UnknownEvent(method="x")) is AgentState.RUNNING
    )
