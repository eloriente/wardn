"""Tools the model can call.

A tool is a name, a description, a strict JSON schema for its arguments and a
function that uses a Target. Tools never run anything themselves, and their
output is short on purpose: the model is small.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wardn.target import UNIT, CommandNotAllowed, CommandTimeout, Target

MAX_LINES = 20


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    func: Callable[[Target, dict[str, Any]], str]

    def schema(self) -> dict[str, Any]:
        """The tool definition in the format Ollama expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }

    def call(self, target: Target, arguments: dict[str, Any]) -> str:
        """Run the tool. Problems come back as text so the model can react."""
        unknown = set(arguments) - set(self.properties)
        missing = set(self.required) - set(arguments)
        if unknown or missing:
            return f"error: invalid arguments (unknown: {sorted(unknown)}, missing: {sorted(missing)})"
        try:
            return self.func(target, arguments)
        except CommandNotAllowed as exc:
            return f"error: {exc}"
        except CommandTimeout:
            return "error: the command timed out"


# Filesystems that are not real disks, and mounts that are only noise
# (container bind mounts, docker's per-container overlays).
IGNORED_FILESYSTEMS = {"tmpfs", "devtmpfs", "shm", "udev", "efivarfs", "squashfs"}
IGNORED_MOUNT_PREFIXES = ("/etc/", "/var/lib/docker/")
WARNING_PERCENT = 90


def _disk_usage(target: Target, arguments: dict[str, Any]) -> str:
    result = target.run(["df", "-h"])
    if result.exit_code != 0:
        return (
            f"error: df failed (exit {result.exit_code}): {result.stderr.strip()[:200]}"
        )

    lines = []
    for raw in result.stdout.splitlines()[1:]:  # skip the header
        parts = raw.split(None, 5)  # the mount point may contain spaces
        if len(parts) != 6:
            continue
        filesystem, size, used, avail, percent, mount = parts
        if filesystem in IGNORED_FILESYSTEMS or mount.startswith(
            IGNORED_MOUNT_PREFIXES
        ):
            continue
        line = f"{mount}: {percent} used ({used} of {size}, {avail} free)"
        if (
            percent.rstrip("%").isdigit()
            and int(percent.rstrip("%")) >= WARNING_PERCENT
        ):
            line += " WARNING: almost full"
        lines.append(line)

    if not lines:
        return "no disks found"
    if len(lines) > MAX_LINES:
        extra = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES] + [f"... {extra} more not shown"]
    return "\n".join(lines)


DISK_USAGE = Tool(
    name="disk_usage",
    description="Show how full each disk is on the server.",
    properties={},
    required=(),
    func=_disk_usage,
)

LOW_MEMORY_PERCENT = 10  # warn when less than this share is available


def _memory_usage(target: Target, arguments: dict[str, Any]) -> str:
    result = target.run(["free", "-m"])
    if result.exit_code != 0:
        return f"error: free failed (exit {result.exit_code}): {result.stderr.strip()[:200]}"

    rows = {}
    for raw in result.stdout.splitlines():
        label, _, rest = raw.partition(":")
        if label in ("Mem", "Swap"):
            rows[label] = rest.split()
    mem = rows.get("Mem", [])
    if len(mem) < 6 or not all(value.isdigit() for value in mem[:6]):
        return "error: could not read the output of free"

    total, available = int(mem[0]), int(mem[5])
    # "available" is what new programs can really get (it counts reclaimable
    # cache), so memory in use is total minus available, not free's "used".
    in_use = total - available
    line = (
        f"RAM: {round(100 * in_use / total)}% in use "
        f"({in_use} MB of {total} MB), {available} MB available"
    )
    if available * 100 < total * LOW_MEMORY_PERCENT:
        line += " WARNING: low memory"

    swap = rows.get("Swap", [])
    if len(swap) >= 2 and swap[0].isdigit() and swap[1].isdigit() and int(swap[0]) > 0:
        # Percentages are computed here: the model is unreliable at arithmetic.
        percent = round(100 * int(swap[1]) / int(swap[0]))
        line += f"\nSwap: {percent}% in use ({swap[1]} MB of {swap[0]} MB)"
    else:
        line += "\nSwap: none"
    return line


MEMORY_USAGE = Tool(
    name="memory_usage",
    description="Show how much RAM is in use on the server.",
    properties={},
    required=(),
    func=_memory_usage,
)

LOG_LINES = 5
MAX_LOG_LINE_CHARS = 200
# Journal lines in `systemctl status` start with a timestamp, e.g. "Oct 27 07:40:34".
JOURNAL_LINE = re.compile(r"[A-Z][a-z]{2} +\d{1,2} \d\d:\d\d:\d\d ")
BARE_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@_-]*")
UNIT_HINT = "error: unit must be a systemd unit name such as ssh.service"


def _service_status(target: Target, arguments: dict[str, Any]) -> str:
    unit = arguments["unit"]
    if not isinstance(unit, str):
        return UNIT_HINT
    if BARE_UNIT.fullmatch(unit):
        unit += ".service"  # forgive "ssh" for "ssh.service"
    if not UNIT.fullmatch(unit):
        return UNIT_HINT

    result = target.run(["systemctl", "status", unit, "--no-pager"])
    if result.exit_code == 4:
        # Say only what we know: a service may run outside systemd (a container).
        return (
            f"{unit}: no systemd unit with that name "
            "(the service may run elsewhere, for example in a container)"
        )

    lines = [line.strip() for line in result.stdout.splitlines()]
    active = next(
        (l.removeprefix("Active:").strip() for l in lines if l.startswith("Active:")),
        None,
    )
    # systemctl exits 3 for a unit that is inactive or failed: still a valid answer.
    if result.exit_code not in (0, 3) or active is None:
        return f"error: systemctl failed (exit {result.exit_code}): {result.stderr.strip()[:200]}"

    summary = [f"{unit}: {active}"]
    log = [l[:MAX_LOG_LINE_CHARS] for l in lines if JOURNAL_LINE.match(l)][-LOG_LINES:]
    if log:
        summary.append("Recent log:")
        summary.extend(f"  {line}" for line in log)
    return "\n".join(summary)


SERVICE_STATUS = Tool(
    name="service_status",
    description=(
        "Show whether a systemd service of the host is running, and its recent "
        "log lines. Not for Docker containers."
    ),
    properties={
        "unit": {
            "type": "string",
            "description": "Name of the systemd unit, for example ssh.service",
        }
    },
    required=("unit",),
    func=_service_status,
)

MAX_DESCRIPTION_CHARS = 80


def _failed_services(target: Target, arguments: dict[str, Any]) -> str:
    result = target.run(
        [
            "systemctl",
            "list-units",
            "--failed",
            "--no-pager",
            "--plain",
            "--no-legend",
        ]
    )
    if result.exit_code != 0:
        return f"error: systemctl failed (exit {result.exit_code}): {result.stderr.strip()[:200]}"

    units = []
    for raw in result.stdout.splitlines():
        parts = raw.lstrip("●* ").split(None, 4)  # unit load active sub description
        if len(parts) >= 4:
            description = parts[4][:MAX_DESCRIPTION_CHARS] if len(parts) == 5 else ""
            units.append(f"  {parts[0]}" + (f" ({description})" if description else ""))

    if not units:
        return "no failed systemd services"
    if len(units) > MAX_LINES:
        extra = len(units) - MAX_LINES
        units = units[:MAX_LINES] + [f"  ... {extra} more not shown"]
    return f"{len(units)} failed systemd services:\n" + "\n".join(units)


FAILED_SERVICES = Tool(
    name="failed_services",
    description=(
        "List the systemd services of the host that have failed. "
        "Use it to check whether anything is broken."
    ),
    properties={},
    required=(),
    func=_failed_services,
)

EXIT_CODE = re.compile(r"Exited \((\d+)\)")


def _container_line(container: dict[str, Any]) -> str:
    name = container.get("Names", "?")
    state = container.get("State", "?")
    status = container.get("Status", "")
    line = f"{name}: {state} ({status})"

    exit_code = EXIT_CODE.search(status)
    if state in ("restarting", "dead", "paused"):
        line += " WARNING"
    elif "(unhealthy)" in status:
        line += " WARNING: unhealthy"
    elif exit_code and exit_code.group(1) != "0":
        line += " WARNING: exited with an error"
    return line


def _container_status(target: Target, arguments: dict[str, Any]) -> str:
    result = target.run(["docker", "ps", "-a", "--format", "{{json .}}"])
    if result.exit_code != 0:
        return f"error: docker failed (exit {result.exit_code}): {result.stderr.strip()[:200]}"

    lines = []
    for raw in result.stdout.splitlines():
        try:
            container = json.loads(raw)
        except ValueError:
            continue
        if isinstance(container, dict):
            lines.append(_container_line(container))

    if not lines:
        return "no containers found"
    lines.sort(key=lambda line: "WARNING" in line, reverse=True)  # problems first
    if len(lines) > MAX_LINES:
        extra = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES] + [f"... {extra} more not shown"]
    return "\n".join(lines)


CONTAINER_STATUS = Tool(
    name="container_status",
    description=(
        "List the Docker containers on the server (apps such as Plex or Sonarr "
        "usually run as containers) and whether each one is running."
    ),
    properties={},
    required=(),
    func=_container_status,
)

ALL_TOOLS = (
    DISK_USAGE,
    MEMORY_USAGE,
    SERVICE_STATUS,
    FAILED_SERVICES,
    CONTAINER_STATUS,
)
