import pytest
from fakes import FakeTarget

from wardn.target import CommandNotAllowed


@pytest.fixture
def target():
    return FakeTarget()


def test_allowed_command_is_executed(target):
    result = target.run(["df", "-h"])
    assert result.exit_code == 0
    assert target.executed == [["df", "-h"]]


def test_allowed_command_with_valid_argument(target):
    target.run(["systemctl", "status", "ssh.service", "--no-pager"])
    assert len(target.executed) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["rm", "-rf", "/"],  # unknown binary
        ["df", "-h", "/"],  # extra argument
        ["df"],  # missing argument
        ["systemctl", "restart", "ssh.service", "--no-pager"],  # wrong verb
        ["systemctl", "status", "--help.service", "--no-pager"],  # option as unit
        ["systemctl", "status", "ssh.service; rm -rf /", "--no-pager"],  # injection
        ["systemctl", "status", "ssh.service\n", "--no-pager"],  # trailing newline
        ["systemctl", "status", "$(id).service", "--no-pager"],  # substitution
        [],  # empty
    ],
)
def test_disallowed_commands_are_rejected_and_not_executed(target, argv):
    with pytest.raises(CommandNotAllowed):
        target.run(argv)
    assert target.executed == []


def test_plain_string_is_rejected(target):
    with pytest.raises(CommandNotAllowed):
        target.run("df -h")
    assert target.executed == []
