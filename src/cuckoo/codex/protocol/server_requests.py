"""Normalization of server→client approval requests.

Two generations coexist on the wire:

- legacy: ``execCommandApproval`` / ``applyPatchApproval`` (thread id under
  ``conversationId``; decisions ``approved``/``denied``/``abort``)
- current: ``item/commandExecution/requestApproval`` /
  ``item/fileChange/requestApproval`` (decisions ``accept``/``decline``/``cancel``)

Both are normalized into one :class:`ApprovalRequest` hierarchy that knows
how to serialize a :class:`Decision` back into the right wording.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from .common import Decision

EXEC_COMMAND_APPROVAL = "execCommandApproval"
APPLY_PATCH_APPROVAL = "applyPatchApproval"
COMMAND_EXECUTION_REQUEST_APPROVAL = "item/commandExecution/requestApproval"
FILE_CHANGE_REQUEST_APPROVAL = "item/fileChange/requestApproval"

_LEGACY_DECISIONS = {
    Decision.APPROVED: "approved",
    Decision.APPROVED_FOR_SESSION: "approved_for_session",
    Decision.DENIED: "denied",
    Decision.ABORT: "abort",
}
_ITEM_DECISIONS = {
    Decision.APPROVED: "accept",
    Decision.APPROVED_FOR_SESSION: "acceptForSession",
    Decision.DENIED: "decline",
    Decision.ABORT: "cancel",
}


class ApprovalRequest(BaseModel):
    """Base for normalized approval requests."""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, alias_generator=to_camel
    )

    #: Wire method this request arrived as; set by :func:`parse_approval_request`.
    method: str = Field(default="", exclude=True)

    thread_id: str | None = Field(
        default=None, validation_alias=AliasChoices("threadId", "conversationId")
    )
    turn_id: str | None = None
    item_id: str | None = None
    call_id: str | None = None  # legacy correlation id
    approval_id: str | None = None
    reason: str | None = None

    @property
    def is_legacy(self) -> bool:
        return self.method in (EXEC_COMMAND_APPROVAL, APPLY_PATCH_APPROVAL)

    def build_response(self, decision: Decision) -> dict[str, Any]:
        """Serialize a decision as this request's JSON-RPC response result."""
        wording = _LEGACY_DECISIONS if self.is_legacy else _ITEM_DECISIONS
        return {"decision": wording[decision]}


class CommandApproval(ApprovalRequest):
    """The agent wants to run a command outside its sandbox allowance."""

    command: str | list[str] | None = None
    cwd: str | None = None

    @property
    def command_text(self) -> str:
        if isinstance(self.command, list):
            return " ".join(self.command)
        return self.command or ""


class FileChangeApproval(ApprovalRequest):
    """The agent wants to apply file changes that need explicit approval."""

    changes: Any = Field(
        default=None, validation_alias=AliasChoices("fileChanges", "changes")
    )
    grant_root: str | None = None


_REQUEST_TYPES: dict[str, type[ApprovalRequest]] = {
    EXEC_COMMAND_APPROVAL: CommandApproval,
    COMMAND_EXECUTION_REQUEST_APPROVAL: CommandApproval,
    APPLY_PATCH_APPROVAL: FileChangeApproval,
    FILE_CHANGE_REQUEST_APPROVAL: FileChangeApproval,
}

#: Methods this library can answer with a plain :class:`Decision`.
APPROVAL_METHODS: frozenset[str] = frozenset(_REQUEST_TYPES)


def parse_approval_request(method: str, params: Any) -> ApprovalRequest | None:
    """Normalize a server request; ``None`` if the method is not an approval
    this library models (caller should answer -32601, like the official
    plugin does for every server request)."""
    request_type = _REQUEST_TYPES.get(method)
    if request_type is None:
        return None
    try:
        request = request_type.model_validate(params or {})
    except ValidationError:
        request = request_type()
    request.method = method
    return request
