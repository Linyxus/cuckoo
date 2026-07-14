"""Live integration tests against the real, authenticated codex CLI.

Run with: CUCKOO_LIVE=1 uv run pytest -m live
Kept to three tests to bound cost and flakiness.
"""

import asyncio
import os
from pathlib import Path

import pytest

from cuckoo.codex import (
    AgentState,
    CodexClient,
    ItemStarted,
    TurnStatus,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CUCKOO_LIVE") != "1",
        reason="live tests need CUCKOO_LIVE=1 and an authenticated codex",
    ),
    pytest.mark.timeout(180),
]

REPO_ROOT = Path(__file__).parent.parent.parent


@pytest.fixture
async def client():
    client = await CodexClient.connect(cwd=REPO_ROOT)
    yield client
    await client.aclose()


async def test_handshake_and_environment(client):
    assert client.is_running
    assert client.codex_version  # version gate ran
    assert client.server_info.codex_home

    auth = await client.auth_status()
    assert auth.is_authenticated

    models = await client.list_models()
    assert models
    assert any(m.id for m in models)


async def test_ephemeral_readonly_turn(client):
    agent = await client.start_thread(cwd=REPO_ROOT, ephemeral=True)
    result = await agent.run(
        "Reply with exactly: OK (nothing else, no punctuation)", timeout=150
    )
    assert result.status == TurnStatus.COMPLETED
    assert result.final_message is not None
    assert "OK" in result.final_message
    assert result.token_usage is not None
    assert result.token_usage.total_tokens > 0
    assert agent.state is AgentState.IDLE
    assert agent.last_agent_message == result.final_message


async def test_bridge_messaging_loop(tmp_path):
    """Full bidirectional loop: agent messages us, blocks on a question,
    uses our answer in its final reply."""
    import tempfile

    from cuckoo.manager import AgentManager

    manager = AgentManager(
        state_dir=tmp_path,
        socket_path=Path(tempfile.mkdtemp(dir="/tmp", prefix="cuckoo-live-")) / "b.sock",
        default_cwd=REPO_ROOT,
    )
    try:
        agent = await manager.spawn(
            "This is a communication test. Do exactly this, in order:\n"
            "1. Call the send_message tool (cuckoo_bridge MCP server) with "
            'text "starting" and kind "progress".\n'
            '2. Call the ask tool with question "Which color?" and options '
            '["red", "blue"].\n'
            "3. Reply with exactly: PICKED <answer>\n"
            "Do not run any commands or read any files.",
            name="live-loop",
            sandbox="read-only",
            ephemeral=True,
        )
        # Answer the blocking question when it appears, then await the turn.
        async with asyncio.timeout(150):
            while not agent.current_task.done():
                for question in manager.pending_questions():
                    await manager.answer(question["question_id"], "blue")
                await asyncio.sleep(1)
        kinds = {item["kind"] for item in manager.recent_activity(limit=50)}
        assert {"message", "question", "completed"} <= kinds
        result = manager.result("live-loop")
        assert result is not None and "PICKED blue" in (result.final_message or "")
    finally:
        await manager.aclose()


async def test_interrupt_running_turn(client):
    agent = await client.start_thread(cwd=REPO_ROOT, ephemeral=True)
    turn = await agent.start_turn(
        "Count from 1 to 50000, one number per line, in your reply. Do not stop early."
    )
    # Wait until the model is actually working, then interrupt.
    with agent.events(kinds=(ItemStarted,)) as stream:
        try:
            await stream.next(timeout=60)
        except TimeoutError:
            pass  # even without an item event, interrupt must work
    await agent.interrupt()
    result = await turn.result(timeout=60)
    assert result.status in (TurnStatus.INTERRUPTED, TurnStatus.COMPLETED)
    assert agent.state is AgentState.IDLE
