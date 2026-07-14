#!/usr/bin/env python3
"""PostToolUse hook: push new Codex subagent activity into Claude's context
while it works on other things — event-driven delivery, no polling.

Fires after every tool call; silent unless subagents produced something new
since the last delivery/notification.
"""

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

    questions = sum(1 for event in pending if event.get("kind") == "question")
    headline = f"[cuckoo] {len(pending)} new update(s) from Codex subagents"
    if questions:
        headline += f" — {questions} BLOCKED on a question (answer with codex_answer NOW)"
    context = (
        f"{headline}:\n{_common.summarize(pending)}\n"
        "Full detail: codex_status (recent activity + questions) or "
        "codex_result(agent) for a completed turn's full output."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
