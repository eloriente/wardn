"""The agent loop: ask the model, run the tools it requests, repeat."""

from dataclasses import dataclass
from typing import Any, Protocol

from wardn.ollama import ChatResponse
from wardn.target import Target
from wardn.tools import Tool

SYSTEM_PROMPT = (
    "You are Wardn, an assistant that watches over a homelab server. "
    "Use the tools to inspect the server. "
    "If you need more information, call a tool right away; never say you will do it later. "
    "Answer briefly and only from what the tools returned."
)

# Backstop: tools already keep their output short, but never trust that.
MAX_TOOL_OUTPUT_CHARS = 2000


class ChatClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse: ...


@dataclass(frozen=True)
class AgentResult:
    answer: str
    messages: list[dict[str, Any]]  # the whole conversation, for inspection
    iterations: int
    finished: bool  # False if it stopped at the iteration limit


class Agent:
    def __init__(
        self,
        client: ChatClient,
        target: Target,
        tools: tuple[Tool, ...],
        max_iterations: int = 5,
    ):
        self._client = client
        self._target = target
        self._tools = {tool.name: tool for tool in tools}
        self._schemas = [tool.schema() for tool in tools]
        self._max_iterations = max_iterations

    def run(self, question: str) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        for iteration in range(1, self._max_iterations + 1):
            response = self._client.chat(messages, tools=self._schemas)
            messages.append(response.as_message())

            if not response.tool_calls:
                return AgentResult(response.content, messages, iteration, True)

            for call in response.tool_calls:
                output = self._run_tool(call.name, call.arguments)
                messages.append(
                    {"role": "tool", "tool_name": call.name, "content": output}
                )

        answer = f"I could not finish within {self._max_iterations} steps."
        return AgentResult(answer, messages, self._max_iterations, False)

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            # Text, not an exception: the model can read it and try again.
            return f"error: unknown tool {name!r}. Available: {sorted(self._tools)}"
        output = tool.call(self._target, arguments)
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            output = output[:MAX_TOOL_OUTPUT_CHARS] + "\n... (truncated)"
        return output
