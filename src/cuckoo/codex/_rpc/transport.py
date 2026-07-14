"""Line-oriented transports carrying NDJSON frames.

``StdioProcessTransport`` owns a spawned ``codex app-server`` process;
``MemoryTransport`` wires two in-process endpoints together for tests.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Protocol

from cuckoo.codex.config import ShutdownPolicy
from cuckoo.codex.errors import (
    ProcessExitedError,
    ProcessSpawnError,
    ProtocolDecodeError,
    TransportError,
)

logger = logging.getLogger("cuckoo.codex.transport")
stderr_logger = logging.getLogger("cuckoo.codex.stderr")

#: Pipe buffer limit — turn diffs and command output routinely exceed
#: asyncio's default 64 KiB StreamReader limit.
STREAM_LIMIT = 10 * 2**20

STDERR_TAIL_LINES = 200


class Transport(Protocol):
    """A bidirectional stream of NDJSON lines."""

    async def send_line(self, line: str) -> None:
        """Write one line (already newline-terminated)."""
        ...

    async def receive_line(self) -> str | None:
        """Read one line (without trailing newline); ``None`` on EOF."""
        ...

    async def aclose(self) -> None: ...

    def exit_error(self) -> TransportError | None:
        """After EOF: the reason the transport died, if abnormal."""
        ...


class StdioProcessTransport:
    """Transport over the stdio pipes of a spawned subprocess."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        shutdown: ShutdownPolicy | None = None,
    ) -> None:
        self._process = process
        self._shutdown = shutdown or ShutdownPolicy()
        self._stderr_tail: collections.deque[str] = collections.deque(
            maxlen=STDERR_TAIL_LINES
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="cuckoo-stderr-drain"
        )
        self._exit_error: TransportError | None = None
        self._eof = False
        self._closing = False

    @classmethod
    async def spawn(
        cls,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        shutdown: ShutdownPolicy | None = None,
    ) -> StdioProcessTransport:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            raise ProcessSpawnError(f"failed to spawn {argv[0]!r}: {exc}") from exc
        return cls(process, shutdown=shutdown)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    async def send_line(self, line: str) -> None:
        stdin = self._process.stdin
        assert stdin is not None
        try:
            stdin.write(line.encode())
            await stdin.drain()
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as exc:
            raise self._build_exit_error() from exc

    async def receive_line(self) -> str | None:
        stdout = self._process.stdout
        assert stdout is not None
        try:
            raw = await stdout.readline()
        except ValueError as exc:
            # Line exceeded the stream limit; the pipe state is unrecoverable.
            raise ProtocolDecodeError(
                f"app-server emitted a line exceeding {STREAM_LIMIT} bytes"
            ) from exc
        if not raw:
            self._eof = True
            await self._process.wait()
            if not self._closing:
                self._exit_error = self._build_exit_error()
            return None
        return raw.decode(errors="replace").rstrip("\r\n")

    def exit_error(self) -> TransportError | None:
        return self._exit_error

    async def aclose(self) -> None:
        self._closing = True
        process = self._process
        if process.returncode is None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            try:
                async with asyncio.timeout(self._shutdown.stdin_grace):
                    await process.wait()
            except TimeoutError:
                logger.debug("app-server ignored stdin EOF; sending SIGTERM")
                try:
                    process.terminate()
                    async with asyncio.timeout(self._shutdown.term_grace):
                        await process.wait()
                except TimeoutError:
                    logger.warning("app-server ignored SIGTERM; sending SIGKILL")
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
        self._stderr_task.cancel()
        try:
            await self._stderr_task
        except asyncio.CancelledError:
            pass

    def _build_exit_error(self) -> ProcessExitedError:
        code = self._process.returncode
        if code is not None and code < 0:
            detail = f"signal {-code}"
            signal_num: int | None = -code
            exit_code: int | None = None
        else:
            detail = f"exit code {code}"
            signal_num = None
            exit_code = code
        return ProcessExitedError(
            f"codex app-server exited unexpectedly ({detail})",
            exit_code=exit_code,
            signal=signal_num,
            stderr_tail=self.stderr_tail,
        )

    async def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        while True:
            try:
                raw = await stderr.readline()
            except ValueError:
                # Oversized stderr line: drop it and keep draining.
                continue
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip("\r\n")
            self._stderr_tail.append(line)
            stderr_logger.debug("%s", line)


class MemoryTransport:
    """In-process transport; ``pair()`` returns two cross-wired endpoints."""

    def __init__(
        self,
        incoming: asyncio.Queue[str | None],
        outgoing: asyncio.Queue[str | None],
    ) -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self._closed = False
        self._exit_error: TransportError | None = None

    @classmethod
    def pair(cls) -> tuple[MemoryTransport, MemoryTransport]:
        a_to_b: asyncio.Queue[str | None] = asyncio.Queue()
        b_to_a: asyncio.Queue[str | None] = asyncio.Queue()
        return cls(b_to_a, a_to_b), cls(a_to_b, b_to_a)

    async def send_line(self, line: str) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        await self._outgoing.put(line.rstrip("\n"))

    async def receive_line(self) -> str | None:
        if self._closed:
            return None
        line = await self._incoming.get()
        if line is None:
            self._closed = True
        return line

    def exit_error(self) -> TransportError | None:
        return self._exit_error

    def set_exit_error(self, error: TransportError | None) -> None:
        """Test hook: make the next EOF look like an abnormal death."""
        self._exit_error = error

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._outgoing.put(None)
            # Unblock our own pending receive_line, if any.
            await self._incoming.put(None)
