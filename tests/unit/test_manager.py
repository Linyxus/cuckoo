"""AgentManager tests over the fake app-server."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from cuckoo.codex import CodexClient, CuckooError
from cuckoo.journal import Journal
from cuckoo.manager import AgentManager
from tests.fake_server import FakeAppServer, thread_start_result, turn_payload


@pytest.fixture
async def rig(tmp_path):
    fake, transport = FakeAppServer.create()
    fake.with_handshake()
    fake.start()

    async def factory() -> CodexClient:
        return await CodexClient.connect(transport=transport, check_version=False)

    socket_dir = tempfile.mkdtemp(dir="/tmp", prefix="cuckoo-mgr-")
    manager = AgentManager(
        state_dir=tmp_path,
        socket_path=Path(socket_dir) / "bridge.sock",
        client_factory=factory,
        default_cwd=tmp_path,
    )
    try:
        yield fake, manager
    finally:
        await manager.aclose()
        await fake.stop()


def wire_thread(fake, thread_id="thread-1"):
    fake.on("thread/start", thread_start_result(thread_id))


def wire_auto_turn(fake, thread_id="thread-1", turn_id="turn-1", **play):
    tasks = []

    def handler(f, request):
        tasks.append(
            asyncio.get_running_loop().create_task(f.play_turn(thread_id, turn_id, **play))
        )
        return {"turn": turn_payload(turn_id)}

    fake.on("turn/start", handler)
    return tasks


async def relay(socket_path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    reply = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
    writer.close()
    return reply


async def settle(manager, name: str) -> None:
    """Wait for the agent's current turn task to finish (test helper)."""
    agent = manager.get(name)
    if agent is not None and agent.current_task is not None:
        await asyncio.wait_for(asyncio.shield(agent.current_task), timeout=5)
    await asyncio.sleep(0)


def items_of(manager, kind: str, agent: str | None = None) -> list[dict]:
    return [
        item
        for item in manager.recent_activity(limit=50)
        if item["kind"] == kind and (agent is None or item["agent"] == agent)
    ]


class TestSpawnAndCompletion:
    async def test_spawn_runs_task_and_posts_completion(self, rig):
        fake, manager = rig
        wire_thread(fake)
        wire_auto_turn(fake, final_text="refactor done")

        agent = await manager.spawn("refactor the parser", name="worker")
        assert agent.name == "worker"
        await settle(manager, "worker")

        completed = items_of(manager, "completed", "worker")
        assert len(completed) == 1
        assert completed[0]["text"] == "refactor done"
        assert completed[0]["data"]["status"] == "completed"
        assert completed[0]["data"]["tokens"] == 100

        # developer briefing was injected
        start = await fake.wait_for_request("thread/start")
        assert "cuckoo bridge" in start.params["developerInstructions"]
        assert start.params["sandbox"] == "workspace-write"

    async def test_auto_names_are_unique(self, rig):
        fake, manager = rig
        counter = 0

        def start_handler(f, request):
            nonlocal counter
            counter += 1
            return thread_start_result(f"thread-{counter}")

        fake.on("thread/start", start_handler)
        fake.on("turn/start", lambda f, r: {"turn": turn_payload(f"turn-{counter}")})
        first = await manager.spawn("a")
        second = await manager.spawn("b")
        assert first.name != second.name
        assert manager.get(first.name) is first

    async def test_spawn_model_effort_flow_through(self, rig):
        fake, manager = rig
        wire_thread(fake)
        wire_auto_turn(fake)
        await manager.spawn(
            "task", model="gpt-5.6-sol", effort="xhigh", sandbox="read-only"
        )
        start = await fake.wait_for_request("thread/start")
        assert start.params["model"] == "gpt-5.6-sol"
        assert start.params["sandbox"] == "read-only"
        turn = await fake.wait_for_request("turn/start")
        assert turn.params["effort"] == "xhigh"


class TestSend:
    async def test_send_steers_running_turn(self, rig):
        fake, manager = rig
        wire_thread(fake)
        started = asyncio.Event()

        def turn_handler(f, request):
            async def play():
                await f.notify(
                    "turn/started", {"threadId": "thread-1", "turn": turn_payload("turn-1")}
                )
                started.set()

            asyncio.get_running_loop().create_task(play())
            return {"turn": turn_payload("turn-1")}

        fake.on("turn/start", turn_handler)
        fake.on("turn/steer", {})
        agent = await manager.spawn("long job")
        await started.wait()
        how = await manager.send(agent.name, "focus on module X")
        assert how == "steered"
        steer = await fake.wait_for_request("turn/steer")
        assert steer.params["input"] == [{"type": "text", "text": "focus on module X"}]

    async def test_send_idle_starts_new_turn(self, rig):
        fake, manager = rig
        wire_thread(fake)
        wire_auto_turn(fake, final_text="first")
        agent = await manager.spawn("first task")
        await settle(manager, agent.name)

        wire_auto_turn(fake, turn_id="turn-2", final_text="second")
        how = await manager.send(agent.name, "follow-up task")
        assert how == "new-turn"
        await settle(manager, agent.name)
        completed = items_of(manager, "completed", agent.name)
        assert [item["text"] for item in completed] == ["first", "second"]

    async def test_send_unknown_agent(self, rig):
        _, manager = rig
        with pytest.raises(CuckooError, match="no agent named"):
            await manager.send("ghost", "hello")


class TestBridgeSocket:
    async def test_message_relay_lands_in_recent_activity(self, rig):
        fake, manager = rig
        wire_thread(fake)
        wire_auto_turn(fake)
        agent = await manager.spawn("task")

        reply = await relay(
            manager.socket_path,
            {
                "op": "message",
                "meta": {"thread_id": agent.thread.id, "turn_id": "turn-1"},
                "text": "found the bug in codec.py",
                "kind": "finding",
            },
        )
        assert reply == {"ok": True}
        messages = items_of(manager, "message", agent.name)
        assert messages and messages[0]["text"] == "found the bug in codec.py"
        assert messages[0]["data"]["kind"] == "finding"

    async def test_ask_blocks_until_answered(self, rig):
        fake, manager = rig
        wire_thread(fake)
        # a turn that never completes: the question stays pending
        fake.on("turn/start", lambda f, r: {"turn": turn_payload("turn-1")})
        agent = await manager.spawn("task")

        ask_task = asyncio.ensure_future(
            relay(
                manager.socket_path,
                {
                    "op": "ask",
                    "meta": {"thread_id": agent.thread.id},
                    "question": "delete the legacy dir?",
                    "options": ["yes", "no"],
                },
            )
        )
        async with asyncio.timeout(5):
            while not manager.pending_questions():
                await asyncio.sleep(0.01)
        question_id = manager.pending_questions()[0]["question_id"]
        posted = items_of(manager, "question", agent.name)
        assert posted and posted[0]["data"]["question_id"] == question_id
        assert not ask_task.done()  # the bridge is still blocked

        assert await manager.answer(question_id, "no — keep it") is True
        reply = await asyncio.wait_for(ask_task, timeout=5)
        assert reply == {"answer": "no — keep it"}
        assert manager.pending_questions() == []
        assert await manager.answer(question_id, "again") is False

    async def test_unknown_thread_still_posts(self, rig):
        fake, manager = rig
        await manager.start()
        reply = await relay(
            manager.socket_path,
            {"op": "message", "meta": {"thread_id": "mystery-thread"}, "text": "hi"},
        )
        assert reply == {"ok": True}
        assert manager.recent_activity()[0]["agent"].startswith("codex[mystery-")


class TestStatusSnapshot:
    async def test_status_file_tracks_busy_then_idle(self, rig, tmp_path):
        fake, manager = rig
        wire_thread(fake)
        go = asyncio.Event()
        tasks = []

        def gated(f, request):
            async def play():
                await go.wait()
                await f.play_turn("thread-1", "turn-1")

            tasks.append(asyncio.get_running_loop().create_task(play()))
            return {"turn": turn_payload("turn-1")}

        fake.on("turn/start", gated)
        await manager.spawn("task", name="tracked")

        status = json.loads((tmp_path / "status.json").read_text())
        assert status["agents"] == [
            {"agent": "tracked", "state": "idle", "busy": True}
        ] or status["agents"][0]["busy"] is True
        assert status["pid"] > 0

        go.set()
        await settle(manager, "tracked")
        status = json.loads((tmp_path / "status.json").read_text())
        assert status["agents"][0]["busy"] is False

    async def test_close_removes_status(self, rig, tmp_path):
        fake, manager = rig
        wire_thread(fake)
        wire_auto_turn(fake)
        await manager.spawn("task")
        await manager.aclose()
        assert not (tmp_path / "status.json").exists()


class TestJournal:
    async def test_events_journaled_for_hooks(self, rig, tmp_path):
        fake, manager = rig
        wire_thread(fake)
        wire_auto_turn(fake, final_text="done")
        await manager.spawn("task", name="journaled")
        await settle(manager, "journaled")

        events, delivered = Journal(tmp_path).read()
        completed = [e for e in events if e["kind"] == "completed"]
        assert completed and completed[0]["agent"] == "journaled"
        # hook notification is the sole delivery mechanism now
        assert delivered == set()

    async def test_item_ids_continue_across_restart(self, tmp_path):
        journal = Journal(tmp_path)
        journal.append_event({"id": 7, "agent": "old", "kind": "message", "text": "x"})
        manager = AgentManager(
            state_dir=tmp_path,
            socket_path=Path(tempfile.mkdtemp(dir="/tmp", prefix="cuckoo-mgr-")) / "b.sock",
            default_cwd=tmp_path,
        )
        item = manager._post("new", "message", "y", {})
        assert item.id == 8
        await manager.aclose()
