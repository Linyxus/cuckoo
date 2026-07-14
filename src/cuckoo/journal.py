"""Append-only event journal shared between the cuckoo MCP server and the
Claude Code hook monitors.

The MCP server process appends events (subagent messages, questions,
completions) and delivery markers; the stop/prompt hooks — separate,
short-lived stdlib processes — read the journal to decide whether Claude
needs to be nudged about undelivered activity.

Layout (under :func:`project_state_dir`):
- ``journal.jsonl`` — ``{"type": "event", "id": n, ...}`` and
  ``{"type": "delivered", "ids": [...]}`` lines
- ``notified.cursor`` — highest event id already surfaced by a hook
- ``bridge.sock`` — unix socket the injected bridge servers dial

NOTE: the read side is intentionally duplicated in ``hooks/*.py`` (they must
run on a bare python3 without this package installed); keep formats in sync.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def project_state_dir(cwd: str | Path | None = None) -> Path:
    """Per-project state directory, derived from the project path so the
    MCP server and hook processes agree without coordination."""
    override = os.environ.get("CUCKOO_STATE_DIR")
    if override:
        return Path(override)
    resolved = str(Path(cwd or Path.cwd()).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "cuckoo" / "projects" / digest


class Journal:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "journal.jsonl"
        state_dir.mkdir(parents=True, exist_ok=True)

    def append_event(self, event: dict[str, Any]) -> None:
        self._append({"type": "event", **event})

    def _append(self, record: dict[str, Any]) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def read(self) -> tuple[list[dict[str, Any]], set[int]]:
        """All events plus the set of event ids marked delivered.

        ``delivered`` markers are no longer produced (hook notification is
        the sole delivery mechanism) but remain tolerated in old journals."""
        events: list[dict[str, Any]] = []
        delivered: set[int] = set()
        if not self.path.exists():
            return events, delivered
        for line in self.path.read_text().splitlines():
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

    def last_event_id(self) -> int:
        events, _ = self.read()
        return max((event.get("id", 0) for event in events), default=0)


def read_notified(state_dir: Path) -> int:
    cursor = state_dir / "notified.cursor"
    try:
        return int(cursor.read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def write_notified(state_dir: Path, event_id: int) -> None:
    (state_dir / "notified.cursor").write_text(str(event_id))
