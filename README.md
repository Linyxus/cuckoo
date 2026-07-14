# Cuckoo

Invoke Codex subagents seamlessly in Claude Code, just like native Claude
agents — spawn OpenAI Codex agents in the background, message them mid-task,
let them message (and ask) you back, and get notified when they report in.

## Install (Claude Code plugin)

Requires: [Codex CLI](https://github.com/openai/codex) installed and
authenticated (`codex login`), `uv`, Python ≥ 3.13.

```
/plugin marketplace add linyxus/cuckoo     # or the local path to this repo
/plugin install cuckoo@cuckoo
```

The plugin provides:

- **MCP server `cuckoo`** — tools Claude uses directly, like native
  subagents: `codex_spawn`, `codex_send`, `codex_status`, `codex_result`,
  `codex_answer`, `codex_interrupt`.
- **Skill `codex-subagents`** — teaches Claude the orchestration patterns
  (single delegated task, parallel fan-out, steering, follow-up turns).
- **Hook monitors (push, never poll)** — subagent activity is delivered
  entirely by notifications; there is no wait tool. A `PostToolUse` hook
  injects new activity into Claude's context while it works on other
  things; a `Stop` hook keeps Claude from finishing when there is
  undelivered activity (and makes it hand off explicitly while agents are
  still running); a `UserPromptSubmit` hook surfaces anything that arrived
  while the session was idle. To wait for a subagent, Claude simply ends
  its turn.

## What it looks like

```
You:    Spawn a codex agent to refactor the parser while you review the tests.
Claude: codex_spawn(task="Refactor src/parser...", name="parser-refactor")
        ... continues reviewing tests ...
        [post-tool-use] parser-refactor (question): "Two entry points; unify them?"
Claude: codex_answer(q-1a2b3c, "Yes, unify behind parse()")
        ... finishes the test review, tries to stop ...
        [stop gate] 1 update from Codex subagents: parser-refactor (completed)
Claude: codex_result(parser-refactor) -> final message + files changed
```

### Bidirectional messaging

- **Main → sub:** `codex_send(agent, text)` steers a running turn mid-flight
  (`turn/steer`) or starts a follow-up turn with full context preserved.
- **Sub → main:** every Codex agent gets a `cuckoo_bridge` MCP server
  injected with two tools: `send_message` (fire-and-forget updates) and
  `ask` (blocks the agent until Claude answers via `codex_answer`).

### Configuration

Per spawn: `model` (e.g. `gpt-5.6`), `effort` (`minimal…xhigh`), `sandbox`
(`read-only | workspace-write | danger-full-access`), `cwd`, extra
`instructions`. Environment defaults for the MCP server: `CUCKOO_DEFAULT_MODEL`,
`CUCKOO_DEFAULT_EFFORT`, `CUCKOO_DEFAULT_SANDBOX`, `CUCKOO_CODEX_PATH`,
`CUCKOO_STATE_DIR`.

## Architecture

```
Claude Code ──(MCP: codex_* tools)──> cuckoo server (FastMCP)
     ▲                                   │ AgentManager ── inbox + journal
     │ hooks read the journal            │      │ unix socket (bridge relays)
     │ (Stop gate, prompt context)       ▼      ▼
     └──────────────────────── codex app-server ◄── cuckoo_bridge (MCP, injected)
                                    │ threads = subagents      ▲ send_message / ask
                                    └── Codex agent ───────────┘
```

- **`cuckoo.codex`** — the underlying library: an async client for
  `codex app-server` (JSON-RPC over stdio) with threads/turns, typed events,
  steering, interrupts, approvals, structured output. Usable standalone.
- **`cuckoo.manager`** — named agents on one client, background turns, the
  activity inbox, the bridge socket, the journal.
- **`cuckoo.server`** — the Claude-facing MCP server (`cuckoo-server`).
- **`cuckoo.bridge`** — stdlib-only MCP server injected into every Codex
  thread; attributes calls via Codex's `x-codex-turn-metadata`.
- **`hooks/`** — stdlib monitors reading the journal and the manager's
  `status.json` (PID-checked); the notified watermark guarantees each item
  is announced exactly once.

## Library quick start (without the plugin)

```python
from cuckoo.codex import CodexClient

async with await CodexClient.connect() as client:
    agent = await client.start_thread(cwd="/path/to/repo")
    result = await agent.run("Summarize this repository.")
    print(result.final_message)
```

See `examples/smoke.py` for streaming, and the docstrings in
`src/cuckoo/codex/` for the full API (events, approvals, steer/interrupt,
structured output, fork/resume).

## Development

```bash
uv sync                                  # install (Python >= 3.13)
uv run pytest                            # hermetic tests (no codex needed)
CUCKOO_LIVE=1 uv run pytest -m live      # live tests (authenticated codex CLI)
uv run python examples/smoke.py "Summarize this repo"
```

- Tested against **codex-cli 0.144.1**; `connect()` gates on `codex --version`
  (floor 0.144, `check_version=False` to bypass).
- `docs/protocol/` holds the `codex app-server generate-json-schema` snapshot
  used as field-name ground truth; regenerate when bumping codex.
- Known limits: one journal per project directory (concurrent Claude sessions
  in the same project share an inbox); unmodeled approval-type server
  requests (elicitation, user-input) are answered `-32601` like the official
  codex plugin does.
