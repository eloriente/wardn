import pytest

from wardn import cli
from wardn.agent import AgentResult
from wardn.ollama import OllamaError

CONFIG = '[targets.lab]\nhost = "h"\nuser = "u"\nkey = "/k"\n'

TRACE = [
    {"role": "system", "content": "prompt"},
    {"role": "user", "content": "disks?"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "disk_usage", "arguments": {}}}],
    },
    {"role": "tool", "tool_name": "disk_usage", "content": "/: 50% used"},
    {"role": "assistant", "content": "Half full."},
]


class FakeAgent:
    def __init__(self, answer="Half full.", finished=True, error=None):
        self.questions = []
        self._result = AgentResult(answer, TRACE, 2, finished)
        self._error = error

    def run(self, question):
        self.questions.append(question)
        if self._error:
            raise self._error
        return self._result


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return ["--config", str(path)]


@pytest.fixture
def agent(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(cli, "build_agent", lambda config, target, model: fake)
    return fake


def test_format_trace_shows_tool_calls_and_results():
    trace = cli.format_trace(AgentResult("x", TRACE, 2, True))
    assert trace == "[tool call] disk_usage({})\n[tool result]\n/: 50% used"


def test_ask_prints_answer_to_stdout_and_trace_to_stderr(config_file, agent, capsys):
    code = cli.main([*config_file, "ask", "how", "full", "are", "my", "disks?"])
    out, err = capsys.readouterr()

    assert code == 0
    assert agent.questions == ["how full are my disks?"]
    assert out == "Half full.\n"
    assert "[tool call] disk_usage" in err


def test_quiet_hides_the_trace(config_file, agent, capsys):
    cli.main([*config_file, "-q", "ask", "disks?"])
    out, err = capsys.readouterr()
    assert out == "Half full.\n"
    assert err == ""


def test_unfinished_run_exits_with_error_code(config_file, monkeypatch, capsys):
    fake = FakeAgent(answer="I could not finish.", finished=False)
    monkeypatch.setattr(cli, "build_agent", lambda *args: fake)
    assert cli.main([*config_file, "ask", "disks?"]) == 1


def test_ollama_error_is_reported_without_traceback(config_file, monkeypatch, capsys):
    fake = FakeAgent(error=OllamaError("connection refused"))
    monkeypatch.setattr(cli, "build_agent", lambda *args: fake)

    assert cli.main([*config_file, "ask", "disks?"]) == 1
    assert "connection refused" in capsys.readouterr().err


def test_bad_config_exits_with_code_2(tmp_path, capsys):
    code = cli.main(["--config", str(tmp_path / "missing.toml"), "ask", "x"])
    assert code == 2
    assert "config not found" in capsys.readouterr().err


def test_unknown_target_exits_with_code_2(config_file, agent, capsys):
    assert cli.main([*config_file, "--target", "nope", "ask", "x"]) == 2


def test_model_flag_overrides_config(config_file, monkeypatch):
    seen = {}

    def fake_build(config, target, model):
        seen["model"] = model
        return FakeAgent()

    monkeypatch.setattr(cli, "build_agent", fake_build)
    cli.main([*config_file, "--model", "qwen3:8b", "ask", "x"])
    assert seen["model"] == "qwen3:8b"


def test_chat_asks_each_line_until_exit(config_file, agent, monkeypatch, capsys):
    lines = iter(["disks?", "", "ram?", "exit", "never asked"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    assert cli.main([*config_file, "chat"]) == 0
    assert agent.questions == ["disks?", "ram?"]


def test_chat_ends_on_ctrl_d(config_file, agent, monkeypatch):
    def raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert cli.main([*config_file, "chat"]) == 0
    assert agent.questions == []
