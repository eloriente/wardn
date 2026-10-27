"""Audit log: one record per attempt to run a command, allowed or not.

It also notes what was hidden from the model (counts only, never the values).
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

Event = dict[str, Any]


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class AuditLog(Protocol):
    def record(self, event: Event) -> None: ...


class MemoryAuditLog:
    """Keeps events in a list. For tests and evals."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        self.events.append(event)


class JsonlAuditLog:
    """Appends one JSON object per line to a file only its owner can read."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, event: Event) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
