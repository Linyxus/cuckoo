"""Shared logic for cuckoo hook monitors.

Stdlib-only: hooks run under bare ``python3``, without the cuckoo package.
Formats must stay in sync with ``src/cuckoo/journal.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def state_dir(cwd: str) -> Path:
    override = os.environ.get("CUCKOO_STATE_DIR")
    if override:
        return Path(override)
    digest = hashlib.sha256(str(Path(cwd).resolve()).encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "cuckoo" / "projects" / digest


def read_journal(directory: Path) -> tuple[list[dict], set[int]]:
    events: list[dict] = []
    delivered: set[int] = set()
    journal = directory / "journal.jsonl"
    if not journal.exists():
        return events, delivered
    for line in journal.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "event":
            events.append(record)
        elif record.get("type") == "delivered":
            delivered.update(record.get("ids", []))
    return events, delivered


def read_notified(directory: Path) -> int:
    try:
        return int((directory / "notified.cursor").read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def write_notified(directory: Path, event_id: int) -> None:
    (directory / "notified.cursor").write_text(str(event_id))


def busy_agents(directory: Path) -> list[str]:
    """Names of agents still working, per the manager's status snapshot.
    Ignored when the publishing process is no longer alive."""
    try:
        status = json.loads((directory / "status.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    pid = status.get("pid")
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return []
    return [
        entry.get("agent", "?")
        for entry in status.get("agents", [])
        if entry.get("busy")
    ]


def pending_items(cwd: str) -> tuple[list[dict], Path | None]:
    """Events not yet delivered to Claude nor surfaced by a hook."""
    directory = state_dir(cwd)
    if not directory.exists():
        return [], None
    events, delivered = read_journal(directory)
    notified = read_notified(directory)
    pending = [
        event
        for event in events
        if event.get("id", 0) not in delivered and event.get("id", 0) > notified
    ]
    return pending, directory


def summarize(pending: list[dict], limit: int = 10) -> str:
    lines = []
    for event in pending[:limit]:
        kind = event.get("kind", "?")
        agent = event.get("agent", "?")
        # Hook notifications are the delivery channel, so carry enough of
        # the message to act on; codex_status/codex_result hold full detail.
        text = str(event.get("text", "")).strip().replace("\n", " ")
        if len(text) > 500:
            text = text[:500] + "…"
        suffix = ""
        if kind == "question":
            question_id = (event.get("data") or {}).get("question_id", "?")
            suffix = f" [BLOCKED — answer with codex_answer(question_id={question_id!r})]"
        lines.append(f"- {agent} ({kind}): {text}{suffix}")
    if len(pending) > limit:
        lines.append(f"- … and {len(pending) - limit} more")
    return "\n".join(lines)


def advance(pending: list[dict], directory: Path, events_max: int) -> None:
    if pending:
        write_notified(directory, events_max)
