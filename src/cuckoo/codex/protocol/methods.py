"""Client→server method names (verified against the 0.144.1 schema snapshot)
and small helpers for building their params.

Only the surface wrapped by Layer 2 is listed; anything else remains
reachable through ``CodexClient.request(method, params)``.
"""

from __future__ import annotations

from typing import Any

INITIALIZE = "initialize"
INITIALIZED = "initialized"  # notification

THREAD_START = "thread/start"
THREAD_RESUME = "thread/resume"
THREAD_FORK = "thread/fork"
THREAD_READ = "thread/read"
THREAD_LIST = "thread/list"
THREAD_ARCHIVE = "thread/archive"
THREAD_UNARCHIVE = "thread/unarchive"
THREAD_DELETE = "thread/delete"
THREAD_NAME_SET = "thread/name/set"
THREAD_GOAL_SET = "thread/goal/set"
THREAD_GOAL_GET = "thread/goal/get"
THREAD_GOAL_CLEAR = "thread/goal/clear"
THREAD_ROLLBACK = "thread/rollback"
THREAD_COMPACT_START = "thread/compact/start"
THREAD_LOADED_LIST = "thread/loaded/list"
THREAD_UNSUBSCRIBE = "thread/unsubscribe"

TURN_START = "turn/start"
TURN_STEER = "turn/steer"
TURN_INTERRUPT = "turn/interrupt"

REVIEW_START = "review/start"
MODEL_LIST = "model/list"
CONFIG_READ = "config/read"
ACCOUNT_READ = "account/read"
GET_AUTH_STATUS = "getAuthStatus"  # legacy method; still served in 0.144.1
MCP_SERVER_STATUS_LIST = "mcpServerStatus/list"


def build_params(**kwargs: Any) -> dict[str, Any]:
    """Assemble a params dict, dropping ``None`` values (the protocol treats
    absent and null the same, and omitting keeps us compatible with older
    servers that reject unknown fields)."""
    return {key: value for key, value in kwargs.items() if value is not None}
