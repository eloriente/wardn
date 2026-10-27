"""Audit log: one record per attempt to run a command, allowed or not."""

import json
from pathlib import Path
from typing import Any, Protocol

Event = dict[str, Any]


class AuditLog(Protocol):
    def record(self, event: Event) -> None: ...


class MemoryAuditLog:
    """Keeps events in a list. For tests and evals."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        self.events.append(event)


class JsonlAuditLog:
    """Appends one JSON object per line to a file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, event: Event) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
