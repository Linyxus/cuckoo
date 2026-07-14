"""JSON-RPC frame types and NDJSON line encoding/decoding.

The codex app-server speaks newline-delimited JSON with bare frames — no
``"jsonrpc": "2.0"`` envelope field. Frames are classified structurally:

- ``{id, method, params?}``   → request (either direction)
- ``{method, params?}``       → notification
- ``{id, result?}``           → success response
- ``{id, error: {...}}``      → error response
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from cuckoo.codex.errors import ProtocolDecodeError

RequestId = int | str


@dataclass(frozen=True, slots=True)
class RpcRequest:
    id: RequestId
    method: str
    params: Any = None


@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: Any = None


@dataclass(frozen=True, slots=True)
class RpcResponse:
    id: RequestId
    result: Any = None


@dataclass(frozen=True, slots=True)
class RpcErrorResponse:
    id: RequestId | None
    code: int
    message: str
    data: Any = None


Frame = RpcRequest | RpcNotification | RpcResponse | RpcErrorResponse


def decode_line(line: str) -> Frame:
    """Decode one NDJSON line into a frame.

    Raises :class:`ProtocolDecodeError` for anything that is not a
    recognizable JSON-RPC frame.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolDecodeError(f"invalid JSON: {exc}", raw_line=line) from exc
    if not isinstance(data, dict):
        raise ProtocolDecodeError("JSON-RPC frame is not an object", raw_line=line)

    frame_id = data.get("id")
    method = data.get("method")

    if method is not None:
        if not isinstance(method, str):
            raise ProtocolDecodeError("frame method is not a string", raw_line=line)
        if frame_id is not None:
            return RpcRequest(id=frame_id, method=method, params=data.get("params"))
        return RpcNotification(method=method, params=data.get("params"))

    error = data.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise ProtocolDecodeError("frame error is not an object", raw_line=line)
        return RpcErrorResponse(
            id=frame_id,
            code=error.get("code", 0),
            message=error.get("message", ""),
            data=error.get("data"),
        )

    if frame_id is not None:
        return RpcResponse(id=frame_id, result=data.get("result"))

    raise ProtocolDecodeError("unrecognizable JSON-RPC frame", raw_line=line)


def encode(frame: Frame) -> str:
    """Encode a frame as one compact NDJSON line (including the newline)."""
    message: dict[str, Any]
    match frame:
        case RpcRequest(id=id_, method=method, params=params):
            message = {"id": id_, "method": method}
            if params is not None:
                message["params"] = params
        case RpcNotification(method=method, params=params):
            message = {"method": method}
            if params is not None:
                message["params"] = params
        case RpcResponse(id=id_, result=result):
            message = {"id": id_, "result": result}
        case RpcErrorResponse(id=id_, code=code, message=msg, data=data):
            error: dict[str, Any] = {"code": code, "message": msg}
            if data is not None:
                error["data"] = data
            message = {"id": id_, "error": error}
        case _:  # pragma: no cover - exhaustive over Frame
            raise TypeError(f"not a frame: {frame!r}")
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
