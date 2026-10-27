"""Runs against the dev sandbox. Skipped unless WARDN_SSH_KEY points at the key.

docker compose -f compose.dev.yaml up -d
WARDN_SSH_KEY=~/.ssh/wardn_sandbox uv run pytest tests/test_ssh_integration.py
"""

import os
from pathlib import Path

import pytest

from wardn.audit import MemoryAuditLog
from wardn.ssh_target import SSHTarget
from wardn.target import DEFAULT_RULES, DOCKER_PS, Allowlist, CommandNotAllowed

KEY = os.environ.get("WARDN_SSH_KEY")

pytestmark = pytest.mark.skipif(not KEY, reason="WARDN_SSH_KEY not set")


@pytest.fixture
def target():
    return SSHTarget(
        Allowlist(DEFAULT_RULES),
        MemoryAuditLog(),
        host="127.0.0.1",
        user="wardn",
        key_path=Path(KEY).expanduser(),
        port=2222,
    )


def test_df_runs_in_sandbox(target):
    result = target.run(["df", "-h"])
    assert result.exit_code == 0
    assert "Filesystem" in result.stdout


def test_systemctl_status_runs_in_sandbox(target):
    result = target.run(["systemctl", "status", "ssh.service", "--no-pager"])
    assert result.exit_code == 0
    assert "active (running)" in result.stdout


def test_disallowed_command_is_blocked_before_ssh(target):
    with pytest.raises(CommandNotAllowed):
        target.run(["cat", "/etc/passwd"])


def test_fake_docker_ps_runs_in_sandbox(target):
    result = target.run(list(DOCKER_PS))
    assert result.exit_code == 0
    assert '"Names":"plex"' in result.stdout


def test_failed_services_runs_in_sandbox(target):
    command = [
        "systemctl",
        "list-units",
        "--failed",
        "--no-pager",
        "--plain",
        "--no-legend",
    ]
    assert target.run(command).exit_code == 0


def test_running_services_runs_in_sandbox(target):
    command = [
        "systemctl",
        "list-units",
        "--type=service",
        "--state=running",
        "--no-pager",
        "--plain",
        "--no-legend",
    ]
    result = target.run(command)
    assert result.exit_code == 0
    assert "ssh.service" in result.stdout
