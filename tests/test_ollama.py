import json

import httpx
import pytest

from wardn.ollama import OllamaClient, OllamaError, ToolCall
from wardn.tools import DISK_USAGE


def make_client(handler, **kwargs):
    http = httpx.Client(
        base_url="http://ollama.test", transport=httpx.MockTransport(handler)
    )
    return OllamaClient("test-model", http=http, **kwargs)


def reply(message, **extra):
    return httpx.Response(200, json={"message": message, **extra})


def test_plain_answer():
    client = make_client(
        lambda request: reply(
            {"role": "assistant", "content": "hi"},
            eval_count=6,
            total_duration=1_286_000_000,
        )
    )
    response = client.chat([{"role": "user", "content": "hello"}])
    assert response.content == "hi"
    assert response.tool_calls == []
    assert response.eval_count == 6
    assert response.total_ms == 1286


def test_tool_call_is_parsed():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "disk_usage", "arguments": {}}}],
    }
    client = make_client(lambda request: reply(message))
    response = client.chat([{"role": "user", "content": "disks?"}])
    assert response.tool_calls == [ToolCall("disk_usage", {})]


def test_request_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return reply({"role": "assistant", "content": "ok"})

    client = make_client(handler, options={"num_ctx": 4096})
    client.chat([{"role": "user", "content": "x"}], tools=[DISK_USAGE.schema()])

    assert seen["url"] == "http://ollama.test/api/chat"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is False
    assert seen["body"]["options"] == {"num_ctx": 4096}
    assert seen["body"]["tools"][0]["function"]["name"] == "disk_usage"


def test_no_tools_key_when_none_given():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return reply({"role": "assistant", "content": "ok"})

    make_client(handler).chat([{"role": "user", "content": "x"}])
    assert "tools" not in seen["body"]


def test_leaked_reasoning_is_stripped():
    content = "Okay, let me think...\n</think>\n\nHi how are you doing?"
    client = make_client(lambda r: reply({"role": "assistant", "content": content}))
    assert client.chat([]).content == "Hi how are you doing?"


def test_as_message_round_trips_tool_calls():
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "disk_usage", "arguments": {}}}],
    }
    response = make_client(lambda r: reply(message)).chat([])
    assert response.as_message() == message


def test_http_error_becomes_ollama_error():
    client = make_client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(OllamaError):
        client.chat([])


def test_connection_error_becomes_ollama_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaError):
        make_client(handler).chat([])


@pytest.mark.parametrize(
    "body",
    [
        {},  # no message
        {"message": {"tool_calls": [{"function": {"arguments": {}}}]}},  # no name
        {
            "message": {
                "tool_calls": [
                    {"function": {"name": "x", "arguments": "not-an-object"}}
                ]
            }
        },
    ],
)
def test_malformed_response_becomes_ollama_error(body):
    client = make_client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(OllamaError):
        client.chat([])
