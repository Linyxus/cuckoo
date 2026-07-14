"""Client configuration objects and defaults."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Final


class _Unset(enum.Enum):
    """Sentinel distinguishing "not given" from an explicit ``None``."""

    UNSET = enum.auto()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = _Unset.UNSET

DEFAULT_REQUEST_TIMEOUT: Final[float] = 60.0

#: Lowest codex version this library is tested against.
MIN_CODEX_VERSION: Final[tuple[int, int]] = (0, 144)

#: Delta notifications suppressed by ``suppress_deltas=True`` — the exact
#: opt-out list the official Claude Code codex plugin sends.
DELTA_NOTIFICATION_METHODS: Final[tuple[str, ...]] = (
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
)


@dataclass(frozen=True, slots=True)
class ClientInfo:
    """Identity reported to the app-server during ``initialize``."""

    name: str = "cuckoo"
    title: str = "Cuckoo Codex Client"
    version: str = "0.1.0"

    def to_params(self) -> dict[str, str]:
        return {"name": self.name, "title": self.title, "version": self.version}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter for retryable RPC errors (-32001)."""

    max_attempts: int = 5
    base_delay: float = 0.25
    max_delay: float = 5.0
    jitter: float = 0.1

    def delay_for(self, attempt: int) -> float:
        """Backoff delay before retry number ``attempt`` (0-based), without jitter."""
        return min(self.base_delay * (2**attempt), self.max_delay)


@dataclass(frozen=True, slots=True)
class ShutdownPolicy:
    """Escalation timeline used when closing the app-server process."""

    stdin_grace: float = 2.0
    term_grace: float = 3.0


@dataclass(frozen=True, slots=True)
class ClientOptions:
    """Bundled options for :meth:`CodexClient.connect` (kwargs mirror this)."""

    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    shutdown: ShutdownPolicy = field(default_factory=ShutdownPolicy)
    client_info: ClientInfo = field(default_factory=ClientInfo)
    experimental_api: bool = False
    suppress_deltas: bool = False
    opt_out_notifications: tuple[str, ...] = ()
    check_version: bool = True
