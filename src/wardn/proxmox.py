"""Read-only client for the Proxmox API: the state of the guests (LXC and VMs).

It can do exactly two things, both GET: list the LXC containers and the virtual
machines of one node. There is no generic request method, just as Target has no
generic command. Every call is written to the audit log.
"""

import re
import ssl
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from wardn.audit import AuditLog, timestamp

NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
TOKEN = re.compile(r"\S+@\S+!\S+=\S+")  # user@realm!tokenid=secret


class ProxmoxError(Exception):
    """Proxmox is unreachable, refused us, or answered with something unusable."""


@dataclass(frozen=True)
class Guest:
    vmid: int
    name: str
    kind: str  # "lxc" or "qemu"
    status: str  # "running", "stopped"...
    cpu: float = 0.0  # share of its CPUs in use, 0 to 1
    mem: int = 0  # bytes
    maxmem: int = 0
    disk: int = 0
    maxdisk: int = 0
    uptime: int = 0  # seconds


class GuestSource(Protocol):
    def guests(self) -> list[Guest]: ...


def load_token(path: Path) -> str:
    """Read the API token, refusing a file other users can read."""
    path = path.expanduser()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProxmoxError(f"cannot read the token file {path}: {exc}") from exc
    if mode & 0o077:
        raise ProxmoxError(f"{path} can be read by other users. Run: chmod 600 {path}")
    if not TOKEN.fullmatch(text):
        raise ProxmoxError(f"{path} must hold one line: user@realm!tokenid=secret")
    return text


class ProxmoxClient:
    def __init__(
        self,
        base_url: str,
        node: str,
        token: str,
        audit: AuditLog,
        ca_file: Path | None = None,
        timeout: float = 10.0,
        http: httpx.Client | None = None,
    ):
        if not NODE.fullmatch(node):
            raise ProxmoxError(f"invalid node name {node!r}")
        self._node = node
        self._audit = audit
        if http is None:
            verify: ssl.SSLContext | bool = True
            if ca_file is not None:
                verify = ssl.create_default_context(cafile=str(ca_file.expanduser()))
            http = httpx.Client(
                base_url=base_url,
                headers={"Authorization": f"PVEAPIToken={token}"},
                verify=verify,
                timeout=timeout,
            )
        self._http = http

    def guests(self) -> list[Guest]:
        found = [*self._list("lxc"), *self._list("qemu")]
        return sorted(found, key=lambda guest: guest.vmid)

    def _list(self, kind: str) -> list[Guest]:
        path = f"/api2/json/nodes/{self._node}/{kind}"
        try:
            response = self._http.get(path)
        except httpx.HTTPError as exc:
            self._note(path, "error")
            raise ProxmoxError(f"cannot reach Proxmox: {exc}") from exc
        self._note(path, response.status_code)

        if response.status_code in (401, 403):
            raise ProxmoxError(
                "Proxmox refused the token: check that it exists and has the PVEAuditor role"
            )
        if response.is_error:
            raise ProxmoxError(f"Proxmox answered {response.status_code}")
        try:
            rows = response.json()["data"]
            return [_parse_guest(row, kind) for row in rows if not row.get("template")]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProxmoxError(f"unexpected answer from Proxmox: {exc!r}") from exc

    def _note(self, path: str, status: int | str) -> None:
        self._audit.record(
            {
                "ts": timestamp(),
                "event": "api",
                "method": "GET",
                "path": path,
                "status": status,
            }
        )


def _parse_guest(row: dict[str, Any], kind: str) -> Guest:
    vmid = int(row["vmid"])
    return Guest(
        vmid=vmid,
        name=str(row.get("name") or f"{kind}-{vmid}"),
        kind=kind,
        status=str(row.get("status", "unknown")),
        cpu=float(row.get("cpu") or 0),
        mem=int(row.get("mem") or 0),
        maxmem=int(row.get("maxmem") or 0),
        disk=int(row.get("disk") or 0),
        maxdisk=int(row.get("maxdisk") or 0),
        uptime=int(row.get("uptime") or 0),
    )
