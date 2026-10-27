"""The `wardn` command: ask the agent questions about a server."""

import argparse
import sys
from pathlib import Path

from wardn.agent import Agent, AgentResult
from wardn.audit import JsonlAuditLog
from wardn.config import Config, ConfigError, TargetConfig, load_config
from wardn.ollama import OllamaClient, OllamaError
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
    return Agent(OllamaClient(model, base_url=config.ollama_url), ssh, tools)


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


def ask(agent: Agent, question: str, quiet: bool) -> int:
    """Answer one question. The answer goes to stdout, the trace to stderr."""
    try:
        result = agent.run(question)
    except OllamaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not quiet and (trace := format_trace(result)):
        print(trace + "\n", file=sys.stderr)
    print(result.answer)
    if not result.finished:
        return 1
    return 0


def chat(agent: Agent, quiet: bool) -> int:
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
            ask(agent, question, quiet)  # each question starts a fresh conversation


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wardn", description="Ask an agent about your homelab."
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--target", help="server to inspect (see config.toml)")
    parser.add_argument("--model", help="Ollama model, overriding the config")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print only the answer"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ask_parser = commands.add_parser("ask", help="ask one question and exit")
    ask_parser.add_argument("question", nargs="+")
    commands.add_parser("chat", help="ask several questions interactively")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        target = config.select_target(args.target)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    agent = build_agent(config, target, args.model or config.model)
    if args.command == "ask":
        return ask(agent, " ".join(args.question), args.quiet)
    return chat(agent, args.quiet)
