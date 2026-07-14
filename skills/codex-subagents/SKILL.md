---
name: codex-subagents
description: Orchestrate OpenAI Codex subagents (parallel background coding agents) via the cuckoo MCP tools — when the user wants to delegate work to Codex, run several coding agents in parallel, message or steer a running Codex agent, answer a Codex agent's question, or check on Codex agent status.
---

# Codex subagents (cuckoo)

The `cuckoo` MCP server manages OpenAI Codex agents as background subagents.
They behave like native subagents: spawn them and their activity is **pushed
to you as notifications** — after your tool calls while you work, when you
try to stop, and on the next user prompt. There is no wait tool and nothing
to poll. The agents cannot see this conversation, so every task string must
be self-contained.

## Tools

| Tool | Purpose |
|---|---|
| `codex_spawn(task, name?, cwd?, model?, effort?, sandbox?, instructions?)` | Start an agent in the background; returns its name immediately |
| `codex_send(agent, message, mode?)` | Message an agent: `auto` steers a running turn or starts a new one |
| `codex_answer(question_id, answer)` | Unblock an agent waiting on a question |
| `codex_status()` | Instant snapshot: agents' state, unanswered questions, recent activity in full |
| `codex_result(agent)` | Full result of the last completed turn |
| `codex_interrupt(agent)` | Gracefully stop the running turn |

## Notification kinds

- `message` — the subagent reported progress/findings (keep working).
- `question` — the subagent is **frozen** until you `codex_answer`. Handle first.
- `completed` — a turn finished; get the full output with `codex_result`.
- `error` — the turn or the codex process failed.

## How to wait: don't

Waiting for a subagent means **finishing your turn**. Notifications bring
you back when there is something to act on:

- **You have other work?** Do it — new subagent activity is injected into
  your context automatically after your tool calls.
- **Nothing left but the Codex work?** Tell the user what's running and end
  your turn. Results are announced when they arrive (at stop time or on the
  next user message).
- **Never** call `codex_status` in a loop to check on agents. It's an
  instant detail lookup for after you've been notified, not a wait
  primitive.

When a notification mentions a blocking question, `codex_answer` it
immediately — the subagent is frozen until you do.

## Patterns

**Single delegated task:** `codex_spawn`, report to the user that it's
running, finish your turn. On the completion notification, `codex_result`
and relay the final message verbatim plus files changed and tokens.

**Parallel fan-out:** spawn several agents with distinct names, keep doing
your own work, and handle notifications as they arrive.

**Steer mid-task:** `codex_send(agent, "Also cover the edge case where ...")`
injects into the running turn. When idle, the same call starts a new turn
with the agent's full context preserved.

**Configuration:** `model` (e.g. `gpt-5.6`), `effort`
(`minimal|low|medium|high|xhigh`), `sandbox`
(`read-only|workspace-write|danger-full-access`) per spawn. Environment
defaults: `CUCKOO_DEFAULT_MODEL`, `CUCKOO_DEFAULT_EFFORT`,
`CUCKOO_DEFAULT_SANDBOX`, `CUCKOO_CODEX_PATH`.
