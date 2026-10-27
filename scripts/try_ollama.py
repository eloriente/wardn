"""Ask the model a question and see whether it calls disk_usage.

    PYTHONPATH=src uv run python scripts/try_ollama.py "How full are my disks?"

Set WARDN_MODEL to try another model (default: qwen3:4b-instruct).
It only shows the model's decision; it does not run the tool yet.
"""

import os
import sys

from wardn.ollama import OllamaClient
from wardn.tools import ALL_TOOLS

question = " ".join(sys.argv[1:]) or "How full are my disks?"
model = os.environ.get("WARDN_MODEL", "qwen3:4b-instruct")

client = OllamaClient(model)
response = client.chat(
    [{"role": "user", "content": question}],
    tools=[tool.schema() for tool in ALL_TOOLS],
)

print(f"model:      {model}")
print(f"question:   {question}")
print(f"tool calls: {response.tool_calls or 'none'}")
print(f"content:    {response.content or '(empty)'}")
print(f"tokens:     {response.eval_count} in {response.total_ms} ms")
