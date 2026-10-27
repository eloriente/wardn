"""The `wardn` command: ask the agent questions about a server."""

import argparse
import ipaddress
import sys
from pathlib import Path

from wardn.agent import Agent, AgentResult
from wardn.audit import AuditLog, Event, JsonlAuditLog, timestamp
from wardn.config import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    GUEST_STATUS,
    Config,
    ConfigError,
    ConfigNotFound,
    TargetConfig,
    load_config,
)
from wardn.evals import evaluate, format_report
from wardn.ollama import OllamaClient, OllamaError
from wardn.proxmox import ProxmoxClient, ProxmoxError
from wardn.redact import RedactionRules, Redactor, hide
from wardn.scenarios import select_scenarios
from wardn.ssh_target import SSHTarget
from wardn.summary import (
    Check,
    Source,
    TargetReport,
    collect,
    format_attention,
    format_full,
    summary_tools,
    write_digest,
)
from wardn.target import DEFAULT_RULES, Allowlist
from wardn.telegram import (
    Bot,
    Handler,
    Reply,
    TelegramClient,
    TelegramError,
)
from wardn.texts import bot_texts
from wardn.tools import ALL_TOOLS, Tool, guest_status_tool


def build_parts(
    config: Config, target: TargetConfig, audit: AuditLog
) -> tuple[SSHTarget, tuple[Tool, ...]]:
    """The connection to one target and the tools it may use."""
    tools = tuple(
        t for t in ALL_TOOLS if target.tools is None or t.name in target.tools
    )
    if target.proxmox and (target.tools is None or GUEST_STATUS in target.tools):
        proxmox = ProxmoxClient(
            target.proxmox.url,
            target.proxmox.node,
            target.proxmox.read_token(),
            audit,
            ca_file=target.proxmox.ca_file,
        )
        tools += (guest_status_tool(proxmox),)
    ssh = SSHTarget(
        Allowlist(DEFAULT_RULES),
        audit,
        host=target.host,
        user=target.user,
        key_path=target.key,
        port=target.port,
    )
    return ssh, tools


def redaction_rules(config: Config, targets: list[TargetConfig]) -> RedactionRules:
    """The user and host we connect with are always hidden, besides what the config
    lists. An IP address is already caught by its pattern."""
    hostnames = list(config.redact.hostnames)
    users = list(config.redact.users)
    for target in targets:
        if not _is_ip(target.host):
            hostnames.append(target.host)
        users.append(target.user)
    return RedactionRules(
        hostnames=tuple(dict.fromkeys(hostnames)),
        users=tuple(dict.fromkeys(users)),
        paths=config.redact.paths,
    )


def build_agent(config: Config, target: TargetConfig, model: str) -> Agent:
    ssh, tools = build_parts(config, target, JsonlAuditLog(config.audit_log))
    client = OllamaClient(model, base_url=config.ollama_url)
    redactor = Redactor(redaction_rules(config, [target]))
    return Agent(client, ssh, tools, redactor=redactor, language=config.language)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def format_trace(result: AgentResult) -> str:
    """What the agent did: the tools it called and what they returned."""
    lines = []
    for message in result.messages[2:]:  # skip the system prompt and the question
        for call in message.get("tool_calls", []):
            function = call["function"]
            lines.append(f"[tool call] {function['name']}({function['arguments']})")
        if message["role"] == "tool":
            lines.append(f"[tool result]\n{message['content']}")
    return "\n".join(lines)


def ask(agent: Agent, question: str, verbose: bool) -> int:
    """Answer one question. The answer goes to stdout, the trace (if asked) to stderr."""
    try:
        result = agent.run(question)
    except OllamaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if verbose and (trace := format_trace(result)):
        print(trace + "\n", file=sys.stderr)
    print(result.answer)
    if not result.finished:
        return 1
    return 0


def chat(agent: Agent, verbose: bool) -> int:
    try:
        import readline  # noqa: F401  # line editing and history for input()
    except ImportError:
        pass
    print("Ask about your server. Type 'exit' or press Ctrl-D to leave.")
    while True:
        try:
            question = input("\nwardn> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question in ("exit", "quit"):
            return 0
        if question:
            ask(agent, question, verbose)  # each question starts a fresh conversation


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wardn", description="Ask an agent about your homelab."
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--target", help="server to inspect (see config.toml)")
    parser.add_argument("--model", help="Ollama model, overriding the config")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also show the tools the agent used and what they returned",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ask_parser = commands.add_parser("ask", help="ask one question and exit")
    ask_parser.add_argument("question", nargs="+")
    commands.add_parser("chat", help="ask several questions interactively")
    eval_parser = commands.add_parser(
        "eval",
        help="run the eval scenarios and measure how often the agent gets them right",
    )
    eval_parser.add_argument(
        "scenarios", nargs="*", help="scenarios to run (default: all)"
    )
    eval_parser.add_argument(
        "--runs", type=int, default=5, help="runs per scenario (default: 5)"
    )
    summary_parser = commands.add_parser(
        "summary", help="summarize the whole homelab: what needs attention"
    )
    summary_parser.add_argument(
        "--no-model",
        action="store_true",
        help="print only what needs attention, without the model's summary",
    )
    commands.add_parser(
        "bot", help="answer /status and /ask in Telegram, to the allowed users only"
    )
    return parser.parse_args(argv)


def summary_targets(config: Config, name: str | None) -> list[TargetConfig]:
    """The target asked for, or every one that has not opted out of the summary."""
    if name:
        return [config.select_target(name)]
    targets = [t for t in config.targets.values() if t.summary]
    if not targets:
        raise ConfigError("no target has summary enabled")
    return targets


def gather_reports(
    config: Config, targets: list[TargetConfig], audit: AuditLog
) -> tuple[list[TargetReport], Redactor]:
    """Check every target. One that cannot even be set up is reported, not fatal."""
    # One redactor for the whole report, so <ip-1> means the same in every section.
    redactor = Redactor(redaction_rules(config, list(config.targets.values())))
    sources, broken = [], {}
    for target in targets:
        try:
            ssh, tools = build_parts(config, target, audit)
        except ProxmoxError as exc:
            broken[target.name] = str(exc)
        else:
            sources.append(Source(target.name, ssh, summary_tools(tools)))
    checked = {report.name: report for report in collect(sources, redactor)}
    reports = []
    for target in targets:
        if target.name in broken:
            text = hide(redactor, audit, f"error: {broken[target.name]}", "setup")
            checked[target.name] = TargetReport(target.name, (Check("setup", text),))
        reports.append(checked[target.name])
    return reports, redactor


def format_checked(reports: list[TargetReport]) -> str:
    return f"Checked {len(reports)} targets: {', '.join(r.name for r in reports)}"


def run_summary(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        targets = summary_targets(config, args.target)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    audit = JsonlAuditLog(config.audit_log)
    reports, redactor = gather_reports(config, targets, audit)

    print(format_attention(reports))
    print("\n" + format_checked(reports))
    code = 0
    if not args.no_model:
        client = OllamaClient(args.model or config.model, base_url=config.ollama_url)
        try:
            print(
                "\nSummary:\n"
                + write_digest(client, reports, redactor, audit, config.language)
            )
        except OllamaError as exc:
            print(f"\nsummary unavailable: {exc}", file=sys.stderr)
            code = 1
    if args.verbose:
        print("\n" + format_full(reports), file=sys.stderr)
    return code


class EchoAudit:
    """Records to the audit log, and shows the bot's events on the screen too.
    These carry no message text: only who, which command, and what failed."""

    def __init__(self, inner: AuditLog):
        self._inner = inner

    def record(self, event: Event) -> None:
        self._inner.record(event)
        if str(event["event"]).startswith("telegram"):
            details = ", ".join(f"{k}={v}" for k, v in event.items() if k != "ts")
            print(details, file=sys.stderr)


MAX_QUESTION_CHARS = 1000


def bot_commands(
    config: Config,
    agent: Agent,
    targets: list[TargetConfig],
    model: str,
    audit: AuditLog,
) -> dict[str, Handler]:
    """What the Telegram bot does. Failures are told in plain words: the exception
    text could hold an address, and it would travel through Telegram. It goes to the
    audit log instead."""
    client = OllamaClient(model, base_url=config.ollama_url)
    texts = bot_texts(config.language)

    def failed(command: str, exc: Exception) -> None:
        audit.record(
            {
                "ts": timestamp(),
                "event": "telegram_error",
                "command": command,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    def status(_args: str, reply: Reply) -> None:
        reply(texts.checking)
        reports, redactor = gather_reports(config, targets, audit)
        reply(f"{format_attention(reports)}\n\n{format_checked(reports)}")
        try:
            reply(
                f"{texts.summary}\n"
                + write_digest(client, reports, redactor, audit, config.language)
            )
        except OllamaError as exc:
            failed("/status", exc)
            reply(texts.summary_unavailable)

    def ask_agent(question: str, reply: Reply) -> None:
        if not question:
            reply(texts.usage)
            return
        reply(texts.thinking)
        try:
            result = agent.run(question[:MAX_QUESTION_CHARS])
        except OllamaError as exc:
            failed("/ask", exc)
            reply(texts.no_answer)
            return
        reply(result.answer or texts.empty_answer)
        if not result.finished:
            reply(texts.unfinished)

    return {"/status": status, "/ask": ask_agent}


def run_bot(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        if config.telegram is None:
            raise ConfigError("no [telegram] section: see config.example.toml")
        target = config.select_target(args.target)
        targets = summary_targets(config, None)
        token = config.telegram.read_token()
        model = args.model or config.model
        agent = build_agent(config, target, model)
    except (ConfigError, TelegramError, ProxmoxError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    telegram = TelegramClient(token)
    try:
        username = telegram.whoami()
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    audit = EchoAudit(JsonlAuditLog(config.audit_log))
    bot = Bot(
        telegram,
        config.telegram.allowed_users,
        bot_commands(config, agent, targets, model, audit),
        audit,
        help_text=bot_texts(config.language).help,
    )
    print(
        f"Wardn bot {username} running; /ask uses {target.name!r}. Ctrl-C to stop.\n"
        "Write to it in Telegram: what it receives shows here."
    )
    try:
        bot.run()
    except KeyboardInterrupt:
        print()
    return 0


def _progress(name: str, run: int, runs: int) -> None:
    print(f"\r{name} ({run}/{runs})".ljust(60), end="", file=sys.stderr, flush=True)


def run_evals(args: argparse.Namespace) -> int:
    """Evals use scripted servers, so no target is needed, and a config is optional."""
    model, url = DEFAULT_MODEL, DEFAULT_OLLAMA_URL
    try:
        config = load_config(args.config)
        model, url = config.model, config.ollama_url
    except ConfigNotFound:
        pass
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    model = args.model or model

    try:
        scenarios = select_scenarios(args.scenarios)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        results = evaluate(
            scenarios, OllamaClient(model, base_url=url), args.runs, _progress
        )
    except OllamaError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    print("\r".ljust(60), end="\r", file=sys.stderr)
    print(format_report(results, model, args.runs, args.verbose))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "eval":
        return run_evals(args)
    if args.command == "summary":
        return run_summary(args)
    if args.command == "bot":
        return run_bot(args)
    try:
        config = load_config(args.config)
        target = config.select_target(args.target)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        agent = build_agent(config, target, args.model or config.model)
    except ProxmoxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command == "ask":
        return ask(agent, " ".join(args.question), args.verbose)
    return chat(agent, args.verbose)
