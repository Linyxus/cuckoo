"""CodexClient: owns one ``codex app-server`` process and its threads."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from cuckoo.codex import approvals as approvals_mod
from cuckoo.codex._events import EventBus, EventStream, Subscription
from cuckoo.codex._rpc.codec import RpcNotification, RpcRequest
from cuckoo.codex._rpc.connection import RpcConnection, RpcHandlerError
from cuckoo.codex._rpc.transport import StdioProcessTransport, Transport
from cuckoo.codex._version import check_codex_version, find_codex
from cuckoo.codex.approvals import ApprovalHandler
from cuckoo.codex.config import (
    DELTA_NOTIFICATION_METHODS,
    UNSET,
    ClientInfo,
    RetryPolicy,
    ShutdownPolicy,
    _Unset,
)
from cuckoo.codex.errors import ClientClosedError, RpcError, TransportError
from cuckoo.codex.inputs import TurnInput, normalize_input
from cuckoo.codex.protocol import methods
from cuckoo.codex.protocol.common import (
    ApprovalPolicy,
    Decision,
    ReasoningEffort,
    SandboxMode,
)
from cuckoo.codex.protocol.events import (
    ApprovalRequested,
    ApprovalResolved,
    Event,
    ProcessExited,
    parse_event,
)
from cuckoo.codex.protocol.models import (
    AuthStatus,
    InitializeResult,
    ModelInfo,
    ThreadInfo,
    ThreadPage,
    ThreadStartResult,
    TurnInfo,
    TurnStartResult,
)
from cuckoo.codex.protocol.server_requests import parse_approval_request
from cuckoo.codex.thread import Thread, ThreadOptions
from cuckoo.codex.turn import Turn

logger = logging.getLogger("cuckoo.codex.client")

#: Thread-routed events buffered until their Thread handle registers
#: (covers the race between a thread/start response and notifications).
_THREAD_EVENT_BUFFER = 512


def _toml_literal(value: Any) -> str:
    """Render a python value as a TOML literal for a ``-c key=value`` flag."""
    match value:
        case bool():
            return "true" if value else "false"
        case int() | float():
            return str(value)
        case str():
            return json.dumps(value)
        case list() | tuple():
            return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
        case dict():
            entries = ", ".join(
                f"{key} = {_toml_literal(item)}" for key, item in value.items()
            )
            return "{" + entries + "}"
        case _:
            raise TypeError(f"cannot render {type(value).__name__} as TOML")


def build_config_flags(overrides: Mapping[str, Any]) -> list[str]:
    flags: list[str] = []
    for key, value in overrides.items():
        flags.extend(["-c", f"{key}={_toml_literal(value)}"])
    return flags


class CodexClient:
    """One app-server process hosting many subagent threads.

    Use :meth:`connect` (or the async context manager form) to spawn and
    handshake. Everything not modeled is reachable via :meth:`request`.
    """

    def __init__(
        self,
        connection: RpcConnection,
        transport: Transport,
        *,
        approval_handler: ApprovalHandler | None = None,
        approval_timeout: float | None = None,
        codex_version: str | None = None,
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._bus = EventBus()
        self._threads: dict[str, Thread] = {}
        self._unrouted: collections.deque[Event] = collections.deque(
            maxlen=_THREAD_EVENT_BUFFER
        )
        self.approval_handler = approval_handler
        self.approval_timeout = approval_timeout
        self._codex_version = codex_version
        self._server_info = InitializeResult()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        *,
        codex_path: str | Path | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        codex_home: str | Path | None = None,
        config_overrides: Mapping[str, Any] | None = None,
        client_info: ClientInfo | None = None,
        suppress_deltas: bool = False,
        opt_out_notifications: Sequence[str] = (),
        experimental_api: bool = False,
        request_timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        shutdown: ShutdownPolicy | None = None,
        approval_handler: ApprovalHandler | None = None,
        approval_timeout: float | None = None,
        check_version: bool = True,
        transport: Transport | None = None,
    ) -> CodexClient:
        """Spawn ``codex app-server`` and perform the initialize handshake.

        ``codex_home`` isolates all agent state (config, auth, sessions)
        under a separate directory via the ``CODEX_HOME`` env var.
        ``transport`` injects a pre-built transport (tests, future brokers);
        the process-spawning options are ignored in that case.
        """
        codex_version: str | None = None
        if transport is None:
            binary = find_codex(codex_path)
            if check_version:
                codex_version = await check_codex_version(binary)
            spawn_env = dict(os.environ if env is None else env)
            if codex_home is not None:
                spawn_env["CODEX_HOME"] = str(codex_home)
            argv = [binary, "app-server"]
            if config_overrides:
                argv += build_config_flags(config_overrides)
            transport = await StdioProcessTransport.spawn(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=spawn_env,
                shutdown=shutdown,
            )

        connection = RpcConnection(
            transport,
            request_timeout=request_timeout,
            retry=retry,
        )
        client = cls(
            connection,
            transport,
            approval_handler=approval_handler,
            approval_timeout=approval_timeout,
            codex_version=codex_version,
        )
        connection._on_notification = client._on_notification
        connection._on_server_request = client._on_server_request
        connection._on_closed = client._on_connection_closed
        connection.start()

        opt_outs = list(opt_out_notifications)
        if suppress_deltas:
            opt_outs.extend(DELTA_NOTIFICATION_METHODS)
        info = client_info or ClientInfo()
        try:
            raw = await connection.request(
                methods.INITIALIZE,
                {
                    "clientInfo": info.to_params(),
                    "capabilities": {
                        "experimentalApi": experimental_api,
                        "optOutNotificationMethods": opt_outs,
                    },
                },
            )
            await connection.notify(methods.INITIALIZED, {})
        except BaseException:
            await client.aclose()
            raise
        client._server_info = InitializeResult.model_validate(raw or {})
        return client

    async def __aenter__(self) -> CodexClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Shut down the process and end all event streams; idempotent."""
        self._closed = True
        await self._connection.aclose()
        self._bus.close()

    @property
    def is_running(self) -> bool:
        return not self._closed and not self._connection.is_closed

    @property
    def server_info(self) -> InitializeResult:
        return self._server_info

    @property
    def codex_version(self) -> str | None:
        """Version reported by ``codex --version`` (None if not checked)."""
        return self._codex_version

    @property
    def stderr_tail(self) -> str:
        return getattr(self._transport, "stderr_tail", "")

    # -- threads (subagents) --------------------------------------------------

    async def start_thread(
        self,
        *,
        cwd: str | Path,
        model: str | None = None,
        sandbox: SandboxMode | str = SandboxMode.READ_ONLY,
        approval_policy: ApprovalPolicy | str | dict[str, Any] = ApprovalPolicy.NEVER,
        ephemeral: bool = False,
        name: str | None = None,
        base_instructions: str | None = None,
        developer_instructions: str | None = None,
        approval_handler: ApprovalHandler | None = None,
        auto_resume: bool = True,
        service_name: str = "cuckoo",
    ) -> Thread:
        """Spawn a new subagent thread."""
        options = ThreadOptions(
            cwd=str(cwd),
            model=model,
            sandbox=str(sandbox) if isinstance(sandbox, SandboxMode) else sandbox,
            approval_policy=(
                str(approval_policy)
                if isinstance(approval_policy, ApprovalPolicy)
                else approval_policy
            ),
            ephemeral=ephemeral,
        )
        raw = await self.request(
            methods.THREAD_START,
            methods.build_params(
                cwd=options.cwd,
                model=options.model,
                sandbox=options.sandbox,
                approvalPolicy=options.approval_policy,
                ephemeral=ephemeral,
                serviceName=service_name,
                baseInstructions=base_instructions,
                developerInstructions=developer_instructions,
            ),
        )
        result = ThreadStartResult.model_validate(raw)
        thread = self._register_thread(
            result.thread,
            options=options,
            approval_handler=approval_handler,
            auto_resume=auto_resume,
        )
        if name is not None:
            try:
                await thread.set_name(name)
            except RpcError as exc:
                # e.g. ephemeral threads reject metadata updates
                logger.debug("could not name thread %s: %s", thread.id, exc)
        return thread

    async def resume_thread(
        self,
        thread_id: str,
        *,
        cwd: str | Path | None = None,
        model: str | None = None,
        sandbox: SandboxMode | str | None = None,
        approval_policy: ApprovalPolicy | str | dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
        auto_resume: bool = True,
    ) -> Thread:
        """Reopen a persisted thread by id and manage it as a subagent."""
        raw = await self.request(
            methods.THREAD_RESUME,
            methods.build_params(
                threadId=thread_id,
                cwd=str(cwd) if cwd is not None else None,
                model=model,
                sandbox=str(sandbox) if isinstance(sandbox, SandboxMode) else sandbox,
                approvalPolicy=(
                    str(approval_policy)
                    if isinstance(approval_policy, ApprovalPolicy)
                    else approval_policy
                ),
            ),
        )
        result = ThreadStartResult.model_validate(raw)
        options = ThreadOptions(
            cwd=str(cwd) if cwd is not None else result.cwd,
            model=model,
            sandbox=str(sandbox) if isinstance(sandbox, SandboxMode) else sandbox,
            approval_policy=(
                str(approval_policy)
                if isinstance(approval_policy, ApprovalPolicy)
                else approval_policy
            ),
        )
        return self._register_thread(
            result.thread,
            options=options,
            approval_handler=approval_handler,
            auto_resume=auto_resume,
        )

    async def fork_thread(
        self,
        thread_id: str,
        *,
        approval_handler: ApprovalHandler | None = None,
        auto_resume: bool = True,
        **overrides: Any,
    ) -> Thread:
        """Branch a thread's history into a new thread."""
        raw = await self.request(
            methods.THREAD_FORK,
            methods.build_params(threadId=thread_id, **overrides),
        )
        result = ThreadStartResult.model_validate(raw)
        options = ThreadOptions(cwd=result.cwd)
        return self._register_thread(
            result.thread,
            options=options,
            approval_handler=approval_handler,
            auto_resume=auto_resume,
        )

    def get_thread(self, thread_id: str) -> Thread | None:
        """A managed thread handle by id (only threads this client opened)."""
        return self._threads.get(thread_id)

    @property
    def threads(self) -> list[Thread]:
        return list(self._threads.values())

    async def list_threads(
        self,
        *,
        cwd: str | Path | None = None,
        limit: int | None = None,
        search_term: str | None = None,
        archived: bool | None = None,
        source_kinds: Sequence[str] | None = None,
        cursor: str | None = None,
        sort_key: str | None = None,
    ) -> ThreadPage:
        """Page through persisted threads (managed or not)."""
        raw = await self.request(
            methods.THREAD_LIST,
            methods.build_params(
                cwd=str(cwd) if cwd is not None else None,
                limit=limit,
                searchTerm=search_term,
                archived=archived,
                sourceKinds=list(source_kinds) if source_kinds else None,
                cursor=cursor,
                sortKey=sort_key,
            ),
        )
        return ThreadPage.model_validate(raw)

    async def iter_threads(
        self, *, page_size: int = 50, **filters: Any
    ) -> AsyncIterator[ThreadInfo]:
        """Auto-paginating iterator over persisted threads."""
        cursor: str | None = None
        while True:
            page = await self.list_threads(limit=page_size, cursor=cursor, **filters)
            for info in page.data:
                yield info
            cursor = page.next_cursor
            if cursor is None or not page.data:
                return

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = False
    ) -> ThreadInfo:
        """Fetch a persisted thread without resuming/loading it."""
        raw = await self.request(
            methods.THREAD_READ,
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        payload = raw.get("thread") if isinstance(raw, dict) else raw
        return ThreadInfo.model_validate(payload)

    async def archive_thread(self, thread_id: str) -> None:
        await self.request(methods.THREAD_ARCHIVE, {"threadId": thread_id})

    async def unarchive_thread(self, thread_id: str) -> None:
        await self.request(methods.THREAD_UNARCHIVE, {"threadId": thread_id})

    async def delete_thread(self, thread_id: str) -> None:
        await self.request(methods.THREAD_DELETE, {"threadId": thread_id})
        self._threads.pop(thread_id, None)

    # -- environment ------------------------------------------------------------

    async def auth_status(self) -> AuthStatus:
        raw = await self.request(methods.GET_AUTH_STATUS, {})
        return AuthStatus.model_validate(raw or {})

    async def account(self) -> dict[str, Any]:
        return await self.request(methods.ACCOUNT_READ, {"refreshToken": False})

    async def list_models(self) -> list[ModelInfo]:
        raw = await self.request(methods.MODEL_LIST, {})
        data = raw.get("data", []) if isinstance(raw, dict) else []
        return [ModelInfo.model_validate(entry) for entry in data]

    async def read_config(self) -> dict[str, Any]:
        return await self.request(methods.CONFIG_READ, {})

    # -- events -------------------------------------------------------------------

    def events(self, **stream_options: Any) -> EventStream:
        """Stream every event on this client (all threads + client scope)."""
        return self._bus.stream(**stream_options)

    def subscribe(self, callback: Any, *, kinds: tuple[type, ...] | None = None) -> Subscription:
        return self._bus.subscribe(callback, kinds=kinds)

    # -- escape hatches ---------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None | _Unset = UNSET,
    ) -> Any:
        """Send any JSON-RPC request (un-modeled methods included)."""
        if self._closed:
            raise ClientClosedError("client is closed")
        return await self._connection.request(method, params, timeout=timeout)

    async def notify(self, method: str, params: Any = None) -> None:
        if self._closed:
            raise ClientClosedError("client is closed")
        await self._connection.notify(method, params)

    # -- turn plumbing (used by Thread/Turn) --------------------------------------

    async def _start_turn(
        self,
        *,
        thread_id: str,
        input: TurnInput,
        model: str | None,
        effort: ReasoningEffort | str | None,
        output_schema: dict[str, Any] | None,
    ) -> TurnInfo:
        raw = await self.request(
            methods.TURN_START,
            methods.build_params(
                threadId=thread_id,
                input=normalize_input(input),
                model=model,
                effort=str(effort) if isinstance(effort, ReasoningEffort) else effort,
                outputSchema=output_schema,
            ),
        )
        return TurnStartResult.model_validate(raw).turn

    async def _steer_turn(self, thread_id: str, turn_id: str, input: TurnInput) -> None:
        await self.request(
            methods.TURN_STEER,
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": normalize_input(input),
            },
        )

    async def _interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request(
            methods.TURN_INTERRUPT, {"threadId": thread_id, "turnId": turn_id}
        )

    async def _resume_in_place(self, thread: Thread) -> None:
        """Transparently reload an auto-unloaded thread, keeping the handle."""
        options = thread._options
        await self.request(
            methods.THREAD_RESUME,
            methods.build_params(
                threadId=thread.id,
                cwd=options.cwd,
                model=options.model,
                sandbox=options.sandbox,
                approvalPolicy=options.approval_policy,
            ),
        )

    # -- routing --------------------------------------------------------------------

    def _register_thread(
        self,
        info: ThreadInfo,
        *,
        options: ThreadOptions,
        approval_handler: ApprovalHandler | None,
        auto_resume: bool,
    ) -> Thread:
        existing = self._threads.get(info.id)
        if existing is not None:
            existing._info = info
            return existing
        thread = Thread(
            self,
            info,
            options=options,
            approval_handler=approval_handler,
            auto_resume=auto_resume,
        )
        self._threads[info.id] = thread
        # Replay events that arrived before the handle existed (already
        # emitted on the bus; only internal state needs catching up).
        buffered = [
            event
            for event in self._unrouted
            if event.routing_thread_id == info.id
        ]
        for event in buffered:
            self._unrouted.remove(event)
            for synthesized in thread._apply(event):
                self._route(synthesized)
        return thread

    def _on_notification(self, frame: RpcNotification) -> None:
        self._route(parse_event(frame.method, frame.params))

    def _route(self, event: Event) -> None:
        """Apply an event internally (state, accumulators), then fan out."""
        thread_id = event.routing_thread_id
        synthesized: list[Event] = []
        if thread_id is not None:
            thread = self._threads.get(thread_id)
            if thread is not None:
                synthesized = thread._apply(event)
            else:
                self._unrouted.append(event)
        self._bus.emit(event)
        for follow_up in synthesized:
            self._route(follow_up)

    def _on_connection_closed(self, reason: TransportError | None) -> None:
        self._closed = True
        event = ProcessExited(
            message=str(reason) if reason else "connection closed",
            exit_code=getattr(reason, "exit_code", None),
            signal=getattr(reason, "signal", None),
            stderr_tail=getattr(reason, "stderr_tail", ""),
        )
        for thread in self._threads.values():
            for synthesized in thread._apply(event):
                self._bus.emit(synthesized)
        self._bus.emit(event)
        self._bus.close()

    # -- approvals ---------------------------------------------------------------

    async def _on_server_request(self, request: RpcRequest) -> Any:
        approval = parse_approval_request(request.method, request.params)
        if approval is None:
            # Same reply the official plugin gives every server request.
            raise RpcHandlerError(
                -32601, f"Unsupported server request: {request.method}"
            )
        self._route(ApprovalRequested(request=approval))
        decision, source = await self._decide(approval)
        self._route(
            ApprovalResolved(request=approval, decision=decision, source=source)
        )
        return approval.build_response(decision)

    async def _decide(self, approval: Any) -> tuple[Decision, str]:
        thread = (
            self._threads.get(approval.thread_id) if approval.thread_id else None
        )
        turn: Turn | None = None
        if thread is not None:
            turn = thread.current_turn
            if (
                turn is not None
                and approval.turn_id is not None
                and turn.id != approval.turn_id
            ):
                turn = thread._turns.get(approval.turn_id)
        scope_chain: list[tuple[str, ApprovalHandler | None]] = [
            ("turn", getattr(turn, "approval_handler", None)),
            ("thread", thread.approval_handler if thread else None),
            ("client", self.approval_handler),
        ]
        try:
            async with asyncio.timeout(self.approval_timeout):
                for source, handler in scope_chain:
                    decision = await approvals_mod.evaluate(handler, approval)
                    if decision is not None:
                        return decision, source
        except TimeoutError:
            logger.warning(
                "approval timed out after %.1fs; denying %s",
                self.approval_timeout,
                approval.method,
            )
            return Decision.DENIED, "timeout"
        logger.warning(
            "no approval handler decided %s for thread %s; denying by default",
            approval.method,
            approval.thread_id,
        )
        return Decision.DENIED, "default"
