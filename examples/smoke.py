"""Manual smoke test: run one Codex subagent turn and stream its progress.

Usage:
    uv run python examples/smoke.py "Summarize this repo"
    uv run python examples/smoke.py --write "Add a docstring to main.py"
"""

import argparse
import asyncio
import sys
from pathlib import Path

from cuckoo.codex import (
    AgentMessageDelta,
    AgentMessageItem,
    AgentStateChanged,
    CodexClient,
    CommandExecutionItem,
    FileChangeItem,
    ItemCompleted,
    ItemStarted,
    ReasoningItem,
    SandboxMode,
    TurnCompleted,
)


def describe(event) -> str | None:
    match event:
        case AgentStateChanged():
            return f"[state] {event.old_state} -> {event.new_state} ({event.cause})"
        case ItemStarted(item=CommandExecutionItem() as item):
            return f"[cmd ] $ {item.command}"
        case ItemCompleted(item=CommandExecutionItem() as item):
            return f"[cmd ] exit {item.exit_code}"
        case ItemCompleted(item=ReasoningItem() as item):
            return f"[think] {item.text[:120]}"
        case ItemCompleted(item=FileChangeItem() as item):
            paths = ", ".join(c.path for c in item.changes)
            return f"[edit] {paths}"
        case ItemCompleted(item=AgentMessageItem() as item) if not item.is_final_answer:
            return f"[msg ] {item.text[:120]}"
        case TurnCompleted():
            return f"[turn] {event.turn.status}"
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="+")
    parser.add_argument("--write", action="store_true", help="workspace-write sandbox")
    parser.add_argument("--model", default=None)
    parser.add_argument("--stream-text", action="store_true", help="stream answer deltas")
    args = parser.parse_args()
    prompt = " ".join(args.prompt)

    async with await CodexClient.connect(cwd=Path.cwd()) as client:
        print(f"connected: {client.server_info.user_agent}", file=sys.stderr)
        agent = await client.start_thread(
            cwd=Path.cwd(),
            model=args.model,
            sandbox=SandboxMode.WORKSPACE_WRITE if args.write else SandboxMode.READ_ONLY,
            ephemeral=True,
        )

        def on_event(event) -> None:
            line = describe(event)
            if line:
                print(line, file=sys.stderr)
            if args.stream_text and isinstance(event, AgentMessageDelta):
                print(event.delta, end="", flush=True)

        agent.subscribe(on_event)

        result = await agent.run(prompt, timeout=600)
        if args.stream_text:
            print()
        else:
            print(result.final_message or "(no final message)")
        if result.token_usage:
            print(
                f"[usage] {result.token_usage.total_tokens} tokens "
                f"({result.token_usage.output_tokens} out)",
                file=sys.stderr,
            )


if __name__ == "__main__":
    asyncio.run(main())
