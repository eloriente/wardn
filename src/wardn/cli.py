"""The `wardn` command: ask the agent questions about a server."""

import argparse
import ipaddress
import sys
from pathlib import Path

from wardn.agent import Agent, AgentResult
from wardn.audit import JsonlAuditLog
from wardn.config import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    Config,
    ConfigError,
    ConfigNotFound,
    TargetConfig,
    load_config,
)
from wardn.evals import evaluate, format_report
from wardn.ollama import OllamaClient, OllamaError
from wardn.redact import RedactionRules, Redactor
from wardn.scenarios import select_scenarios
from wardn.ssh_target import SSHTarget
from wardn.target import DEFAULT_RULES, Allowlist
from wardn.tools import ALL_TOOLS


def build_agent(config: Config, target: TargetConfig, model: str) -> Agent:
    tools = tuple(
        t for t in ALL_TOOLS if target.tools is None or t.name in target.tools
    )
    ssh = SSHTarget(
        Allowlist(DEFAULT_RULES),
        JsonlAuditLog(config.audit_log),
        host=target.host,
        user=target.user,
        key_path=target.key,
        port=target.port,
    )
    # The user and host we connect with are always hidden, besides what the
    # config lists. An IP address is already caught by its pattern.
    rules = RedactionRules(
        hostnames=config.redact.hostnames
        + (() if _is_ip(target.host) else (target.host,)),
        users=config.redact.users + (target.user,),
        paths=config.redact.paths,
    )
    client = OllamaClient(model, base_url=config.ollama_url)
    return Agent(client, ssh, tools, redactor=Redactor(rules))


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
    return parser.parse_args(argv)


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
    try:
        config = load_config(args.config)
        target = config.select_target(args.target)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    agent = build_agent(config, target, args.model or config.model)
    if args.command == "ask":
        return ask(agent, " ".join(args.question), args.verbose)
    return chat(agent, args.verbose)
