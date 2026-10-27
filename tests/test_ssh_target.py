import re
import subprocess
from pathlib import Path

import pytest

from wardn import ssh_target
from wardn.audit import MemoryAuditLog
from wardn.ssh_target import SSHTarget
from wardn.target import (
    DEFAULT_RULES,
    Allowlist,
    CommandNotAllowed,
    CommandTimeout,
    Rule,
)


class FakeRun:
    """Stands in for subprocess.run and records how it was called."""

    def __init__(self, returncode=0, stdout="out", stderr=""):
        self.calls = []
        self._completed = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self._completed


def make_target(rules=DEFAULT_RULES, **kwargs):
    return SSHTarget(
        Allowlist(rules),
        MemoryAuditLog(),
        host="127.0.0.1",
        user="wardn",
        key_path=Path("/keys/id"),
        port=2222,
        **kwargs,
    )


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(ssh_target.subprocess, "run", fake)
    return fake


def test_builds_ssh_command(fake_run):
    result = make_target().run(["df", "-h"])

    command, _ = fake_run.calls[0]
    assert command[0] == "ssh"
    assert command[command.index("-i") + 1] == "/keys/id"
    assert command[command.index("-p") + 1] == "2222"
    assert "BatchMode=yes" in command
    assert command[-3:] == ["wardn@127.0.0.1", "--", "df -h"]
    assert result.stdout == "out"
    assert result.exit_code == 0


def test_never_uses_a_local_shell(fake_run):
    make_target().run(["df", "-h"])
    _, kwargs = fake_run.calls[0]
    assert not kwargs.get("shell")


def test_remote_arguments_are_quoted(fake_run):
    rules = (Rule(("echo", re.compile(r".+"))),)
    make_target(rules).run(["echo", "a; b"])
    command, _ = fake_run.calls[0]
    assert command[-1] == "echo 'a; b'"


def test_disallowed_command_never_reaches_ssh(fake_run):
    with pytest.raises(CommandNotAllowed):
        make_target().run(["rm", "-rf", "/"])
    assert fake_run.calls == []


def test_nonzero_exit_code_is_returned(monkeypatch):
    monkeypatch.setattr(
        ssh_target.subprocess, "run", FakeRun(returncode=3, stderr="boom")
    )
    result = make_target().run(["df", "-h"])
    assert (result.exit_code, result.stderr) == (3, "boom")


def test_timeout_raises_command_timeout(monkeypatch):
    def raise_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(ssh_target.subprocess, "run", raise_timeout)
    with pytest.raises(CommandTimeout):
        make_target(timeout=1).run(["df", "-h"])
