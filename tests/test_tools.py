import pytest
from fakes import FakeTarget

from wardn.target import CommandTimeout
from wardn.tools import DISK_USAGE, MAX_LINES, MEMORY_USAGE

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
