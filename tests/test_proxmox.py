import stat

import httpx
import pytest

from wardn.audit import MemoryAuditLog
from wardn.proxmox import ProxmoxClient, ProxmoxError, load_token

LXC = [
    {"vmid": 101, "name": "debian", "status": "stopped", "maxmem": 1073741824},
    {
        "vmid": 100,
        "name": "plex",
        "status": "running",
        "cpu": 0.04,
        "mem": 1572864000,
        "maxmem": 4294967296,
        "disk": 3221225472,
        "maxdisk": 8589934592,
        "uptime": 259200,
    },
]
QEMU = [
    {"vmid": 200, "name": "vm", "status": "running", "cpu": 0.5, "mem": 1, "maxmem": 2},
    {"vmid": 900, "name": "template", "status": "stopped", "template": 1},
]


def make_client(handler, audit=None):
    http = httpx.Client(
        base_url="https://pve.test:8006",
        headers={"Authorization": "PVEAPIToken=wardn@pve!agent=secret"},
        transport=httpx.MockTransport(handler),
    )
    return ProxmoxClient(
        "https://pve.test:8006", "pve", "unused", audit or MemoryAuditLog(), http=http
    )


def api(request):
    kind = request.url.path.rsplit("/", 1)[-1]
    return httpx.Response(200, json={"data": {"lxc": LXC, "qemu": QEMU}[kind]})


def test_lists_lxc_and_vms_sorted_by_id_without_templates():
    guests = make_client(api).guests()

    assert [(g.vmid, g.name, g.kind) for g in guests] == [
        (100, "plex", "lxc"),
        (101, "debian", "lxc"),
        (200, "vm", "qemu"),
    ]
    plex = guests[0]
    assert (plex.status, plex.cpu, plex.uptime) == ("running", 0.04, 259200)
    assert guests[1].mem == 0  # a stopped guest reports no usage


def test_only_two_get_requests_to_the_configured_node():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return api(request)

    make_client(handler).guests()
    assert seen == [
        ("GET", "/api2/json/nodes/pve/lxc"),
        ("GET", "/api2/json/nodes/pve/qemu"),
    ]


def test_every_call_is_audited_without_the_token():
    audit = MemoryAuditLog()
    make_client(api, audit).guests()

    assert [
        (e["event"], e["method"], e["path"], e["status"]) for e in audit.events
    ] == [
        ("api", "GET", "/api2/json/nodes/pve/lxc", 200),
        ("api", "GET", "/api2/json/nodes/pve/qemu", 200),
    ]
    assert "secret" not in str(audit.events)


@pytest.mark.parametrize("status", [401, 403])
def test_refused_token_is_explained(status):
    client = make_client(lambda request: httpx.Response(status))
    with pytest.raises(ProxmoxError, match="PVEAuditor"):
        client.guests()


def test_other_http_errors():
    client = make_client(lambda request: httpx.Response(500))
    with pytest.raises(ProxmoxError, match="500"):
        client.guests()


def test_unreachable_proxmox_is_audited_and_reported():
    audit = MemoryAuditLog()

    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProxmoxError, match="cannot reach"):
        make_client(handler, audit).guests()
    assert audit.events[0]["status"] == "error"


@pytest.mark.parametrize("body", [{}, {"data": "nope"}, {"data": [{"name": "x"}]}])
def test_unexpected_answers(body):
    client = make_client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ProxmoxError, match="unexpected"):
        client.guests()


@pytest.mark.parametrize("node", ["", "pve/../x", "pve node", "-pve"])
def test_node_name_is_validated(node):
    with pytest.raises(ProxmoxError, match="invalid node"):
        ProxmoxClient("https://x", node, "t", MemoryAuditLog())


def write_token(tmp_path, text, mode):
    path = tmp_path / "token"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_token_is_read_from_a_private_file(tmp_path):
    path = write_token(tmp_path, "wardn@pve!agent=0123abcd-4567\n", 0o600)
    assert load_token(path) == "wardn@pve!agent=0123abcd-4567"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_token_file_readable_by_others_is_refused(tmp_path, mode):
    path = write_token(tmp_path, "wardn@pve!agent=0123abcd", mode)
    with pytest.raises(ProxmoxError, match="chmod 600"):
        load_token(path)
    assert stat.S_IMODE(path.stat().st_mode) == mode  # we do not touch the file


def test_token_with_the_wrong_shape_is_refused(tmp_path):
    path = write_token(tmp_path, "just-a-secret", 0o600)
    with pytest.raises(ProxmoxError, match="user@realm!tokenid=secret"):
        load_token(path)


def test_missing_token_file(tmp_path):
    with pytest.raises(ProxmoxError, match="cannot read"):
        load_token(tmp_path / "nope")
