from fakes import FakeTarget, ScriptedClient

from wardn.audit import MemoryAuditLog
from wardn.ollama import ChatResponse
from wardn.redact import Redactor
from wardn.summary import (
    MAX_REPORT_CHARS,
    SUMMARY_PROMPT,
    Check,
    Source,
    TargetReport,
    attention,
    collect,
    format_attention,
    format_full,
    summary_tools,
    write_digest,
)
from wardn.tools import ALL_TOOLS, DISK_USAGE

DF_FULL = "Filesystem Size Used Avail Use% Mounted on\n/dev/a 100G 95G 5G 95% /data\n"
DF_OK = "Filesystem Size Used Avail Use% Mounted on\n/dev/a 100G 10G 90G 10% /data\n"


def source(name, stdout, tools=(DISK_USAGE,)):
    target = FakeTarget()
    target.stdout = stdout
    return Source(name, target, tuple(tools))


def report(name, *checks):
    return TargetReport(name, tuple(Check(tool, text) for tool, text in checks))


def test_summary_tools_keeps_the_no_argument_tools_in_report_order():
    names = [t.name for t in summary_tools(ALL_TOOLS)]
    assert names == [
        "failed_services",
        "disk_usage",
        "memory_usage",
        "container_status",
    ]
    assert "service_status" not in names  # it needs a unit
    assert "running_services" not in names


def test_collect_runs_each_tool_and_keeps_the_order_of_the_sources():
    reports = collect([source("a", DF_OK), source("b", DF_FULL)], Redactor())
    assert [r.name for r in reports] == ["a", "b"]
    assert reports[0].checks[0].tool == "disk_usage"
    assert "WARNING" in reports[1].checks[0].text


def test_collect_hides_sensitive_data_and_notes_it_in_the_audit_log():
    src = source(
        "a",
        "Filesystem Size Used Avail Use% Mounted on\n/dev/a 1G 1G 1G 1% /home/ana\n",
    )
    (result,) = collect([src], Redactor())
    assert "/home/ana" not in result.checks[0].text
    assert any(e["event"] == "redaction" for e in src.target.audit.events)


def test_one_broken_machine_does_not_stop_the_rest():
    broken = source("broken", DF_OK)
    broken.target.raises = OSError("ssh: not found")
    reports = collect([broken, source("fine", DF_OK)], Redactor())

    assert reports[0].checks[0].text == "error: ssh: not found"
    assert reports[0].unreachable
    assert not reports[1].unreachable


def test_aliases_are_shared_across_machines():
    text = "Filesystem Size Used Avail Use% Mounted on\n/dev/a 1G 1G 1G 1% /mnt/203.0.113.7\n"
    one, two = collect([source("a", text), source("b", text)], Redactor())
    assert one.checks[0].text == two.checks[0].text


def test_attention_lists_warnings_failed_services_and_errors():
    reports = [
        report(
            "docker",
            ("container_status", "radarr: restarting (x) WARNING\nplex: running (Up)"),
        ),
        report(
            "homelab",
            (
                "failed_services",
                "1 failed systemd services:\n  fix-storage-perms.service (Fix)",
            ),
            ("disk_usage", "/: 10% used (1G of 9G, 8G free)"),
            ("memory_usage", "error: free failed (exit 1): boom"),
        ),
    ]
    assert attention(reports) == [
        "docker: radarr: restarting (x) WARNING",
        "homelab: failed service fix-storage-perms.service (Fix)",
        "homelab: memory_usage failed (free failed (exit 1): boom)",
    ]


def test_an_unreachable_machine_is_one_item_not_one_per_tool():
    err = (
        "error: df failed (exit 255): ssh: connect to host x port 22: No route to host"
    )
    reports = [report("plex", ("disk_usage", err), ("memory_usage", err))]
    assert attention(reports) == [
        "plex: could not be checked (df failed (exit 255): ssh: connect to host x port 22: No route to host)"
    ]


def test_nothing_to_report():
    reports = [
        report(
            "a",
            ("failed_services", "no failed systemd services"),
            ("disk_usage", "/: 10% used"),
        )
    ]
    assert attention(reports) == []
    assert format_attention(reports) == "Nothing needs attention."


def test_format_attention_and_full():
    reports = [report("docker", ("container_status", "x: restarting WARNING"))]
    assert (
        format_attention(reports)
        == "Needs attention:\n  - docker: x: restarting WARNING"
    )
    assert (
        format_full(reports) == "== docker\n[container_status]\nx: restarting WARNING"
    )


def test_digest_is_written_from_the_attention_list_and_the_full_report():
    client = ScriptedClient(ChatResponse("Radarr is down."))
    reports = [report("docker", ("container_status", "radarr: restarting WARNING"))]

    digest = write_digest(client, reports, Redactor(), MemoryAuditLog())

    assert digest == "Radarr is down."
    system, user = client.requests[0][0]
    assert system == {"role": "system", "content": SUMMARY_PROMPT}
    assert user["content"].startswith(
        "Needs attention:\n  - docker: radarr: restarting WARNING"
    )
    assert "== docker" in user["content"]


def test_the_digest_is_hidden_too():
    client = ScriptedClient(ChatResponse("The server at 203.0.113.7 is fine."))
    digest = write_digest(
        client, [report("a", ("disk_usage", "ok"))], Redactor(), MemoryAuditLog()
    )
    assert digest == "The server at <ip-1> is fine."


def test_a_long_report_is_cut_at_the_end_so_the_problems_survive():
    long = "\n".join(f"line {i}" for i in range(2000))
    reports = [
        report("a", ("container_status", "x: restarting WARNING")),
        report("b", ("disk_usage", long)),
    ]
    client = ScriptedClient(ChatResponse("ok"))
    write_digest(client, reports, Redactor(), MemoryAuditLog())

    prompt = client.requests[0][0][1]["content"]
    assert len(prompt) < MAX_REPORT_CHARS + 50
    assert "a: x: restarting WARNING" in prompt
    assert prompt.endswith("... (report cut)")


def test_bot_texts_default_to_english_and_know_spanish():
    from wardn.texts import ENGLISH, SPANISH, bot_texts

    assert bot_texts(None) is ENGLISH
    assert bot_texts("German") is ENGLISH
    for name in ("Spanish", "spanish", "Español", "castellano"):
        assert bot_texts(name) is SPANISH
    assert SPANISH.thinking == "Pensando..."
