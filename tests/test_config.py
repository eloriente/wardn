from pathlib import Path

import pytest

from wardn.config import DEFAULT_MODEL, ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


VALID = """
model = "qwen3:8b"
default_target = "lab"

[targets.lab]
host = "192.168.1.10"
user = "wardn"
key = "~/.ssh/wardn_lab"
tools = ["disk_usage", "failed_services"]

[targets.sandbox]
host = "127.0.0.1"
port = 2222
user = "wardn"
key = "/keys/sandbox"
"""


def test_valid_config(tmp_path):
    config = load_config(write(tmp_path, VALID))

    assert config.model == "qwen3:8b"
    lab = config.targets["lab"]
    assert (lab.host, lab.user, lab.port) == ("192.168.1.10", "wardn", 22)
    assert lab.key == Path("~/.ssh/wardn_lab").expanduser()
    assert lab.tools == ("disk_usage", "failed_services")
    assert config.targets["sandbox"].tools is None  # all tools
    assert config.targets["sandbox"].port == 2222


def test_defaults(tmp_path):
    config = load_config(
        write(tmp_path, '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\n')
    )
    assert config.model == DEFAULT_MODEL
    assert config.ollama_url == "http://localhost:11434"


def test_missing_file_explains_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not toml [", "cannot read"),
        ("", "at least one"),
        ('[targets.a]\nuser = "u"\nkey = "/k"\n', "missing 'host'"),
        (
            '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\nport = "22"\n',
            "'port' must be",
        ),
        (
            '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\ntools = ["rm_rf"]\n',
            "unknown tools",
        ),
        (
            'default_target = "x"\n[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\n',
            "default_target",
        ),
    ],
)
def test_invalid_config(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_select_target(tmp_path):
    config = load_config(write(tmp_path, VALID))
    assert config.select_target(None).name == "lab"  # the default
    assert config.select_target("sandbox").name == "sandbox"
    with pytest.raises(ConfigError, match="unknown target"):
        config.select_target("nope")


def test_several_targets_without_default_need_a_choice(tmp_path):
    text = VALID.replace('default_target = "lab"\n', "")
    config = load_config(write(tmp_path, text))
    with pytest.raises(ConfigError, match="choose one with --target"):
        config.select_target(None)


def test_single_target_is_selected_automatically(tmp_path):
    config = load_config(
        write(tmp_path, '[targets.only]\nhost = "h"\nuser = "u"\nkey = "/k"\n')
    )
    assert config.select_target(None).name == "only"


def test_redact_section(tmp_path):
    text = (
        '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\n'
        '[redact]\nhostnames = ["pve"]\nusers = ["deploy"]\npaths = ["/mnt/private"]\n'
    )
    redact = load_config(write(tmp_path, text)).redact
    assert (redact.hostnames, redact.users, redact.paths) == (
        ("pve",),
        ("deploy",),
        ("/mnt/private",),
    )


def test_redact_is_optional(tmp_path):
    config = load_config(
        write(tmp_path, '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\n')
    )
    assert config.redact.hostnames == ()


def test_invalid_redact_section(tmp_path):
    text = (
        '[targets.a]\nhost = "h"\nuser = "u"\nkey = "/k"\n[redact]\nusers = "deploy"\n'
    )
    with pytest.raises(ConfigError, match="'users' must be a list"):
        load_config(write(tmp_path, text))
