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


def test_ask_prints_only_the_answer_by_default(config_file, agent, capsys):
    code = cli.main([*config_file, "ask", "how", "full", "are", "my", "disks?"])
    out, err = capsys.readouterr()

    assert code == 0
    assert agent.questions == ["how full are my disks?"]
    assert out == "Half full.\n"
    assert err == ""


def test_verbose_shows_the_trace_on_stderr(config_file, agent, capsys):
    cli.main([*config_file, "-v", "ask", "disks?"])
    out, err = capsys.readouterr()
    assert out == "Half full.\n"
    assert "[tool call] disk_usage" in err
    assert "[tool result]" in err


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


def test_build_agent_always_hides_the_connection_user_and_host(tmp_path):
    from wardn.config import Config, TargetConfig
    from wardn.redact import RedactionRules

    target = TargetConfig("lab", "nas.lan", "deploy", tmp_path / "key")
    config = Config(
        model="m",
        ollama_url="http://x",
        audit_log=tmp_path / "audit.jsonl",
        targets={"lab": target},
        redact=RedactionRules(hostnames=("pve",), users=("admin",)),
    )
    agent = cli.build_agent(config, target, "m")

    hidden = agent._redactor.redact("deploy admin pve nas.lan 203.0.113.7")
    assert hidden == "<user-1> <user-2> <host-1> <host-2> <ip-1>"


def proxmox_target(tmp_path, tools=None):
    from wardn.config import Config, ProxmoxConfig, TargetConfig

    token = tmp_path / "token"
    token.write_text("wardn@pve!agent=0123abcd-4567\n")
    token.chmod(0o600)
    proxmox = ProxmoxConfig("https://pve.test:8006", "pve", token_file=token)
    target = TargetConfig(
        "pve", "h", "u", tmp_path / "key", tools=tools, proxmox=proxmox
    )
    config = Config("m", "http://x", tmp_path / "audit.jsonl", {"pve": target})
    return config, target


def test_guest_status_is_added_when_proxmox_is_configured(tmp_path):
    config, target = proxmox_target(tmp_path)
    assert "guest_status" in cli.build_agent(config, target, "m")._tools


def test_guest_status_follows_the_tools_list(tmp_path):
    config, target = proxmox_target(tmp_path, tools=("disk_usage",))
    assert "guest_status" not in cli.build_agent(config, target, "m")._tools


def test_unsafe_token_file_stops_the_cli_with_a_clear_error(
    tmp_path, monkeypatch, capsys
):
    config, target = proxmox_target(tmp_path)
    target.proxmox.token_file.chmod(0o644)
    monkeypatch.setattr(cli, "load_config", lambda path: config)

    assert cli.main(["ask", "x"]) == 2
    assert "chmod 600" in capsys.readouterr().err


SUMMARY_CONFIG = """
[targets.docker]
host = "h1"
user = "u"
key = "/k"

[targets.homelab]
host = "h2"
user = "u"
key = "/k"

[targets.sandbox]
host = "127.0.0.1"
user = "u"
key = "/k"
summary = false
"""

DF_FULL = "Filesystem Size Used Avail Use% Mounted on\n/dev/a 100G 97G 3G 97% /data\n"
DF_OK = "Filesystem Size Used Avail Use% Mounted on\n/dev/a 100G 10G 90G 10% /data\n"


@pytest.fixture
def summary_setup(tmp_path, monkeypatch):
    """Two machines: `docker` has a disk at 97%, `homelab` is fine."""
    from fakes import FakeTarget

    from wardn.tools import DISK_USAGE

    path = tmp_path / "config.toml"
    path.write_text(SUMMARY_CONFIG)
    built = []

    def fake_parts(config, target, audit):
        fake = FakeTarget()
        fake.stdout = DF_FULL if target.name == "docker" else DF_OK
        built.append(target.name)
        return fake, (DISK_USAGE,)

    monkeypatch.setattr(cli, "build_parts", fake_parts)
    return ["--config", str(path)], built


def test_summary_without_the_model_lists_what_needs_attention(summary_setup, capsys):
    config, built = summary_setup
    code = cli.main([*config, "summary", "--no-model"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Needs attention:" in out
    assert "docker: /data: 97% used" in out
    assert "homelab: /data" not in out  # a healthy machine adds nothing to the list
    assert "Checked 2 targets: docker, homelab" in out
    assert built == ["docker", "homelab"]  # `sandbox` has summary = false


def test_summary_includes_the_models_text_after_the_attention_list(
    summary_setup, monkeypatch, capsys
):
    from fakes import ScriptedClient

    from wardn.ollama import ChatResponse

    config, _ = summary_setup
    client = ScriptedClient(ChatResponse("The disk of docker is almost full."))
    monkeypatch.setattr(cli, "OllamaClient", lambda *args, **kwargs: client)

    assert cli.main([*config, "summary"]) == 0
    out = capsys.readouterr().out
    assert out.index("Needs attention:") < out.index(
        "Summary:\nThe disk of docker is almost full."
    )


def test_summary_survives_a_model_that_is_down(summary_setup, monkeypatch, capsys):
    class Down:
        def chat(self, messages, tools=None):
            raise OllamaError("connection refused")

    monkeypatch.setattr(cli, "OllamaClient", lambda *args, **kwargs: Down())
    code = cli.main([*summary_setup[0], "summary"])
    captured = capsys.readouterr()

    assert code == 1
    assert "docker: /data: 97% used" in captured.out  # the part that needs no model
    assert "summary unavailable: connection refused" in captured.err


def test_summary_verbose_prints_the_full_report_to_stderr(summary_setup, capsys):
    cli.main([*summary_setup[0], "-v", "summary", "--no-model"])
    captured = capsys.readouterr()
    assert "== homelab" in captured.err
    assert "== homelab" not in captured.out


def test_summary_of_one_target(summary_setup, capsys):
    config, built = summary_setup
    assert cli.main([*config, "--target", "sandbox", "summary", "--no-model"]) == 0
    assert built == ["sandbox"]  # an explicit target ignores summary = false


def test_summary_reports_a_target_that_cannot_be_set_up(
    summary_setup, monkeypatch, capsys
):
    from wardn.proxmox import ProxmoxError

    def fail(config, target, audit):
        raise ProxmoxError("token file is open to others")

    monkeypatch.setattr(cli, "build_parts", fail)
    assert cli.main([*summary_setup[0], "summary", "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "docker: could not be checked (token file is open to others)" in out
    assert "homelab: could not be checked" in out


def test_summary_without_any_enabled_target(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\nsummary = false\n'
    )
    assert cli.main(["--config", str(path), "summary", "--no-model"]) == 2
    assert "no target has summary enabled" in capsys.readouterr().err


BOT_CONFIG = CONFIG + '[telegram]\ntoken_file = "/t"\nallowed_users = [7]\n'


def test_bot_needs_a_telegram_section(config_file, capsys):
    assert cli.main([*config_file, "bot"]) == 2
    assert "no [telegram] section" in capsys.readouterr().err


def test_bot_stops_on_a_token_that_cannot_be_read(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(BOT_CONFIG)
    assert cli.main(["--config", str(path), "bot"]) == 2
    assert "cannot read the token file" in capsys.readouterr().err


@pytest.fixture
def bot_commands(tmp_path, monkeypatch):
    from wardn.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(BOT_CONFIG)
    config = load_config(path)
    config = type(config)(**{**config.__dict__, "audit_log": tmp_path / "audit.jsonl"})
    agent = FakeAgent()
    targets = [config.targets["lab"]]
    return cli.bot_commands(config, agent, targets, "m"), agent


def test_bot_ask_runs_the_agent(bot_commands):
    commands, agent = bot_commands
    sent = []
    commands["/ask"]("how full?", sent.append)
    assert agent.questions == ["how full?"]
    assert sent == ["Half full."]


def test_bot_ask_without_a_question_shows_the_usage(bot_commands):
    commands, agent = bot_commands
    sent = []
    commands["/ask"]("", sent.append)
    assert agent.questions == [] and sent == ["Usage: /ask <question>"]


def test_bot_ask_does_not_pass_the_model_error_on(tmp_path, monkeypatch, bot_commands):
    commands, agent = bot_commands
    agent._error = OllamaError("cannot reach http://10.0.0.5:11434")
    sent = []
    commands["/ask"]("q", sent.append)
    assert sent == ["The model did not answer."]


def test_bot_status_sends_the_attention_list_even_if_the_model_is_down(
    bot_commands, monkeypatch
):
    from fakes import FakeTarget

    from wardn.tools import DISK_USAGE

    def fake_parts(config, target, audit):
        fake = FakeTarget()
        fake.stdout = DF_FULL
        return fake, (DISK_USAGE,)

    def model_down(*args, **kwargs):
        raise OllamaError("down")

    monkeypatch.setattr(cli, "build_parts", fake_parts)
    monkeypatch.setattr(cli, "write_digest", model_down)
    commands, _ = bot_commands
    sent = []
    commands["/status"]("", sent.append)

    assert sent[0] == "Checking the homelab..."
    assert "Needs attention:" in sent[1] and "Checked 1 targets: lab" in sent[1]
    assert sent[2] == "The summary is unavailable: the model did not answer."


def test_echo_audit_shows_telegram_events_but_not_others(capsys):
    from wardn.audit import MemoryAuditLog

    inner = MemoryAuditLog()
    echo = cli.EchoAudit(inner)
    echo.record({"ts": "t", "event": "telegram_denied", "user": 999})
    echo.record({"ts": "t", "event": "command", "argv": ["df"]})

    assert capsys.readouterr().err == "event=telegram_denied, user=999\n"
    assert len(inner.events) == 2  # everything is still recorded


def test_bot_stops_when_telegram_refuses_the_token(tmp_path, monkeypatch, capsys):
    from wardn.telegram import TelegramError

    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG + '[telegram]\ntoken = "123456:ABCdefGhIJKlmNoPQRsTUVwxyz0123456789"\n'
        "allowed_users = [7]\n"
    )
    path.chmod(0o600)

    def refuse(self):
        raise TelegramError("Telegram refused the token: check it with @BotFather")

    monkeypatch.setattr(cli, "build_agent", lambda config, target, model: FakeAgent())
    monkeypatch.setattr(cli.TelegramClient, "whoami", refuse)

    assert cli.main(["--config", str(path), "bot"]) == 2
    assert "refused the token" in capsys.readouterr().err
