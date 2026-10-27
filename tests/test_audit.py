import json

import pytest
from fakes import FakeTarget

from wardn.audit import JsonlAuditLog
from wardn.target import CommandNotAllowed, CommandTimeout


def test_executed_command_is_recorded(target):
    target.run(["df", "-h"])
    (event,) = target.audit.events
    assert event["argv"] == ["df", "-h"]
    assert event["status"] == "executed"
    assert event["exit_code"] == 0
    assert "ts" in event
    assert event["duration_ms"] >= 0


def test_rejected_command_is_recorded(target):
    with pytest.raises(CommandNotAllowed):
        target.run(["rm", "-rf", "/"])
    (event,) = target.audit.events
    assert event["argv"] == ["rm", "-rf", "/"]
    assert event["status"] == "rejected"
    assert "reason" in event


def test_timeout_is_recorded(target):
    target.raises = CommandTimeout("slow")
    with pytest.raises(CommandTimeout):
        target.run(["df", "-h"])
    assert target.audit.events[0]["status"] == "timeout"


def test_unexpected_error_is_recorded(target):
    target.raises = OSError("ssh not found")
    with pytest.raises(OSError):
        target.run(["df", "-h"])
    (event,) = target.audit.events
    assert event["status"] == "error"
    assert "ssh not found" in event["reason"]


def test_jsonl_log_appends_one_line_per_event(tmp_path):
    log = JsonlAuditLog(tmp_path / "logs" / "audit.jsonl")
    log.record({"status": "executed"})
    log.record({"status": "rejected"})
    lines = (tmp_path / "logs" / "audit.jsonl").read_text().splitlines()
    assert [json.loads(line)["status"] for line in lines] == ["executed", "rejected"]


@pytest.fixture
def target():
    return FakeTarget()
