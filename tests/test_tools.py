import pytest
from fakes import FakeTarget

from wardn.target import CommandTimeout
from wardn.tools import (
    DISK_USAGE,
    LOG_LINES,
    MAX_LINES,
    MEMORY_USAGE,
    SERVICE_STATUS,
)

# Real output captured from the dev sandbox.
SANDBOX_DF = """\
Filesystem      Size  Used Avail Use% Mounted on
overlay          59G   28G   28G  50% /
tmpfs            64M     0   64M   0% /dev
shm              64M     0   64M   0% /dev/shm
tmpfs           3.9G   36K  3.9G   1% /run
tmpfs           3.9G     0  3.9G   0% /tmp
/dev/vda1        59G   28G   28G  50% /etc/hosts
tmpfs           3.9G     0  3.9G   0% /run/lock
"""


@pytest.fixture
def target():
    return FakeTarget()


def test_schema_has_no_parameters_and_is_strict():
    function = DISK_USAGE.schema()["function"]
    assert function["name"] == "disk_usage"
    assert function["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_disk_usage_keeps_only_real_disks(target):
    target.stdout = SANDBOX_DF
    output = DISK_USAGE.call(target, {})
    assert output == "/: 50% used (28G of 59G, 28G free)"
    assert target.executed == [["df", "-h"]]


def test_almost_full_disk_is_flagged(target):
    target.stdout = (
        "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 95G 5G 95% /data\n"
    )
    assert "WARNING" in DISK_USAGE.call(target, {})


def test_mount_point_with_spaces_is_kept(target):
    target.stdout = "Filesystem Size Used Avail Use% Mounted on\n/dev/sdb1 1T 1G 1T 1% /mnt/my disk\n"
    assert DISK_USAGE.call(target, {}).startswith("/mnt/my disk:")


def test_output_is_truncated(target):
    rows = "\n".join(f"/dev/sd{i} 1G 1G 1G 1% /mnt/d{i}" for i in range(MAX_LINES + 5))
    target.stdout = f"Filesystem Size Used Avail Use% Mounted on\n{rows}\n"
    lines = DISK_USAGE.call(target, {}).splitlines()
    assert len(lines) == MAX_LINES + 1
    assert lines[-1] == "... 5 more not shown"


def test_no_disks(target):
    target.stdout = "Filesystem Size Used Avail Use% Mounted on\n"
    assert DISK_USAGE.call(target, {}) == "no disks found"


def test_unknown_argument_is_rejected_without_running(target):
    output = DISK_USAGE.call(target, {"path": "/"})
    assert output.startswith("error: invalid arguments")
    assert target.executed == []


def test_df_failure_is_reported_as_text(target):
    target.exit_code = 1
    target.stderr = "df: boom"
    assert DISK_USAGE.call(target, {}) == "error: df failed (exit 1): df: boom"


def test_timeout_is_reported_as_text(target):
    target.raises = CommandTimeout("slow")
    assert DISK_USAGE.call(target, {}) == "error: the command timed out"


# Real output captured from the dev sandbox.
SANDBOX_FREE = """\
               total        used        free      shared  buff/cache   available
Mem:            7834         501        6830           0         663        7332
Swap:           1023           0        1023
"""


def test_memory_usage_summary(target):
    target.stdout = SANDBOX_FREE
    output = MEMORY_USAGE.call(target, {})
    assert output == (
        "RAM: 6% in use (502 MB of 7834 MB), 7332 MB available\nSwap: 0 MB used of 1023 MB"
    )
    assert target.executed == [["free", "-m"]]


def test_memory_usage_without_swap(target):
    target.stdout = "total used free shared buff/cache available\nMem: 1000 100 800 0 100 850\nSwap: 0 0 0\n"
    assert MEMORY_USAGE.call(target, {}).endswith("Swap: none")


def test_low_memory_is_flagged(target):
    target.stdout = "total used free shared buff/cache available\nMem: 1000 950 20 0 30 50\nSwap: 0 0 0\n"
    assert "WARNING: low memory" in MEMORY_USAGE.call(target, {})


def test_unreadable_free_output(target):
    target.stdout = "garbage"
    assert MEMORY_USAGE.call(target, {}).startswith("error: could not read")


def test_memory_usage_rejects_arguments(target):
    assert MEMORY_USAGE.call(target, {"unit": "gb"}).startswith("error: invalid")
    assert target.executed == []


def test_free_failure_is_reported_as_text(target):
    target.exit_code = 1
    target.stderr = "free: boom"
    assert MEMORY_USAGE.call(target, {}) == "error: free failed (exit 1): free: boom"


# Real output captured from the dev sandbox (trimmed).
SANDBOX_SSH_STATUS = """\
● ssh.service - OpenBSD Secure Shell server
     Loaded: loaded (/lib/systemd/system/ssh.service; enabled; preset: enabled)
     Active: active (running) since Tue 2026-10-27 07:42:53 UTC; 39s ago
       Docs: man:sshd(8)
   Main PID: 55 (sshd)
      Tasks: 4 (limit: 9444)
     CGroup: /docker/a9c2/system.slice/ssh.service
             ├─55 "sshd: /usr/sbin/sshd -D [listener] 0 of 10-100 startups"
             └─66 systemctl status ssh --no-pager

Oct 27 07:43:30 a9c21a719dfb sshd[60]: Accepted publickey for wardn
Oct 27 07:43:31 a9c21a719dfb sshd[60]: pam_unix(sshd:session): session opened
Oct 27 07:43:32 a9c21a719dfb sshd[65]: Received disconnect from 172.19.0.1
Oct 27 07:43:32 a9c21a719dfb sshd[65]: Disconnected from user wardn
Oct 27 07:43:33 a9c21a719dfb sshd[70]: Accepted publickey for wardn
Oct 27 07:43:34 a9c21a719dfb sshd[70]: pam_unix(sshd:session): session closed
Warning: journal has been rotated since unit was started, output may be incomplete.
"""

SSH_COMMAND = ["systemctl", "status", "ssh.service", "--no-pager"]


def test_service_status_schema_requires_unit():
    parameters = SERVICE_STATUS.schema()["function"]["parameters"]
    assert parameters["required"] == ["unit"]
    assert parameters["properties"]["unit"]["type"] == "string"


def test_service_status_summary_and_last_log_lines(target):
    target.stdout = SANDBOX_SSH_STATUS
    lines = SERVICE_STATUS.call(target, {"unit": "ssh.service"}).splitlines()

    assert (
        lines[0]
        == "ssh.service: active (running) since Tue 2026-10-27 07:42:53 UTC; 39s ago"
    )
    assert lines[1] == "Recent log:"
    assert len(lines) == 2 + LOG_LINES
    assert lines[-1].endswith("session closed")  # the warning line is ignored
    assert target.executed == [SSH_COMMAND]


def test_failed_service_is_a_valid_answer(target):
    target.exit_code = 3
    target.stdout = "● plex.service - Plex\n     Active: failed (Result: exit-code) since Tue 2026-10-27 07:00:00 UTC; 5min ago\n"
    output = SERVICE_STATUS.call(target, {"unit": "plex.service"})
    assert (
        output
        == "plex.service: failed (Result: exit-code) since Tue 2026-10-27 07:00:00 UTC; 5min ago"
    )


def test_unknown_unit(target):
    target.exit_code = 4
    target.stderr = "Unit nope.service could not be found."
    output = SERVICE_STATUS.call(target, {"unit": "nope.service"})
    assert output.startswith("nope.service: no systemd unit with that name")
    assert "container" in output
    assert "not installed" not in output


def test_bare_name_gets_service_suffix(target):
    target.stdout = SANDBOX_SSH_STATUS
    SERVICE_STATUS.call(target, {"unit": "ssh"})
    assert target.executed == [SSH_COMMAND]


@pytest.mark.parametrize(
    "unit", ["ssh; rm -rf /", "--help", "$(id)", "a b", "", 42, None]
)
def test_invalid_unit_is_rejected_without_running(target, unit):
    output = SERVICE_STATUS.call(target, {"unit": unit})
    assert output.startswith("error: unit must be")
    assert target.executed == []


def test_missing_unit_is_an_invalid_call(target):
    assert SERVICE_STATUS.call(target, {}).startswith("error: invalid arguments")


def test_systemctl_failure_is_reported_as_text(target):
    target.exit_code = 1
    target.stderr = "boom"
    assert SERVICE_STATUS.call(target, {"unit": "ssh"}).startswith(
        "error: systemctl failed (exit 1)"
    )
