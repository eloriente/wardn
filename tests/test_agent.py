import pytest
from fakes import FakeTarget

from wardn.agent import MAX_TOOL_OUTPUT_CHARS, SYSTEM_PROMPT, Agent
from wardn.ollama import ChatResponse, ToolCall
from wardn.tools import ALL_TOOLS, DISK_USAGE, Tool

DF = "Filesystem Size Used Avail Use% Mounted on\noverlay 59G 28G 28G 50% /\n"


class ScriptedClient:
    """Plays back a fixed list of responses and records what it was sent."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append((list(messages), tools))
        return self._responses.pop(0)


def call(name="disk_usage", **arguments):
    return ChatResponse(content="", tool_calls=[ToolCall(name, arguments)])


def answer(text):
    return ChatResponse(content=text)


@pytest.fixture
def target():
    target = FakeTarget()
    target.stdout = DF
    return target


def test_answers_without_tools(target):
    client = ScriptedClient(answer("All good."))
    result = Agent(client, target, ALL_TOOLS).run("hello")

    assert result.answer == "All good."
    assert result.finished
    assert result.iterations == 1
    assert target.executed == []


def test_conversation_starts_with_system_prompt_and_question(target):
    client = ScriptedClient(answer("ok"))
    Agent(client, target, ALL_TOOLS).run("how are the disks?")

    messages, tools = client.requests[0]
    assert messages == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "how are the disks?"},
    ]
    assert tools[0]["function"]["name"] == "disk_usage"


def test_tool_result_goes_back_to_the_model(target):
    client = ScriptedClient(call(), answer("The root disk is 50% full."))
    result = Agent(client, target, ALL_TOOLS).run("disks?")

    assert result.answer == "The root disk is 50% full."
    assert result.iterations == 2
    assert target.executed == [["df", "-h"]]
    second_request, _ = client.requests[1]
    assert second_request[-1] == {
        "role": "tool",
        "tool_name": "disk_usage",
        "content": "/: 50% used (28G of 59G, 28G free)",
    }


def test_unknown_tool_is_reported_to_the_model(target):
    client = ScriptedClient(call("format_disk"), answer("Sorry."))
    result = Agent(client, target, ALL_TOOLS).run("do something bad")

    assert result.answer == "Sorry."
    assert target.executed == []
    tool_message = client.requests[1][0][-1]
    assert "unknown tool 'format_disk'" in tool_message["content"]


def test_invalid_arguments_are_reported_to_the_model(target):
    client = ScriptedClient(call(path="/"), answer("ok"))
    Agent(client, target, ALL_TOOLS).run("disks?")

    assert target.executed == []
    assert "invalid arguments" in client.requests[1][0][-1]["content"]


def test_stops_at_the_iteration_limit(target):
    client = ScriptedClient(*[call() for _ in range(3)])
    result = Agent(client, target, ALL_TOOLS, max_iterations=3).run("disks?")

    assert not result.finished
    assert result.iterations == 3
    assert "3 steps" in result.answer
    assert len(client.requests) == 3


def test_long_tool_output_is_truncated(target):
    noisy = Tool(
        name="noisy",
        description="Returns far too much text.",
        properties={},
        required=(),
        func=lambda target, arguments: "x" * (MAX_TOOL_OUTPUT_CHARS * 3),
    )
    client = ScriptedClient(call("noisy"), answer("ok"))
    Agent(client, target, (noisy,)).run("go")

    content = client.requests[1][0][-1]["content"]
    assert len(content) < MAX_TOOL_OUTPUT_CHARS + 50
    assert content.endswith("(truncated)")


def test_several_tool_calls_in_one_turn_are_all_answered(target):
    two_calls = ChatResponse(
        content="", tool_calls=[ToolCall("disk_usage", {}), ToolCall("disk_usage", {})]
    )
    client = ScriptedClient(two_calls, answer("done"))
    Agent(client, target, (DISK_USAGE,)).run("disks?")

    roles = [m["role"] for m in client.requests[1][0]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]
