"""A summary of the whole homelab.

Every target runs the tools that need no arguments. What needs attention is
found by plain rules, so it never depends on the model; the model only writes
a short summary on top of it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from wardn.agent import ChatClient, with_language
from wardn.redact import Redactor, hide
from wardn.target import Target
from wardn.tools import Tool

# The tools that need no arguments, in the order of the report.
NO_ARGUMENT_TOOLS = (
    "guest_status",
    "failed_services",
    "disk_usage",
    "memory_usage",
    "container_status",
)

# The model sees the attention list first, so a cut here loses the least.
MAX_REPORT_CHARS = 5000

SUMMARY_PROMPT = (
    "You are Wardn, an assistant that watches over a homelab. Below is the health "
    "report of its machines. Write a short summary for the owner, in plain sentences. "
    "Start with the problems: mention each one once, naming the machine, or say that "
    "nothing needs attention. Do not begin with yes or no. State only what the report "
    "says: never guess consequences or causes, and never say anything about a machine "
    "or a check the report does not list as a problem."
)


@dataclass(frozen=True)
class Source:
    """One machine to check, with the tools it may use."""

    name: str
    target: Target
    tools: tuple[Tool, ...]


@dataclass(frozen=True)
class Check:
    tool: str
    text: str


@dataclass(frozen=True)
class TargetReport:
    name: str
    checks: tuple[Check, ...]

    @property
    def unreachable(self) -> bool:
        """Every check failed: the machine itself could not be checked."""
        return bool(self.checks) and all(
            c.text.startswith("error:") for c in self.checks
        )


def summary_tools(tools: Sequence[Tool]) -> tuple[Tool, ...]:
    """Keep the tools that need no arguments, in report order."""
    by_name = {tool.name: tool for tool in tools}
    return tuple(by_name[name] for name in NO_ARGUMENT_TOOLS if name in by_name)


def collect(sources: Sequence[Source], redactor: Redactor) -> list[TargetReport]:
    """Run every source's tools. One broken machine never stops the rest."""
    redactor.reset()
    reports = []
    for source in sources:
        checks = []
        for tool in source.tools:
            try:
                text = tool.call(source.target, {})
            except OSError as exc:  # for example the ssh program is missing
                text = f"error: {exc}"
            text = hide(redactor, source.target.audit, text, tool.name)
            checks.append(Check(tool.name, text))
        reports.append(TargetReport(source.name, tuple(checks)))
    return reports


def attention(reports: Sequence[TargetReport]) -> list[str]:
    """What needs a look, as one line per item."""
    items = []
    for report in reports:
        if report.unreachable:
            reason = report.checks[0].text.removeprefix("error: ")
            items.append(f"{report.name}: could not be checked ({reason})")
            continue
        for check in report.checks:
            lines = check.text.splitlines()
            if check.text.startswith("error:"):
                items.append(
                    f"{report.name}: {check.tool} failed ({check.text.removeprefix('error: ')})"
                )
            elif check.tool == "failed_services" and not check.text.startswith(
                "no failed"
            ):
                items += [
                    f"{report.name}: failed service {line.strip()}"
                    for line in lines[1:]
                ]
            else:
                items += [
                    f"{report.name}: {line.strip()}"
                    for line in lines
                    if "WARNING" in line
                ]
    return items


def format_attention(reports: Sequence[TargetReport]) -> str:
    items = attention(reports)
    if not items:
        return "Nothing needs attention."
    return "Needs attention:\n" + "\n".join(f"  - {item}" for item in items)


def format_full(reports: Sequence[TargetReport]) -> str:
    sections = []
    for report in reports:
        body = "\n".join(f"[{c.tool}]\n{c.text}" for c in report.checks)
        sections.append(f"== {report.name}\n{body}")
    return "\n\n".join(sections)


def write_digest(
    client: ChatClient,
    reports: Sequence[TargetReport],
    redactor: Redactor,
    audit,
    language: str | None = None,
) -> str:
    """The model's short summary. It reads the attention list, then the full report."""
    report = f"{format_attention(reports)}\n\n{format_full(reports)}"
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n... (report cut)"
    response = client.chat(
        [
            {"role": "system", "content": with_language(SUMMARY_PROMPT, language)},
            {"role": "user", "content": report},
        ]
    )
    response = replace(
        response, content=hide(redactor, audit, response.content, "summary")
    )
    return response.content
