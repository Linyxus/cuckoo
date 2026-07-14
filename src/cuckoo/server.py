"""Cuckoo MCP server — the tools Claude Code uses to run Codex subagents.

Run with: ``cuckoo-server`` (or ``python -m cuckoo.server``). Speaks MCP
over stdio; Claude Code launches it via the plugin's ``.mcp.json``.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from cuckoo.manager import AgentManager

mcp = FastMCP(
    "cuckoo",
    instructions=(
        "Codex subagents for Claude Code. Spawn OpenAI Codex agents that work "
        "in the background, message them mid-task, and collect their results. "
        "Their activity is PUSHED to you via hook notifications — while you "
        "work, when you try to stop, and on the next user prompt. There is "
        "no wait tool and nothing to poll: to wait for a subagent, simply "
        "finish your turn; notifications bring you back. Subagents may "
        "message you or block on a question (answer promptly with "
        "codex_answer). codex_status/codex_result are instant detail lookups."
    ),
)

_manager: AgentManager | None = None


def get_manager() -> AgentManager:
    global _manager
    if _manager is None:
        _manager = AgentManager()
    return _manager


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


@mcp.tool()
async def codex_spawn(
    task: str,
    name: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    sandbox: str = "workspace-write",
    instructions: str | None = None,
) -> str:
    """Spawn a Codex subagent and start it on a task in the background.

    Returns immediately with the agent's name; results and messages are
    pushed to you as notifications while you work (and at stop time). Give
    the task full context — the agent cannot see this conversation.

    Args:
        task: The complete task prompt (include relevant context and paths).
        name: Optional friendly name; auto-generated (codex-1, ...) if omitted.
        cwd: Working directory for the agent (default: this project).
        model: Codex model override, e.g. "gpt-5.6" (default: user's codex config).
        effort: Reasoning effort: minimal | low | medium | high | xhigh.
        sandbox: read-only | workspace-write | danger-full-access
            (default workspace-write: can edit files in cwd, no network).
        instructions: Extra standing instructions for the agent (style, scope,
            constraints) — applied on top of the task.
    """
    manager = get_manager()
    agent = await manager.spawn(
        task,
        name=name,
        cwd=cwd,
        model=model,
        effort=effort,
        sandbox=sandbox,
        instructions=instructions,
    )
    return _dump(
        {
            "agent": agent.name,
            "state": agent.state,
            "note": (
                "Running in background — keep working, or finish your turn "
                "if nothing else remains; its updates and results will be "
                "pushed to you via notifications. codex_send to add "
                "instructions."
            ),
        }
    )


@mcp.tool()
async def codex_send(agent: str, message: str, mode: str = "auto") -> str:
    """Send a message to a Codex subagent (main-agent -> subagent messaging).

    Args:
        agent: The agent's name (from codex_spawn / codex_status).
        message: The instruction or information to deliver.
        mode: "auto" (steer the running turn, else start a new turn),
            "steer" (inject into the running turn only, fails when idle),
            "new-turn" (start a fresh turn; fails while the agent is busy).
    """
    manager = get_manager()
    how = await manager.send(agent, message, mode=mode)
    return _dump({"agent": agent, "delivered": how})


@mcp.tool()
async def codex_status() -> str:
    """Instant snapshot of all Codex subagents: state, busy/idle, model,
    token usage, unanswered questions blocking agents, and the most recent
    activity items in full. Never blocks — do NOT call it in a loop to wait
    for agents; finish your turn instead and notifications will bring the
    activity to you."""
    manager = get_manager()
    return _dump(
        {
            "agents": manager.status(),
            "pending_questions": manager.pending_questions(),
            "recent_activity": manager.recent_activity(),
        }
    )


@mcp.tool()
async def codex_result(agent: str) -> str:
    """Full result of an agent's most recent completed turn: final message,
    file changes, commands run, token usage, and error details if any."""
    manager = get_manager()
    result = manager.result(agent)
    if result is None:
        return _dump({"agent": agent, "result": None, "note": "no completed turn yet"})
    return _dump(
        {
            "agent": agent,
            "status": str(result.status),
            "final_message": result.final_message,
            "file_changes": [
                {"path": change.path, "kind": str(change.kind)}
                for file_change in result.file_changes
                for change in file_change.changes
            ],
            "commands_run": [
                {"command": item.command, "exit_code": item.exit_code}
                for item in result.command_executions
            ],
            "tokens": result.token_usage.total_tokens if result.token_usage else None,
            "error": result.error.message if result.error else None,
        }
    )


@mcp.tool()
async def codex_answer(question_id: str, answer: str) -> str:
    """Answer a blocking question from a Codex subagent (the agent is frozen
    until this is called). Question ids come from notifications and
    codex_status."""
    manager = get_manager()
    resolved = await manager.answer(question_id, answer)
    if not resolved:
        return _dump(
            {
                "ok": False,
                "note": f"no pending question {question_id!r} (already answered or timed out)",
                "pending_questions": manager.pending_questions(),
            }
        )
    return _dump({"ok": True, "question_id": question_id})


@mcp.tool()
async def codex_interrupt(agent: str) -> str:
    """Interrupt a Codex subagent's running turn (graceful; the agent stays
    alive and can take new instructions via codex_send)."""
    manager = get_manager()
    await manager.interrupt(agent)
    return _dump({"agent": agent, "interrupted": True})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
