from wardn.audit import MemoryAuditLog
from wardn.target import DEFAULT_RULES, Allowlist, CommandResult, Target


class FakeTarget(Target):
    """Records what would have been executed; never runs anything."""

    def __init__(self):
        self.audit = MemoryAuditLog()
        super().__init__(Allowlist(DEFAULT_RULES), self.audit)
        self.executed = []
        self.raises = None

    def _execute(self, argv):
        if self.raises:
            raise self.raises
        self.executed.append(list(argv))
        return CommandResult(stdout="ok", stderr="", exit_code=0)
