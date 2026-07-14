"""AgentManager: the cuckoo host that owns Codex subagents.

Lives inside the Claude-facing MCP server process. Responsibilities:

- one lazily-connected :class:`CodexClient` with the bridge MCP server
  injected into every Codex thread (``mcp_servers.cuckoo_bridge.*``)
- named agents, each running turns as background tasks
- an inbox of activity items (subagent messages, blocking questions, turn
  completions, errors) delivered to Claude via ``wait()`` and journaled for
  the hook monitors
- a unix socket the injected bridges dial to relay ``send_message``/``ask``
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import cuckoo.bridge
from cuckoo.codex import (
    AgentState,
    ApprovalPolicy,
    CodexClient,
    CuckooError,
    NoActiveTurnError,
    ProcessExited,
    TurnResult,
)
from cuckoo.journal import Journal, project_state_dir

logger = logging.getLogger("cuckoo.manager")

BRIEFING_TEMPLATE = """\
You are a Codex subagent named "{name}", working for a Claude Code \
orchestrator through the cuckoo bridge.

The cuckoo_bridge MCP server connects you to your orchestrator:
- send_message(text, kind): fire-and-forget updates. Report significant \
findings, milestones, or blockers as you work — not minor steps.
- ask(question, options): blocks until the orchestrator answers. Use it only \
when you genuinely cannot proceed without a decision; prefer making \
reasonable, clearly-stated assumptions.

Your final message ends the turn and is delivered to the orchestrator \
automatically — do not repeat it via send_message. The orchestrator may \
inject follow-up instructions mid-turn; treat them as authoritative."""


@dataclass
class InboxItem:
    id: int
    ts: float
    agent: str
    kind: str  # message | question | completed | error
    text: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "agent": self.agent,
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
        }


@dataclass
class PendingQuestion:
    id: str
    agent: str
    question: str
    options: list[str] | None
    future: asyncio.Future[str]


class ManagedAgent:
    def __init__(self, name: str, thread: Any, *, model: str | None, effort: str | None) -> None:
        self.name = name
        self.thread = thread
        self.model = model
        self.effort = effort
        self.created_at = time.time()
        self.current_task: asyncio.Task[None] | None = None
        self.last_result: TurnResult | None = None
        self.last_error: str | None = None
        self.turns_completed = 0

    @property
    def busy(self) -> bool:
        return self.current_task is not None and not self.current_task.done()

    @property
    def state(self) -> str:
        return str(self.thread.state)

    def snapshot(self) -> dict[str, Any]:
        usage = self.thread.token_usage
        return {
            "agent": self.name,
            "state": self.state,
            "busy": self.busy,
            "model": self.model or "(codex default)",
            "effort": self.effort or "(codex default)",
            "cwd": self.thread._options.cwd,
            "turns_completed": self.turns_completed,
            "last_turn_status": str(self.last_result.status) if self.last_result else None,
            "last_error": self.last_error,
            "total_tokens": usage.total.total_tokens if usage else None,
            "thread_id": self.thread.id,
        }


class AgentManager:
    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        socket_path: Path | None = None,
        client_factory: Callable[[], Awaitable[CodexClient]] | None = None,
        default_cwd: Path | None = None,
    ) -> None:
        self.state_dir = state_dir or project_state_dir()
        self.journal = Journal(self.state_dir)
        self.socket_path = socket_path or (self.state_dir / "bridge.sock")
        self._client_factory = client_factory
        self._client: CodexClient | None = None
        self._client_lock = asyncio.Lock()
        self._agents: dict[str, ManagedAgent] = {}
        self._by_thread: dict[str, ManagedAgent] = {}
        self._recent: collections.deque[InboxItem] = collections.deque(maxlen=50)
        self._questions: dict[str, PendingQuestion] = {}
        self._item_ids = itertools.count(self.journal.last_event_id() + 1)
        self._agent_numbers = itertools.count(1)
        self._socket_server: asyncio.AbstractServer | None = None
        self._default_cwd = default_cwd or Path.cwd()
        self.default_model = os.environ.get("CUCKOO_DEFAULT_MODEL") or None
        self.default_effort = os.environ.get("CUCKOO_DEFAULT_EFFORT") or None
        self.default_sandbox = os.environ.get("CUCKOO_DEFAULT_SANDBOX", "workspace-write")

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._socket_server is None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            self.socket_path.unlink(missing_ok=True)
            self._socket_server = await asyncio.start_unix_server(
                self._handle_bridge_connection, path=str(self.socket_path)
            )

    async def aclose(self) -> None:
        for agent in self._agents.values():
            if agent.current_task is not None:
                agent.current_task.cancel()
        for question in self._questions.values():
            if not question.future.done():
                question.future.set_exception(CuckooError("cuckoo host shutting down"))
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._socket_server is not None:
            self._socket_server.close()
            await self._socket_server.wait_closed()
            self._socket_server = None
        self.socket_path.unlink(missing_ok=True)
        # No agents survive this process; leave a quiescent status behind.
        (self.state_dir / "status.json").unlink(missing_ok=True)

    async def _ensure_client(self) -> CodexClient:
        async with self._client_lock:
            if self._client is not None and self._client.is_running:
                return self._client
            await self.start()
            if self._client_factory is not None:
                self._client = await self._client_factory()
            else:
                self._client = await CodexClient.connect(
                    codex_path=os.environ.get("CUCKOO_CODEX_PATH") or None,
                    config_overrides=self._bridge_overrides(),
                    suppress_deltas=True,
                )
            self._client.subscribe(self._on_process_exited, kinds=(ProcessExited,))
            return self._client

    def _bridge_overrides(self) -> dict[str, Any]:
        bridge_path = Path(cuckoo.bridge.__file__).resolve()
        return {
            "mcp_servers.cuckoo_bridge.command": sys.executable,
            "mcp_servers.cuckoo_bridge.args": [str(bridge_path)],
            "mcp_servers.cuckoo_bridge.env": {
                "CUCKOO_BRIDGE_SOCKET": str(self.socket_path)
            },
            "mcp_servers.cuckoo_bridge.startup_timeout_sec": 30,
            "mcp_servers.cuckoo_bridge.tool_timeout_sec": 3600,
        }

    def _on_process_exited(self, event: ProcessExited) -> None:
        self._post(
            "cuckoo",
            "error",
            f"codex app-server died: {event.message}. Agents will restart lazily "
            "on the next spawn/send.",
            {"stderr_tail": event.stderr_tail[-2000:] if event.stderr_tail else ""},
        )

    # -- agent operations ------------------------------------------------------

    async def spawn(
        self,
        task: str,
        *,
        name: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        instructions: str | None = None,
        ephemeral: bool = False,
    ) -> ManagedAgent:
        client = await self._ensure_client()
        agent_name = self._unique_name(name)
        briefing = BRIEFING_TEMPLATE.format(name=agent_name)
        if instructions:
            briefing += f"\n\nAdditional instructions from the orchestrator:\n{instructions}"
        thread = await client.start_thread(
            cwd=cwd or self._default_cwd,
            model=model or self.default_model,
            sandbox=sandbox or self.default_sandbox,
            approval_policy=approval_policy or ApprovalPolicy.NEVER,
            ephemeral=ephemeral,
            developer_instructions=briefing,
            name=f"Cuckoo agent: {agent_name}",
        )
        agent = ManagedAgent(
            agent_name, thread, model=model or self.default_model,
            effort=effort or self.default_effort,
        )
        self._agents[agent_name] = agent
        self._by_thread[thread.id] = agent
        self._start_turn(agent, task)
        return agent

    async def send(self, agent_name: str, message: str, *, mode: str = "auto") -> str:
        """Message an agent. ``steer`` injects into the running turn,
        ``new-turn`` starts a fresh turn, ``auto`` picks for you."""
        agent = self._require(agent_name)
        if mode not in ("auto", "steer", "new-turn"):
            raise CuckooError(f"unknown send mode: {mode!r}")
        if mode in ("auto", "steer"):
            # A turn started moments ago may not have its handle registered
            # yet (turn/start response in flight) — retry briefly while the
            # agent is busy instead of misclassifying it as idle.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while True:
                try:
                    await agent.thread.steer(message)
                    return "steered"
                except NoActiveTurnError:
                    if agent.busy and loop.time() < deadline:
                        await asyncio.sleep(0.02)
                        continue
                    if mode == "steer":
                        raise CuckooError(
                            f"agent {agent_name!r} has no running turn to steer"
                        ) from None
                    break
        if agent.busy:
            raise CuckooError(
                f"agent {agent_name!r} is mid-turn; use mode='steer' or wait"
            )
        self._start_turn(agent, message)
        return "new-turn"

    def status(self) -> list[dict[str, Any]]:
        return [agent.snapshot() for agent in self._agents.values()]

    def recent_activity(self, limit: int = 10) -> list[dict[str, Any]]:
        """The last activity items (full text), newest last. Non-blocking:
        detail lookup for hook-notified activity, never a wait primitive."""
        items = list(self._recent)[-limit:]
        return [item.to_dict() for item in items]

    def pending_questions(self) -> list[dict[str, Any]]:
        return [
            {
                "question_id": q.id,
                "agent": q.agent,
                "question": q.question,
                "options": q.options,
            }
            for q in self._questions.values()
        ]

    def result(self, agent_name: str) -> TurnResult | None:
        return self._require(agent_name).last_result

    async def answer(self, question_id: str, answer_text: str) -> bool:
        question = self._questions.pop(question_id, None)
        if question is None or question.future.done():
            return False
        question.future.set_result(answer_text)
        self._write_status()
        return True

    async def interrupt(self, agent_name: str) -> None:
        await self._require(agent_name).thread.interrupt()

    def get(self, agent_name: str) -> ManagedAgent | None:
        return self._agents.get(agent_name)

    # -- internals ----------------------------------------------------------------

    def _require(self, agent_name: str) -> ManagedAgent:
        agent = self._agents.get(agent_name)
        if agent is None:
            known = ", ".join(self._agents) or "(none)"
            raise CuckooError(f"no agent named {agent_name!r}; known agents: {known}")
        return agent

    def _unique_name(self, name: str | None) -> str:
        if name and name not in self._agents:
            return name
        base = name or "codex"
        while True:
            candidate = f"{base}-{next(self._agent_numbers)}"
            if candidate not in self._agents:
                return candidate

    def _start_turn(self, agent: ManagedAgent, input_text: str) -> None:
        async def run() -> None:
            try:
                result = await agent.thread.run(
                    input_text, effort=agent.effort, raise_on_failure=False
                )
                agent.last_result = result
                agent.turns_completed += 1
                files = [
                    change.path
                    for file_change in result.file_changes
                    for change in file_change.changes
                ]
                self._post(
                    agent.name,
                    "completed",
                    result.final_message or f"(no final message; status: {result.status})",
                    {
                        "status": str(result.status),
                        "files_changed": files,
                        "commands_run": len(result.command_executions),
                        "tokens": (
                            result.token_usage.total_tokens if result.token_usage else None
                        ),
                        "error": result.error.message if result.error else None,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                agent.last_error = str(exc)
                logger.exception("turn task failed for %s", agent.name)
                self._post(agent.name, "error", str(exc), {})

        agent.current_task = asyncio.create_task(run(), name=f"cuckoo-turn-{agent.name}")
        # The done callback runs after the task is finished, so the snapshot
        # sees busy=False (a write inside the task itself would not).
        agent.current_task.add_done_callback(lambda _task: self._write_status())
        self._write_status()

    def _post(self, agent: str, kind: str, text: str, data: dict[str, Any]) -> InboxItem:
        item = InboxItem(
            id=next(self._item_ids),
            ts=time.time(),
            agent=agent,
            kind=kind,
            text=text,
            data=data,
        )
        self._recent.append(item)
        try:
            self.journal.append_event(item.to_dict())
        except OSError:
            logger.exception("journal write failed")
        self._write_status()
        return item

    def _write_status(self) -> None:
        """Publish a liveness snapshot for the hook monitors: they use it to
        keep Claude from stopping while agents are still working."""
        status = {
            "pid": os.getpid(),
            "ts": time.time(),
            "agents": [
                {"agent": a.name, "state": a.state, "busy": a.busy}
                for a in self._agents.values()
            ],
            "pending_questions": len(self._questions),
        }
        try:
            (self.state_dir / "status.json").write_text(json.dumps(status))
        except OSError:
            logger.exception("status write failed")

    # -- bridge socket ---------------------------------------------------------------

    async def _handle_bridge_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    reply: dict[str, Any] = {"error": "invalid JSON"}
                else:
                    reply = await self._handle_bridge_message(message)
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def _handle_bridge_message(self, message: dict[str, Any]) -> dict[str, Any]:
        meta = message.get("meta") or {}
        thread_id = meta.get("thread_id") or meta.get("session_id") or ""
        agent = self._by_thread.get(thread_id)
        agent_name = agent.name if agent else f"codex[{thread_id[:8] or 'unknown'}]"
        match message.get("op"):
            case "message":
                self._post(
                    agent_name,
                    "message",
                    str(message.get("text", "")),
                    {"kind": message.get("kind", "info"), "turn_id": meta.get("turn_id")},
                )
                return {"ok": True}
            case "ask":
                question_id = f"q-{uuid.uuid4().hex[:8]}"
                future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                options = message.get("options")
                self._questions[question_id] = PendingQuestion(
                    id=question_id,
                    agent=agent_name,
                    question=str(message.get("question", "")),
                    options=options if isinstance(options, list) else None,
                    future=future,
                )
                self._post(
                    agent_name,
                    "question",
                    str(message.get("question", "")),
                    {
                        "question_id": question_id,
                        "options": options,
                        "note": "agent is BLOCKED until codex_answer is called",
                    },
                )
                try:
                    answer = await future
                except CuckooError as exc:
                    return {"error": str(exc)}
                return {"answer": answer}
            case unknown:
                return {"error": f"unknown op: {unknown}"}
