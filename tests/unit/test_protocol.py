import json
from pathlib import Path

import pytest

from cuckoo.codex.protocol import (
    AgentMessageItem,
    CommandApproval,
    Decision,
    FileChangeApproval,
    ItemCompleted,
    MalformedEvent,
    ThreadStarted,
    ThreadStartResult,
    ThreadTokenUsageUpdated,
    TurnCompleted,
    TurnStartResult,
    TurnStatus,
    UnknownEvent,
    UnknownItem,
    parse_approval_request,
    parse_event,
    parse_item,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load_fixture_records() -> list[dict]:
    path = FIXTURES / "live_turn_events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestLiveFixtureReplay:
    """Replay a real captured session; nothing may come back malformed."""

    def test_no_malformed_events(self):
        for record in load_fixture_records():
            if record["method"].startswith("<response:"):
                continue
            event = parse_event(record["method"], record["params"])
            assert not isinstance(event, MalformedEvent), record["method"]

    def test_thread_started(self):
        record = next(
            r for r in load_fixture_records() if r["method"] == "thread/started"
        )
        event = parse_event(record["method"], record["params"])
        assert isinstance(event, ThreadStarted)
        assert event.thread.id
        assert event.routing_thread_id == event.thread.id
        assert event.thread.ephemeral is True

    def test_turn_completed_with_final_answer(self):
        records = load_fixture_records()
        completed = next(
            parse_event(r["method"], r["params"])
            for r in records
            if r["method"] == "turn/completed"
        )
        assert isinstance(completed, TurnCompleted)
        assert completed.turn.status == TurnStatus.COMPLETED
        assert completed.turn.error is None

        item_events = [
            parse_event(r["method"], r["params"])
            for r in records
            if r["method"] == "item/completed"
        ]
        messages = [
            e.item
            for e in item_events
            if isinstance(e, ItemCompleted) and isinstance(e.item, AgentMessageItem)
        ]
        assert any(m.is_final_answer and m.text.strip() == "OK" for m in messages)

    def test_token_usage(self):
        record = next(
            r
            for r in load_fixture_records()
            if r["method"] == "thread/tokenUsage/updated"
        )
        event = parse_event(record["method"], record["params"])
        assert isinstance(event, ThreadTokenUsageUpdated)
        assert event.token_usage.total.total_tokens > 0

    def test_unmodeled_methods_become_unknown_events(self):
        record = next(
            r for r in load_fixture_records() if r["method"] == "hook/started"
        )
        event = parse_event(record["method"], record["params"])
        assert isinstance(event, UnknownEvent)
        assert event.routing_thread_id == record["params"]["threadId"]

    def test_response_models(self):
        records = load_fixture_records()
        thread_response = next(
            r["params"] for r in records if r["method"] == "<response:thread/start>"
        )
        started = ThreadStartResult.model_validate(thread_response)
        assert started.thread.id
        turn_response = next(
            r["params"] for r in records if r["method"] == "<response:turn/start>"
        )
        turn = TurnStartResult.model_validate(turn_response)
        assert turn.turn.status == TurnStatus.IN_PROGRESS


class TestLenientParsing:
    def test_unknown_item_type(self):
        item = parse_item({"type": "holographicChart", "id": "x", "data": [1]})
        assert isinstance(item, UnknownItem)
        assert item.raw["data"] == [1]

    def test_unknown_enum_value_degrades_to_str(self):
        item = parse_item(
            {"type": "commandExecution", "id": "c1", "command": "ls",
             "status": "someFutureStatus"}
        )
        assert item.status == "someFutureStatus"

    def test_extra_fields_preserved(self):
        event = parse_event(
            "thread/closed", {"threadId": "t1", "brandNewField": {"a": 1}}
        )
        assert event.raw["brandNewField"] == {"a": 1}

    def test_malformed_known_event(self):
        event = parse_event("item/completed", {"item": "not-a-dict"})
        assert isinstance(event, MalformedEvent)
        assert event.method == "item/completed"

    def test_none_params(self):
        event = parse_event("account/rateLimits/updated", None)
        assert not isinstance(event, (UnknownEvent, MalformedEvent))


class TestApprovalRequests:
    def test_legacy_exec_command(self):
        request = parse_approval_request(
            "execCommandApproval",
            {
                "conversationId": "t1",
                "callId": "c1",
                "command": ["rm", "-rf", "build"],
                "cwd": "/repo",
                "reason": "cleanup",
            },
        )
        assert isinstance(request, CommandApproval)
        assert request.thread_id == "t1"
        assert request.command_text == "rm -rf build"
        assert request.is_legacy
        assert request.build_response(Decision.APPROVED) == {"decision": "approved"}
        assert request.build_response(Decision.ABORT) == {"decision": "abort"}

    def test_item_generation_command(self):
        request = parse_approval_request(
            "item/commandExecution/requestApproval",
            {
                "threadId": "t2",
                "turnId": "u1",
                "itemId": "i1",
                "command": "pip install requests",
                "cwd": "/repo",
                "startedAtMs": 1,
            },
        )
        assert isinstance(request, CommandApproval)
        assert (request.thread_id, request.turn_id, request.item_id) == ("t2", "u1", "i1")
        assert not request.is_legacy
        assert request.build_response(Decision.APPROVED) == {"decision": "accept"}
        assert request.build_response(Decision.DENIED) == {"decision": "decline"}
        assert request.build_response(Decision.APPROVED_FOR_SESSION) == {
            "decision": "acceptForSession"
        }

    def test_legacy_apply_patch(self):
        request = parse_approval_request(
            "applyPatchApproval",
            {
                "conversationId": "t1",
                "callId": "c2",
                "fileChanges": {"/repo/a.py": {"kind": "update"}},
                "grantRoot": "/repo",
            },
        )
        assert isinstance(request, FileChangeApproval)
        assert request.changes == {"/repo/a.py": {"kind": "update"}}
        assert request.grant_root == "/repo"
        assert request.build_response(Decision.DENIED) == {"decision": "denied"}

    def test_unmodeled_server_request(self):
        assert parse_approval_request("item/tool/requestUserInput", {}) is None

    @pytest.mark.parametrize("params", [None, {}, {"unexpected": True}])
    def test_degenerate_params_never_raise(self, params):
        request = parse_approval_request("execCommandApproval", params)
        assert isinstance(request, CommandApproval)
