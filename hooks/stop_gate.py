#!/usr/bin/env python3
"""Stop hook: don't let Claude finish while Codex subagents have news
(or are still working).

Blocks in two cases:
- undelivered subagent activity exists (messages, blocking questions,
  results) — a notified watermark makes each item nag at most once;
- agents are still busy and this is the first stop attempt
  (``stop_hook_active`` guards against loops): Claude must explicitly hand
  off to the user — results are pushed to it when they arrive.
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
    if directory is None:
        directory = _common.state_dir(cwd)
        if not directory.exists():
            return

    if pending:
        events, _ = _common.read_journal(directory)
        max_id = max((event.get("id", 0) for event in events), default=0)
        _common.advance(pending, directory, max_id)
        questions = sum(1 for event in pending if event.get("kind") == "question")
        headline = f"{len(pending)} update(s) from Codex subagents"
        if questions:
            headline += f" — {questions} BLOCKED on a question"
        reason = (
            f"[cuckoo] {headline}:\n"
            f"{_common.summarize(pending)}\n\n"
            "Handle these before finishing: codex_answer any blocking "
            "questions, codex_result(agent) for full final outputs, and "
            "report results to the user. If nothing needs action, briefly "
            "tell the user what the subagents said and finish."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
        return

    busy = _common.busy_agents(directory)
    if busy and not payload.get("stop_hook_active"):
        names = ", ".join(busy)
        reason = (
            f"[cuckoo] Codex subagent(s) still working: {names}. "
            "Do NOT wait or poll for them — tell the user these agents are "
            "still running and will report back (their results are pushed "
            "to you via notifications), then finish your turn."
        )
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
