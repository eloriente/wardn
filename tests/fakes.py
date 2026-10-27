from wardn.audit import MemoryAuditLog
from wardn.target import DEFAULT_RULES, Allowlist, CommandResult, Target


class ScriptedClient:
    """Plays back a fixed list of responses and records what it was sent."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append((list(messages), tools))
        return self._responses.pop(0)


class FakeTarget(Target):
    """Records what would have been executed; never runs anything."""

    def __init__(self):
        super().__init__(Allowlist(DEFAULT_RULES), MemoryAuditLog())
        self.executed = []
        self.raises = None
        self.stdout = "ok"
        self.stderr = ""
        self.exit_code = 0

    def _execute(self, argv):
        if self.raises:
            raise self.raises
        self.executed.append(list(argv))
        return CommandResult(
            stdout=self.stdout, stderr=self.stderr, exit_code=self.exit_code
        )
