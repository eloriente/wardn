"""Minimal client for Ollama's /api/chat endpoint, with tool calling."""

from dataclasses import dataclass, field
from typing import Any

import httpx


class OllamaError(Exception):
    """Ollama is unreachable, or answered with something we cannot use."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    eval_count: int = 0  # tokens generated
    total_ms: int = 0

    def as_message(self) -> dict[str, Any]:
        """This answer in the shape /api/chat expects it in the history."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return message


def _clean_content(content: str) -> str:
    # Some Qwen3 variants leak their reasoning into `content`, ending it with
    # a stray </think>. Keep only what comes after the last one.
    return content.rsplit("</think>", 1)[-1].strip()


class OllamaClient:
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        options: dict[str, Any] | None = None,
        http: httpx.Client | None = None,
    ):
        self._model = model
        self._options = options
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if self._options:
            payload["options"] = self._options

        try:
            response = self._http.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaError(f"request to Ollama failed: {exc}") from exc

        return self._parse(data)

    @staticmethod
    def _parse(data: dict[str, Any]) -> ChatResponse:
        try:
            message = data["message"]
            calls = []
            for raw in message.get("tool_calls") or []:
                function = raw["function"]
                arguments = function.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise TypeError(f"tool arguments are not an object: {arguments!r}")
                calls.append(ToolCall(function["name"], arguments))
            return ChatResponse(
                content=_clean_content(message.get("content") or ""),
                tool_calls=calls,
                eval_count=data.get("eval_count", 0),
                total_ms=data.get("total_duration", 0) // 1_000_000,
            )
        except (KeyError, TypeError) as exc:
            raise OllamaError(f"unexpected response from Ollama: {exc!r}") from exc
