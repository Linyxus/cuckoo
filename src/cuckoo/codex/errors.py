"""Exception taxonomy for the cuckoo Codex client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cuckoo.codex.turn import TurnResult


class CuckooError(Exception):
    """Base class for all errors raised by this library."""


class CodexNotFoundError(CuckooError):
    """The codex binary could not be found or is not executable."""


class CodexVersionError(CuckooError):
    """The installed codex binary is older than the tested floor."""

    def __init__(self, message: str, *, version: str | None = None) -> None:
        super().__init__(message)
        self.version = version


class TransportError(CuckooError):
    """Base class for transport-level failures."""


class ProcessSpawnError(TransportError):
    """The codex app-server process could not be spawned."""


class ProcessExitedError(TransportError):
    """The codex app-server process exited while the connection was live."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        signal: int | None = None,
        stderr_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.signal = signal
        self.stderr_tail = stderr_tail


class ConnectionClosedError(TransportError):
    """The connection was closed (cleanly or as a race with in-flight work)."""


class ProtocolDecodeError(CuckooError):
    """A line from the server could not be decoded as a JSON-RPC frame."""

    def __init__(self, message: str, *, raw_line: str = "") -> None:
        super().__init__(message)
        self.raw_line = raw_line


class RpcError(CuckooError):
    """The server answered a request with a JSON-RPC error response."""

    def __init__(
        self,
        message: str,
        *,
        code: int,
        data: Any = None,
        method: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.method = method


class ServerOverloadedError(RpcError):
    """The server kept answering -32001 (overloaded/busy) after all retries."""


class MethodNotSupportedError(RpcError):
    """The server does not implement the requested method (-32601 or
    an "unknown variant/method" style error from an older codex)."""


class RequestTimeoutError(CuckooError, TimeoutError):
    """A request did not receive a response within its timeout."""

    def __init__(self, message: str, *, method: str | None = None) -> None:
        super().__init__(message)
        self.method = method


class TurnFailedError(CuckooError):
    """A turn ended with status ``failed`` (raised by ``Thread.run`` unless
    ``raise_on_failure=False``)."""

    def __init__(self, message: str, *, result: TurnResult) -> None:
        super().__init__(message)
        self.result = result


class ThreadClosedError(CuckooError):
    """Operation on a closed thread with auto-resume disabled."""


class NoActiveTurnError(CuckooError):
    """steer() was called while no turn is running."""


class ClientClosedError(CuckooError):
    """Operation on a client that has been closed."""
