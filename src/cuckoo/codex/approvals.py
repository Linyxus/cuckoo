"""Approval handlers: decide server-requested approvals programmatically.

A handler returns a :class:`Decision`, or ``None`` to abstain — the
dispatcher then falls through the scope chain turn → thread → client →
built-in default (DENIED, with a warning). Handlers may be sync or async.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Awaitable, Callable, Sequence

from cuckoo.codex.protocol.common import Decision
from cuckoo.codex.protocol.server_requests import (
    ApprovalRequest,
    CommandApproval,
    FileChangeApproval,
)

ApprovalHandler = Callable[
    [ApprovalRequest], Decision | None | Awaitable[Decision | None]
]


async def evaluate(
    handler: ApprovalHandler | None, request: ApprovalRequest
) -> Decision | None:
    """Invoke a handler, awaiting if needed; ``None`` handler abstains."""
    if handler is None:
        return None
    decision = handler(request)
    if isinstance(decision, Awaitable):
        decision = await decision
    return decision


def APPROVE_ALL(request: ApprovalRequest) -> Decision:
    return Decision.APPROVED


def DENY_ALL(request: ApprovalRequest) -> Decision:
    return Decision.DENIED


def chain(*handlers: ApprovalHandler) -> ApprovalHandler:
    """Combine handlers; the first non-``None`` decision wins."""

    async def chained(request: ApprovalRequest) -> Decision | None:
        for handler in handlers:
            decision = await evaluate(handler, request)
            if decision is not None:
                return decision
        return None

    return chained


def _compile(patterns: Sequence[str | re.Pattern[str]]) -> list[re.Pattern[str]]:
    return [
        pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
        for pattern in patterns
    ]


def _is_under(path: str, roots: Sequence[PurePath]) -> bool:
    candidate = PurePath(path)
    return any(candidate.is_relative_to(root) for root in roots)


class RuleBasedApprovals:
    """Composable allow/deny policy; itself an :class:`ApprovalHandler`.

    Deny rules always win over allow rules. Requests not covered by any
    rule get ``default`` (pass ``default=None`` to abstain so a later
    handler in the chain decides).
    """

    def __init__(
        self,
        *,
        allow_commands: Sequence[str | re.Pattern[str]] = (),
        deny_commands: Sequence[str | re.Pattern[str]] = (),
        allow_write_paths: Sequence[str | PurePath] = (),
        default: Decision | None = Decision.DENIED,
    ) -> None:
        self._allow_commands = _compile(allow_commands)
        self._deny_commands = _compile(deny_commands)
        self._allow_write_paths = [PurePath(p) for p in allow_write_paths]
        self._default = default

    def __call__(self, request: ApprovalRequest) -> Decision | None:
        if isinstance(request, CommandApproval):
            command = request.command_text
            if any(p.search(command) for p in self._deny_commands):
                return Decision.DENIED
            if any(p.search(command) for p in self._allow_commands):
                return Decision.APPROVED
            return self._default
        if isinstance(request, FileChangeApproval):
            paths = _change_paths(request)
            if (
                self._allow_write_paths
                and paths
                and all(_is_under(path, self._allow_write_paths) for path in paths)
            ):
                return Decision.APPROVED
            return self._default
        return self._default


def _change_paths(request: FileChangeApproval) -> list[str]:
    changes = request.changes
    if isinstance(changes, dict):  # legacy: {path: FileChange}
        return [str(path) for path in changes]
    if isinstance(changes, list):  # item generation: [{path, ...}]
        return [
            str(change.get("path", ""))
            for change in changes
            if isinstance(change, dict)
        ]
    return []
