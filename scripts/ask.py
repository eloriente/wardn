"""Ask Wardn a question about the dev sandbox, end to end.

    docker compose -f compose.dev.yaml up -d
    WARDN_SSH_KEY=~/.ssh/wardn_sandbox PYTHONPATH=src \
        uv run python scripts/ask.py "How full are my disks?"

Set WARDN_MODEL to try another model (default: qwen3:4b-instruct).
Every command attempt is appended to logs/audit.jsonl.
"""

import os
import sys
from pathlib import Path

from wardn.agent import Agent
from wardn.audit import JsonlAuditLog
from wardn.ollama import OllamaClient
from wardn.ssh_target import SSHTarget
from wardn.target import DEFAULT_RULES, Allowlist
from wardn.tools import ALL_TOOLS

question = " ".join(sys.argv[1:]) or "How full are my disks?"
model = os.environ.get("WARDN_MODEL", "qwen3:4b-instruct")
key = os.environ.get("WARDN_SSH_KEY")
if not key:
    sys.exit("Set WARDN_SSH_KEY to the sandbox private key.")

target = SSHTarget(
    Allowlist(DEFAULT_RULES),
    JsonlAuditLog(Path("logs/audit.jsonl")),
    host="127.0.0.1",
    user="wardn",
    key_path=Path(key).expanduser(),
    port=2222,
)
agent = Agent(OllamaClient(model), target, ALL_TOOLS)
result = agent.run(question)

print(f"model: {model}\n")
for message in result.messages[1:]:  # skip the system prompt
    for tool_call in message.get("tool_calls", []):
        function = tool_call["function"]
        print(f"[model asks] {function['name']}({function['arguments']})")
    if message["role"] == "tool":
        print(f"[tool result]\n{message['content']}")
    elif message["role"] == "user":
        print(f"[question] {message['content']}")
print(f"\n[answer] {result.answer}")
print(f"({result.iterations} model calls, finished={result.finished})")
