import pytest

from cuckoo.codex._rpc.codec import (
    RpcErrorResponse,
    RpcNotification,
    RpcRequest,
    RpcResponse,
    decode_line,
    encode,
)
from cuckoo.codex.errors import ProtocolDecodeError


def test_decode_request():
    frame = decode_line('{"id": 3, "method": "thread/start", "params": {"cwd": "/x"}}')
    assert frame == RpcRequest(id=3, method="thread/start", params={"cwd": "/x"})


def test_decode_notification():
    frame = decode_line('{"method": "turn/started", "params": {"threadId": "t1"}}')
    assert frame == RpcNotification(method="turn/started", params={"threadId": "t1"})


def test_decode_response():
    assert decode_line('{"id": 1, "result": {"ok": true}}') == RpcResponse(
        id=1, result={"ok": True}
    )
    # result may be omitted entirely
    assert decode_line('{"id": 2}') == RpcResponse(id=2, result=None)


def test_decode_error_response():
    frame = decode_line('{"id": 1, "error": {"code": -32001, "message": "busy"}}')
    assert frame == RpcErrorResponse(id=1, code=-32001, message="busy", data=None)


def test_decode_tolerates_jsonrpc_envelope_field():
    frame = decode_line('{"jsonrpc": "2.0", "id": 5, "method": "m"}')
    assert frame == RpcRequest(id=5, method="m", params=None)


@pytest.mark.parametrize(
    "line",
    ["not json", "[1,2]", '"str"', "{}", '{"params": {}}'],
)
def test_decode_rejects_garbage(line):
    with pytest.raises(ProtocolDecodeError):
        decode_line(line)


@pytest.mark.parametrize(
    "frame",
    [
        RpcRequest(id=1, method="a", params={"x": 1}),
        RpcRequest(id="s-1", method="a"),
        RpcNotification(method="n", params=[1, 2]),
        RpcNotification(method="n"),
        RpcResponse(id=2, result={"y": "z"}),
        RpcErrorResponse(id=3, code=-32601, message="nope", data={"d": 1}),
    ],
)
def test_encode_decode_roundtrip(frame):
    line = encode(frame)
    assert line.endswith("\n")
    assert "\n" not in line[:-1]
    assert decode_line(line) == frame


def test_encode_response_keeps_null_result():
    # A response must carry an explicit result key even when None,
    # otherwise it would decode as an empty (invalid) frame.
    assert '"result":null' in encode(RpcResponse(id=1, result=None)) or decode_line(
        encode(RpcResponse(id=1, result=None))
    ) == RpcResponse(id=1, result=None)
