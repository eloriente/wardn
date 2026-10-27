"""Target: the only way Wardn runs commands on a machine.

Tools never execute anything themselves. They hand an argv list to a Target,
which checks it against an allowlist before anything runs.
"""

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from wardn.audit import AuditLog


class CommandNotAllowed(Exception):
    """The command is not in the allowlist."""


class CommandTimeout(Exception):
    """The command did not finish in time."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class Rule:
    """One allowed command shape.

    Each part is either a literal string (must match exactly) or a compiled
    regex (must match the whole argument). The argv length must be identical.
    """

    parts: tuple[str | re.Pattern[str], ...]

    def matches(self, argv: Sequence[str]) -> bool:
        if len(argv) != len(self.parts):
            return False
        for arg, part in zip(argv, self.parts):
            if isinstance(part, re.Pattern):
                if not part.fullmatch(arg):
                    return False
            elif arg != part:
                return False
        return True


class Allowlist:
    def __init__(self, rules: Sequence[Rule]):
        self._rules = tuple(rules)

    def check(self, argv: Sequence[str]) -> None:
        if isinstance(argv, str) or not argv:
            raise CommandNotAllowed(f"argv must be a non-empty list, got {argv!r}")
        if not any(rule.matches(argv) for rule in self._rules):
            raise CommandNotAllowed(f"command not allowed: {list(argv)!r}")


class Target(ABC):
    """Runs allowlisted commands. Subclasses only implement `_execute`."""

    def __init__(self, allowlist: Allowlist, audit: AuditLog):
        self._allowlist = allowlist
        self._audit = audit

    def run(self, argv: Sequence[str]) -> CommandResult:
        # The check and the audit live here, not in subclasses, so neither
        # can be skipped. Every attempt is recorded, including rejected ones.
        started = time.monotonic()
        shown = argv if isinstance(argv, str) else list(argv)
        try:
            self._allowlist.check(argv)
        except CommandNotAllowed as exc:
            self._record(shown, "rejected", started, reason=str(exc))
            raise
        try:
            result = self._execute(argv)
        except CommandTimeout:
            self._record(shown, "timeout", started)
            raise
        except Exception as exc:
            self._record(shown, "error", started, reason=repr(exc))
            raise
        self._record(shown, "executed", started, exit_code=result.exit_code)
        return result

    def _record(self, argv, status: str, started: float, **extra) -> None:
        self._audit.record(
            {
                "ts": datetime.now(UTC).isoformat(),
                "argv": argv,
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000),
                **extra,
            }
        )

    @abstractmethod
    def _execute(self, argv: Sequence[str]) -> CommandResult: ...


# A systemd unit name must start with an alphanumeric, so it can never be
# read as an option (e.g. "--help").
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]*\.service")

DEFAULT_RULES = (
    Rule(("df", "-h")),
    Rule(("free", "-m")),
    Rule(("systemctl", "status", UNIT, "--no-pager")),
)
