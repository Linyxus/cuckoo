# Cuckoo

Invoke Codex subagents seamlessly in Claude Code, just like native Claude agents.

This repository currently contains the first component: **`cuckoo.codex`**, an
async Python library for programmatically spawning, messaging, monitoring, and
managing Codex agents. Planned next: an MCP server exposing these operations to
Claude Code, and a monitor surfacing Codex notifications back into the session.

## How it works

The library drives **`codex app-server`** — the JSON-RPC 2.0-over-stdio
protocol behind OpenAI's own Codex IDE/desktop clients. One long-lived process
hosts many *threads* (subagents); each user request is a *turn* that streams
typed events (commands, file changes, reasoning, messages) until completion.
This is the only Codex surface with mid-turn steering, graceful interrupts,
and live approval callbacks.

```
Layer 2  client.py / thread.py / turn.py     CodexClient, Thread (the agent), Turn
         approvals.py / state.py / _events   approval chain, lifecycle FSM, EventBus
Layer 1  protocol/                           typed events, items, models (pydantic, lenient)
Layer 0  _rpc/                               JSON-RPC codec, stdio transport, correlation
```

## Quick start

```python
import asyncio
from cuckoo.codex import CodexClient

async def main():
    async with await CodexClient.connect() as client:
        agent = await client.start_thread(cwd="/path/to/repo")
        result = await agent.run("Summarize this repository.")
        print(result.final_message)

asyncio.run(main())
```

### Streaming, steering, interrupting

```python
turn = await agent.start_turn("Refactor the auth module")

async for event in turn.events():        # typed: ItemCompleted, TurnCompleted, ...
    print(type(event).__name__)

await agent.steer("Skip the tests for now")   # inject input mid-turn
await agent.interrupt()                       # graceful turn/interrupt
result = await turn                           # Turn is awaitable
```

### Monitoring a fleet

```python
from cuckoo.codex import AgentStateChanged, ApprovalRequested

client.subscribe(print, kinds=(AgentStateChanged, ApprovalRequested))
await agent.wait_for(AgentState.IDLE)
```

Every thread has a client-side lifecycle state machine
(`IDLE → RUNNING → AWAITING_APPROVAL → ... → CLOSED`); transitions are emitted
as `AgentStateChanged` events — the hook the future MCP server and monitor
build on. Threads unloaded by the server (~30 min idle) auto-resume on the
next turn.

### Approvals

```python
from cuckoo.codex import ApprovalPolicy, RuleBasedApprovals, Decision

agent = await client.start_thread(
    cwd=repo,
    sandbox="workspace-write",
    approval_policy=ApprovalPolicy.ON_REQUEST,
    approval_handler=RuleBasedApprovals(
        allow_commands=[r"^git (status|diff)", r"^pytest\b"],
        deny_commands=[r"rm\s+-rf"],
        allow_write_paths=[repo / "src"],
        default=None,                # abstain -> fall through to client handler
    ),
)
```

Handlers are chained turn → thread → client → deny-by-default, may be sync or
async, and cover both approval generations (`execCommandApproval` and
`item/*/requestApproval`). Defaults mirror the official plugin: sandboxed
read-only, `approval_policy="never"`.

### Structured output

```python
from pydantic import BaseModel

class Verdict(BaseModel):
    ok: bool
    reasons: list[str]

result = await agent.run("Is this PR safe to merge?", output_schema=Verdict)
verdict = result.output_as(Verdict)
```

### Escape hatches

The app-server protocol is experimental and evolves; unknown events and item
types degrade to `UnknownEvent`/`UnknownItem` (never a crash), every model
keeps the raw payload, and any un-modeled method is one call away:

```python
await client.request("thread/inject_items", {...})
```

## Development

```bash
uv sync                                  # install (Python >= 3.13)
uv run pytest                            # hermetic tests (fake app-server, no codex needed)
CUCKOO_LIVE=1 uv run pytest -m live      # 3 live tests (needs authenticated codex CLI)
uv run python examples/smoke.py "Summarize this repo"
```

- Tested against **codex-cli 0.144.1**; `connect()` gates on `codex --version`
  (floor 0.144, `check_version=False` to bypass).
- `docs/protocol/` holds the `codex app-server generate-json-schema` snapshot
  used as field-name ground truth; regenerate it when bumping the codex
  version.
- `tests/fixtures/live_turn_events.jsonl` is a captured real session replayed
  by the protocol tests.
