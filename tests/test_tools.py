from pathlib import Path

import pytest
from fakes import FakeTarget

from wardn.proxmox import Guest, ProxmoxError
from wardn.target import DOCKER_PS, CommandTimeout
from wardn.tools import (
    CONTAINER_STATUS,
    DISK_USAGE,
    FAILED_SERVICES,
    LOG_LINES,
    MAX_LINES,
    MEMORY_USAGE,
    RUNNING_SERVICES,
    SERVICE_STATUS,
    guest_status_tool,
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
        "RAM: 6% in use (502 MB of 7834 MB), 7332 MB available\nSwap: 0% in use (0 MB of 1023 MB)"
    )
    assert target.executed == [["free", "-m"]]


def test_swap_percentage_is_computed(target):
    # Real output from the homelab host: 2435 / 8191 is 29.7%, rounded to 30%.
    target.stdout = (
        "total used free shared buff/cache available\n"
        "Mem: 15858 6955 4000 100 5000 8903\n"
        "Swap: 8191 2435 5756\n"
    )
    assert MEMORY_USAGE.call(target, {}).endswith(
        "Swap: 30% in use (2435 MB of 8191 MB)"
    )


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


# Same content as sandbox/fake/docker-ps.jsonl.
SANDBOX_DOCKER_PS = (
    Path(__file__).parent.parent / "sandbox/fake/docker-ps.jsonl"
).read_text()
DOCKER_COMMAND = list(DOCKER_PS)


def test_container_status_lists_problems_first(target):
    target.stdout = SANDBOX_DOCKER_PS
    lines = CONTAINER_STATUS.call(target, {}).splitlines()

    assert lines[0] == "radarr: restarting (Restarting (1) 12 seconds ago) WARNING"
    assert "plex: running (Up 3 days (healthy))" in lines
    assert (
        "old-backup: exited (Exited (0) 2 weeks ago)" in lines
    )  # a clean exit is fine
    assert target.executed == [DOCKER_COMMAND]


def test_unhealthy_and_crashed_containers_are_flagged(target):
    target.stdout = (
        '{"Names":"a","State":"running","Status":"Up 1 hour (unhealthy)"}\n'
        '{"Names":"b","State":"exited","Status":"Exited (137) 5 minutes ago"}\n'
    )
    lines = CONTAINER_STATUS.call(target, {}).splitlines()
    assert lines == [
        "a: running (Up 1 hour (unhealthy)) WARNING: unhealthy",
        "b: exited (Exited (137) 5 minutes ago) WARNING: exited with an error",
    ]


def test_container_status_skips_garbage_lines(target):
    target.stdout = 'not json\n[1, 2]\n{"Names":"a","State":"running","Status":"Up"}\n'
    assert CONTAINER_STATUS.call(target, {}) == "a: running (Up)"


def test_no_containers(target):
    target.stdout = ""
    assert CONTAINER_STATUS.call(target, {}) == "no containers found"


def test_container_status_rejects_arguments(target):
    assert CONTAINER_STATUS.call(target, {"all": True}).startswith("error: invalid")
    assert target.executed == []


def test_docker_failure_is_reported_as_text(target):
    target.exit_code = 1
    target.stderr = "Cannot connect to the Docker daemon"
    output = CONTAINER_STATUS.call(target, {})
    assert (
        output == "error: docker failed (exit 1): Cannot connect to the Docker daemon"
    )


FAILED_COMMAND = [
    "systemctl",
    "list-units",
    "--failed",
    "--no-pager",
    "--plain",
    "--no-legend",
]


def test_failed_services_lists_units(target):
    target.stdout = (
        "pvedaemon.service loaded failed failed PVE API Daemon\n"
        "● backup.service loaded failed failed Nightly backup\n"
    )
    assert FAILED_SERVICES.call(target, {}) == (
        "2 failed systemd services:\n"
        "  pvedaemon.service (PVE API Daemon)\n"
        "  backup.service (Nightly backup)"
    )
    assert target.executed == [FAILED_COMMAND]


def test_no_failed_services(target):
    target.stdout = ""
    assert FAILED_SERVICES.call(target, {}) == "no failed systemd services"


def test_failed_services_truncates_long_descriptions_and_lists(target):
    rows = "".join(
        f"u{i}.service loaded failed failed {'x' * 200}\n" for i in range(MAX_LINES + 3)
    )
    target.stdout = rows
    lines = FAILED_SERVICES.call(target, {}).splitlines()
    assert lines[1] == f"  u0.service ({'x' * 80})"
    assert lines[-1] == "  ... 3 more not shown"


def test_failed_services_rejects_arguments(target):
    assert FAILED_SERVICES.call(target, {"unit": "x"}).startswith("error: invalid")
    assert target.executed == []


def test_failed_services_failure_is_reported_as_text(target):
    target.exit_code = 1
    target.stderr = "boom"
    assert FAILED_SERVICES.call(target, {}) == "error: systemctl failed (exit 1): boom"


def make_guest(vmid, name, status="running", kind="lxc", **fields):
    values = {"cpu": 0.05, "mem": 512 * 1024**2, "maxmem": 2048 * 1024**2}
    values |= {
        "disk": 3 * 1024**3,
        "maxdisk": 8 * 1024**3,
        "uptime": 3 * 86400 + 5 * 3600,
    }
    return Guest(vmid=vmid, name=name, kind=kind, status=status, **(values | fields))


class FakeProxmox:
    def __init__(self, guests=(), error=None):
        self._guests, self._error = list(guests), error

    def guests(self):
        if self._error:
            raise self._error
        return self._guests


def call_guests(*guests, error=None):
    tool = guest_status_tool(FakeProxmox(guests, error))
    return tool.call(FakeTarget(), {})


def test_guest_status_summary_computes_the_percentages():
    output = call_guests(make_guest(100, "plex"))
    assert output == (
        "1 of 1 guests running\n"
        "plex (lxc 100): running, up 3d 5h, CPU 5%, RAM 25% (512 of 2048 MB), disk 38%"
    )


def test_guest_status_has_no_parameters():
    schema = guest_status_tool(FakeProxmox()).schema()["function"]["parameters"]
    assert schema["properties"] == {}
    assert (
        guest_status_tool(FakeProxmox())
        .call(FakeTarget(), {"x": 1})
        .startswith("error: invalid")
    )


def test_stopped_guest_shows_only_its_state():
    output = call_guests(make_guest(107, "mealie", "stopped", mem=0, uptime=0))
    assert output.splitlines()[1] == "mealie (lxc 107): stopped"


def test_problems_come_first_then_stopped_then_the_rest():
    output = call_guests(
        make_guest(100, "plex"),
        make_guest(101, "off", "stopped"),
        make_guest(102, "starved", mem=2000 * 1024**2),
    )
    names = [line.split(" ")[0] for line in output.splitlines()[1:]]
    assert names == ["starved", "off", "plex"]
    assert output.splitlines()[0] == "2 of 3 guests running"
    assert "WARNING: high usage" in output.splitlines()[1]


@pytest.mark.parametrize(
    "fields",
    [{"cpu": 0.95}, {"mem": 1950 * 1024**2}, {"disk": 7 * 1024**3 + 700 * 1024**2}],
)
def test_high_cpu_ram_or_disk_is_flagged(fields):
    assert "WARNING" in call_guests(make_guest(100, "x", **fields))


def test_vms_have_no_disk_figure():
    line = call_guests(
        make_guest(200, "vm", kind="qemu", disk=0, maxdisk=0)
    ).splitlines()[1]
    assert "disk" not in line


def test_durations():
    from wardn.tools import _duration

    assert [_duration(s) for s in (59, 600, 3 * 3600 + 120, 2 * 86400 + 7200)] == [
        "0m",
        "10m",
        "3h 2m",
        "2d 2h",
    ]


def test_guest_status_truncates_long_lists():
    output = call_guests(*(make_guest(i, f"g{i}") for i in range(MAX_LINES + 5)))
    lines = output.splitlines()
    assert len(lines) == MAX_LINES + 1
    assert lines[-1] == "... 6 more not shown"


def test_no_guests_and_proxmox_errors_are_text():
    assert call_guests() == "no guests found"
    assert call_guests(error=ProxmoxError("refused")) == "error: refused"


RUNNING_COMMAND = [
    "systemctl",
    "list-units",
    "--type=service",
    "--state=running",
    "--no-pager",
    "--plain",
    "--no-legend",
]
RUNNING_OUTPUT = (
    "cron.service loaded active running Regular background program processing daemon\n"
    "plexmediaserver.service loaded active running Plex Media Server for Linux\n"
    "ssh.service loaded active running OpenBSD Secure Shell server\n"
)


def test_running_services_schema_makes_search_optional():
    parameters = RUNNING_SERVICES.schema()["function"]["parameters"]
    assert parameters["properties"]["search"]["type"] == "string"
    assert parameters["required"] == []


def test_running_services_lists_every_service(target):
    target.stdout = RUNNING_OUTPUT
    output = RUNNING_SERVICES.call(target, {})
    assert output.splitlines()[0] == "3 running services:"
    assert "  ssh.service (OpenBSD Secure Shell server)" in output
    assert target.executed == [RUNNING_COMMAND]


def test_running_services_search_matches_name_or_description_case_insensitively(target):
    target.stdout = RUNNING_OUTPUT
    assert RUNNING_SERVICES.call(target, {"search": "PLEX"}) == (
        "1 running services matching 'PLEX':\n"
        "  plexmediaserver.service (Plex Media Server for Linux)"
    )
    assert "ssh.service" in RUNNING_SERVICES.call(target, {"search": "secure shell"})


def test_running_services_without_a_match(target):
    target.stdout = RUNNING_OUTPUT
    assert (
        RUNNING_SERVICES.call(target, {"search": "nope"})
        == "no running services match 'nope'"
    )


def test_running_services_truncates_and_suggests_searching(target):
    target.stdout = "".join(
        f"u{i}.service loaded active running U {i}\n" for i in range(MAX_LINES + 4)
    )
    lines = RUNNING_SERVICES.call(target, {}).splitlines()
    assert len(lines) == MAX_LINES + 2
    assert lines[-1] == "  ... 4 more not shown: pass search to narrow it down"


def test_running_services_rejects_non_text_search_and_unknown_arguments(target):
    assert RUNNING_SERVICES.call(target, {"search": 5}).startswith(
        "error: search must be text"
    )
    assert RUNNING_SERVICES.call(target, {"filter": "x"}).startswith("error: invalid")
    assert target.executed == []


def test_running_services_failure_is_reported_as_text(target):
    target.exit_code = 1
    target.stderr = "boom"
    assert RUNNING_SERVICES.call(target, {}) == "error: systemctl failed (exit 1): boom"


def test_service_status_not_found_points_to_the_service_list(target):
    target.exit_code = 4
    assert "running services" in SERVICE_STATUS.call(target, {"unit": "plex.service"})
