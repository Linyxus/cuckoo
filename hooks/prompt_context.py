#!/usr/bin/env python3
"""UserPromptSubmit hook: surface pending Codex subagent activity as context
when the user sends a new prompt."""

import json
import sys

import _common


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    cwd = payload.get("cwd") or "."
    pending, directory = _common.pending_items(cwd)
    if not pending or directory is None:
        return

    events, _ = _common.read_journal(directory)
    max_id = max((event.get("id", 0) for event in events), default=0)
    _common.advance(pending, directory, max_id)

    context = (
        f"[cuckoo] Codex subagents produced {len(pending)} update(s) since "
        "you last checked:\n"
        f"{_common.summarize(pending)}\n"
        "codex_answer any blocking questions; codex_status/codex_result "
        "give full detail when relevant to the user's request."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
