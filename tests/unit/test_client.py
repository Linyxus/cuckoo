import asyncio

import pytest
from pydantic import BaseModel

from cuckoo.codex import (
    APPROVE_ALL,
    AgentState,
    AgentStateChanged,
    ApprovalRequested,
    ApprovalResolved,
    ClientClosedError,
    CodexClient,
    CommandExecutionItem,
    Decision,
    ItemCompleted,
    ProcessExited,
    ProcessExitedError,
    ThreadClosedError,
    TurnCompleted,
    TurnFailedError,
    TurnStatus,
)
from cuckoo.codex.client import build_config_flags
from cuckoo.codex.errors import ProcessExitedError as ProcessExitedError2  # noqa: F401
from tests.fake_server import (
    FakeAppServer,
    thread_start_result,
    turn_payload,
)


@pytest.fixture
async def rig():
    fake, client_transport = FakeAppServer.create()
    fake.with_handshake()
    fake.start()
    client = await CodexClient.connect(transport=client_transport, check_version=False)
    try:
        yield fake, client
    finally:
        await client.aclose()
        await fake.stop()


def wire_thread(fake: FakeAppServer, thread_id: str = "thread-1"):
    fake.on("thread/start", thread_start_result(thread_id))


def wire_auto_turn(fake: FakeAppServer, thread_id: str, turn_id: str = "turn-1", **play):
    """turn/start responds, then the turn plays out asynchronously."""
    tasks = []

    def handler(f: FakeAppServer, request):
        tasks.append(
            asyncio.get_running_loop().create_task(
                f.play_turn(thread_id, turn_id, **play)
            )
        )
        return {"turn": turn_payload(turn_id)}

    fake.on("turn/start", handler)
    return tasks


class TestConnect:
    async def test_handshake(self, rig):
        fake, client = rig
        assert client.server_info.user_agent == "codex/0.144.1-fake"
        initialize = await fake.wait_for_request("initialize")
        assert initialize.params["clientInfo"]["name"] == "cuckoo"
        assert initialize.params["capabilities"]["experimentalApi"] is False
        assert any(n.method == "initialized" for n in fake.notifications)
        assert client.is_running

    async def test_suppress_deltas_opt_out(self):
        fake, client_transport = FakeAppServer.create()
        fake.with_handshake()
        fake.start()
        client = await CodexClient.connect(
            transport=client_transport, check_version=False, suppress_deltas=True
        )
        try:
            initialize = await fake.wait_for_request("initialize")
            opt_outs = initialize.params["capabilities"]["optOutNotificationMethods"]
            assert "item/agentMessage/delta" in opt_outs
        finally:
            await client.aclose()
            await fake.stop()

    async def test_config_flags_rendering(self):
        flags = build_config_flags(
            {
                "model": "gpt-5.6",
                "features.multi_agent": True,
                "agents.max_threads": 12,
                "sandbox_workspace_write.writable_roots": ["/a", "/b"],
            }
        )
        assert flags == [
            "-c", 'model="gpt-5.6"',
            "-c", "features.multi_agent=true",
            "-c", "agents.max_threads=12",
            "-c", 'sandbox_workspace_write.writable_roots=["/a", "/b"]',
        ]


class TestThreadLifecycle:
    async def test_start_thread_defaults(self, rig):
        fake, client = rig
        wire_thread(fake)
        thread = await client.start_thread(cwd="/tmp/workdir")
        request = await fake.wait_for_request("thread/start")
        assert request.params["sandbox"] == "read-only"
        assert request.params["approvalPolicy"] == "never"
        assert request.params["serviceName"] == "cuckoo"
        assert "model" not in request.params  # None omitted -> config default
        assert thread.id == "thread-1"
        assert thread.state is AgentState.IDLE
        assert client.get_thread("thread-1") is thread

    async def test_run_turn_end_to_end(self, rig):
        fake, client = rig
        wire_thread(fake)
        wire_auto_turn(fake, "thread-1", final_text="All done.")
        thread = await client.start_thread(cwd="/tmp/workdir")

        states: list[AgentStateChanged] = []
        client.subscribe(states.append, kinds=(AgentStateChanged,))

        result = await thread.run("do the thing")
        assert result.status == TurnStatus.COMPLETED
        assert result.final_message == "All done."
        assert result.succeeded
        assert [type(i).__name__ for i in result.items] == [
            "CommandExecutionItem",
            "AgentMessageItem",
        ]
        assert result.command_executions[0].exit_code == 0
        assert result.token_usage is not None
        assert result.token_usage.total_tokens == 100
        assert thread.state is AgentState.IDLE
        assert thread.last_agent_message == "All done."
        assert thread.token_usage is not None
        assert [(s.old_state, s.new_state) for s in states] == [
            (AgentState.IDLE, AgentState.RUNNING),
            (AgentState.RUNNING, AgentState.IDLE),
        ]
        assert thread.current_turn is None

    async def test_turn_event_stream_ends_at_terminal(self, rig):
        fake, client = rig
        wire_thread(fake)
        go = asyncio.Event()
        tasks = []

        def gated_handler(f: FakeAppServer, request):
            async def play():
                await go.wait()
                await f.play_turn("thread-1", "turn-1")

            tasks.append(asyncio.get_running_loop().create_task(play()))
            return {"turn": turn_payload("turn-1")}

        fake.on("turn/start", gated_handler)
        thread = await client.start_thread(cwd="/tmp/workdir")
        turn = await thread.start_turn("stream me")
        stream = turn.events()  # subscribed before any event is emitted
        go.set()
        kinds = [type(event).__name__ async for event in stream]
        assert kinds[-1] == "TurnCompleted"
        assert "ItemCompleted" in kinds
        result = await turn
        assert result.final_message == "Done!"

    async def test_turn_event_stream_on_finished_turn_ends_immediately(self, rig):
        fake, client = rig
        wire_thread(fake)
        wire_auto_turn(fake, "thread-1")
        thread = await client.start_thread(cwd="/tmp/workdir")
        turn = await thread.start_turn("quick")
        await turn.result(timeout=5)
        collected = [event async for event in turn.events()]
        assert collected == []

    async def test_notifications_before_turn_response_are_not_lost(self, rig):
        fake, client = rig
        wire_thread(fake)

        async def eager_handler(f: FakeAppServer, request):
            # Entire turn plays out before the turn/start response is sent.
            await f.play_turn("thread-1", "turn-race", final_text="raced")
            return {"turn": turn_payload("turn-race")}

        fake.on("turn/start", eager_handler)
        thread = await client.start_thread(cwd="/tmp/workdir")
        result = await thread.run("race", timeout=5)
        assert result.final_message == "raced"
        assert result.status == TurnStatus.COMPLETED

    async def test_concurrent_threads_route_independently(self, rig):
        fake, client = rig
        counter = 0

        def start_handler(f, request):
            nonlocal counter
            counter += 1
            return thread_start_result(f"thread-{counter}")

        fake.on("thread/start", start_handler)

        def turn_handler(f: FakeAppServer, request):
            thread_id = request.params["threadId"]
            turn_id = f"turn-of-{thread_id}"
            asyncio.get_running_loop().create_task(
                f.play_turn(thread_id, turn_id, final_text=f"answer:{thread_id}")
            )
            return {"turn": turn_payload(turn_id)}

        fake.on("turn/start", turn_handler)

        thread_a = await client.start_thread(cwd="/tmp/workdir")
        thread_b = await client.start_thread(cwd="/tmp/workdir")
        result_a, result_b = await asyncio.gather(
            thread_a.run("a"), thread_b.run("b")
        )
        assert result_a.final_message == "answer:thread-1"
        assert result_b.final_message == "answer:thread-2"

    async def test_failed_turn_raises(self, rig):
        fake, client = rig
        wire_thread(fake)
        wire_auto_turn(
            fake,
            "thread-1",
            final_text=None,
            status="failed",
            error={"message": "model exploded"},
        )
        thread = await client.start_thread(cwd="/tmp/workdir")
        with pytest.raises(TurnFailedError) as exc_info:
            await thread.run("fail please")
        assert exc_info.value.result.status == TurnStatus.FAILED
        assert exc_info.value.result.error.message == "model exploded"

        wire_auto_turn(
            fake,
            "thread-1",
            turn_id="turn-2",
            final_text=None,
            status="failed",
            error={"message": "again"},
        )
        result = await thread.run("fail quietly", raise_on_failure=False)
        assert result.status == TurnStatus.FAILED

    async def test_interrupt(self, rig):
        fake, client = rig
        wire_thread(fake)

        started = asyncio.Event()

        def turn_handler(f: FakeAppServer, request):
            async def play():
                await f.notify(
                    "turn/started",
                    {"threadId": "thread-1", "turn": turn_payload("turn-1")},
                )
                started.set()

            asyncio.get_running_loop().create_task(play())
            return {"turn": turn_payload("turn-1")}

        def interrupt_handler(f: FakeAppServer, request):
            async def finish():
                await f.notify(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": turn_payload("turn-1", status="interrupted"),
                    },
                )

            asyncio.get_running_loop().create_task(finish())
            return {}

        fake.on("turn/start", turn_handler)
        fake.on("turn/interrupt", interrupt_handler)

        thread = await client.start_thread(cwd="/tmp/workdir")
        turn = await thread.start_turn("long job")
        await started.wait()
        assert thread.state is AgentState.RUNNING
        await thread.interrupt()
        result = await turn.result(timeout=5)
        assert result.status == TurnStatus.INTERRUPTED
        assert thread.state is AgentState.IDLE
        request = await fake.wait_for_request("turn/interrupt")
        assert request.params == {"threadId": "thread-1", "turnId": "turn-1"}

    async def test_steer_sends_expected_turn_id(self, rig):
        fake, client = rig
        wire_thread(fake)
        started = asyncio.Event()

        def turn_handler(f: FakeAppServer, request):
            async def play():
                await f.notify(
                    "turn/started",
                    {"threadId": "thread-1", "turn": turn_payload("turn-1")},
                )
                started.set()

            asyncio.get_running_loop().create_task(play())
            return {"turn": turn_payload("turn-1")}

        fake.on("turn/start", turn_handler)
        fake.on("turn/steer", {})
        thread = await client.start_thread(cwd="/tmp/workdir")
        await thread.start_turn("job")
        await started.wait()
        await thread.steer("also do this")
        steer = await fake.wait_for_request("turn/steer")
        assert steer.params["expectedTurnId"] == "turn-1"
        assert steer.params["input"] == [{"type": "text", "text": "also do this"}]

    async def test_structured_output(self, rig):
        fake, client = rig
        wire_thread(fake)

        class Verdict(BaseModel):
            ok: bool
            score: int

        wire_auto_turn(fake, "thread-1", final_text='{"ok": true, "score": 9}')
        thread = await client.start_thread(cwd="/tmp/workdir")
        result = await thread.run("judge", output_schema=Verdict)
        start = await fake.wait_for_request("turn/start")
        assert start.params["outputSchema"]["properties"].keys() == {"ok", "score"}
        assert result.structured_output == {"ok": True, "score": 9}
        verdict = result.output_as(Verdict)
        assert verdict.ok is True and verdict.score == 9

    async def test_thread_closed_and_auto_resume(self, rig):
        fake, client = rig
        wire_thread(fake)
        fake.on("thread/resume", thread_start_result("thread-1"))
        wire_auto_turn(fake, "thread-1", final_text="back again")
        thread = await client.start_thread(cwd="/tmp/workdir")

        await fake.notify("thread/closed", {"threadId": "thread-1"})
        await thread.wait_for(AgentState.CLOSED, timeout=5)

        result = await thread.run("wake up")
        assert result.final_message == "back again"
        resume = await fake.wait_for_request("thread/resume")
        assert resume.params["threadId"] == "thread-1"
        assert resume.params["cwd"] == "/tmp/workdir"

    async def test_thread_closed_without_auto_resume(self, rig):
        fake, client = rig
        wire_thread(fake)
        thread = await client.start_thread(cwd="/tmp/workdir", auto_resume=False)
        await fake.notify("thread/closed", {"threadId": "thread-1"})
        await thread.wait_for(AgentState.CLOSED, timeout=5)
        with pytest.raises(ThreadClosedError):
            await thread.run("hello?")

    async def test_thread_closed_fails_inflight_turn(self, rig):
        fake, client = rig
        wire_thread(fake)
        fake.on("turn/start", lambda f, r: {"turn": turn_payload("turn-1")})
        thread = await client.start_thread(cwd="/tmp/workdir")
        turn = await thread.start_turn("job")
        await fake.notify("thread/closed", {"threadId": "thread-1"})
        with pytest.raises(ThreadClosedError):
            await turn.result(timeout=5)


class TestProcessDeath:
    async def test_death_mid_turn(self):
        fake, client_transport = FakeAppServer.create()
        fake.with_handshake()
        fake.start()
        client_transport.set_exit_error(
            ProcessExitedError("app-server crashed", exit_code=1, stderr_tail="boom")
        )
        client = await CodexClient.connect(
            transport=client_transport, check_version=False
        )
        wire_thread(fake)
        fake.on("turn/start", lambda f, r: {"turn": turn_payload("turn-1")})

        exits: list[ProcessExited] = []
        client.subscribe(exits.append, kinds=(ProcessExited,))
        monitor = client.events()

        thread = await client.start_thread(cwd="/tmp/workdir")
        turn = await thread.start_turn("doomed")
        await fake.transport.aclose()

        with pytest.raises(ProcessExitedError):
            await turn.result(timeout=5)
        assert thread.state is AgentState.CLOSED
        assert not client.is_running
        # the monitor stream terminates after the death event
        collected = [event async for event in monitor]
        assert any(isinstance(event, ProcessExited) for event in collected)
        assert exits and exits[0].exit_code == 1
        with pytest.raises(ClientClosedError):
            await client.request("model/list")
        await client.aclose()
        await fake.stop()


class TestApprovals:
    async def wire_approval_turn(self, fake, method, params, decisions):
        """Turn whose progress requires an approval round trip."""

        def turn_handler(f: FakeAppServer, request):
            async def play():
                await f.notify(
                    "turn/started",
                    {"threadId": "thread-1", "turn": turn_payload("turn-1")},
                )
                reply = await f.request(method, params)
                decisions.append(getattr(reply, "result", reply))
                await f.notify(
                    "turn/completed",
                    {"threadId": "thread-1", "turn": turn_payload("turn-1", status="completed")},
                )

            asyncio.get_running_loop().create_task(play())
            return {"turn": turn_payload("turn-1")}

        fake.on("turn/start", turn_handler)

    async def test_client_level_approve_all_legacy_method(self, rig):
        fake, client = rig
        client.approval_handler = APPROVE_ALL
        wire_thread(fake)
        decisions = []
        await self.wire_approval_turn(
            fake,
            "execCommandApproval",
            {
                "conversationId": "thread-1",
                "callId": "c1",
                "command": ["ls"],
                "cwd": "/tmp/workdir",
                "parsedCmd": [],
            },
            decisions,
        )
        thread = await client.start_thread(cwd="/tmp/workdir")

        events = []
        client.subscribe(
            events.append, kinds=(ApprovalRequested, ApprovalResolved, AgentStateChanged)
        )
        await thread.run("needs approval")
        assert decisions == [{"decision": "approved"}]

        requested = [e for e in events if isinstance(e, ApprovalRequested)]
        resolved = [e for e in events if isinstance(e, ApprovalResolved)]
        assert len(requested) == 1
        assert requested[0].request.command_text == "ls"
        assert resolved[0].decision is Decision.APPROVED
        assert resolved[0].source == "client"
        transitions = [
            (e.old_state, e.new_state) for e in events if isinstance(e, AgentStateChanged)
        ]
        assert (AgentState.RUNNING, AgentState.AWAITING_APPROVAL) in transitions
        assert (AgentState.AWAITING_APPROVAL, AgentState.RUNNING) in transitions

    async def test_thread_handler_item_generation_denial(self, rig):
        fake, client = rig
        client.approval_handler = APPROVE_ALL  # thread handler should win
        wire_thread(fake)
        decisions = []
        await self.wire_approval_turn(
            fake,
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "command": "rm -rf /",
                "startedAtMs": 1,
            },
            decisions,
        )
        thread = await client.start_thread(
            cwd="/tmp/workdir", approval_handler=lambda r: Decision.DENIED
        )
        await thread.run("dangerous")
        assert decisions == [{"decision": "decline"}]

    async def test_turn_handler_outranks_thread_and_client(self, rig):
        fake, client = rig
        client.approval_handler = lambda r: Decision.DENIED
        wire_thread(fake)
        decisions = []
        await self.wire_approval_turn(
            fake,
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "turnId": "turn-1", "itemId": "i", "command": "make", "startedAtMs": 1},
            decisions,
        )
        thread = await client.start_thread(
            cwd="/tmp/workdir", approval_handler=lambda r: Decision.DENIED
        )
        result = await thread.run(
            "build it", approval_handler=lambda r: Decision.APPROVED_FOR_SESSION
        )
        assert result.status == TurnStatus.COMPLETED
        assert decisions == [{"decision": "acceptForSession"}]

    async def test_no_handler_denies_by_default(self, rig):
        fake, client = rig
        wire_thread(fake)
        decisions = []
        await self.wire_approval_turn(
            fake,
            "execCommandApproval",
            {"conversationId": "thread-1", "callId": "c", "command": ["x"], "cwd": "/", "parsedCmd": []},
            decisions,
        )
        thread = await client.start_thread(cwd="/tmp/workdir")
        await thread.run("whatever")
        assert decisions == [{"decision": "denied"}]

    async def test_unmodeled_server_request_gets_32601(self, rig):
        fake, client = rig
        reply = await fake.request("item/tool/requestUserInput", {"threadId": "x"})
        assert reply.code == -32601


class TestEscapeHatches:
    async def test_raw_request(self, rig):
        fake, client = rig
        fake.on("fuzzyFileSearch", {"results": [1, 2, 3]})
        result = await client.request("fuzzyFileSearch", {"query": "x"})
        assert result == {"results": [1, 2, 3]}

    async def test_close_is_idempotent(self, rig):
        fake, client = rig
        await client.aclose()
        await client.aclose()
        with pytest.raises(ClientClosedError):
            await client.request("model/list")
