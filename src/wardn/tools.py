"""Tools the model can call.

A tool is a name, a description, a strict JSON schema for its arguments and a
function that uses a Target. Tools never run anything themselves, and their
output is short on purpose: the model is small.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wardn.target import CommandNotAllowed, CommandTimeout, Target

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
    if len(swap) >= 2 and swap[0].isdigit() and int(swap[0]) > 0:
        line += f"\nSwap: {swap[1]} MB used of {swap[0]} MB"
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

ALL_TOOLS = (DISK_USAGE, MEMORY_USAGE)
