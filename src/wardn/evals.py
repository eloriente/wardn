"""Evals: run the agent on known scenarios and measure how often it gets them right.

The model is not deterministic, so one run proves little. Each scenario runs
several times against a scripted server (no SSH, no real machine), and the
result is a pass rate.
"""

import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from wardn.agent import Agent, AgentResult, ChatClient
from wardn.audit import MemoryAuditLog
from wardn.redact import RedactionRules, Redactor
from wardn.target import DEFAULT_RULES, Allowlist, CommandResult, Target
from wardn.tools import ALL_TOOLS


class ScriptedTarget(Target):
    """A server that answers each command with a fixed output."""

    def __init__(self, responses: Mapping[tuple[str, ...], CommandResult]):
        super().__init__(Allowlist(DEFAULT_RULES), MemoryAuditLog())
        self._responses = responses

    def _execute(self, argv: Sequence[str]) -> CommandResult:
        fallback = CommandResult("", f"no fixture for {list(argv)}", 127)
        return self._responses.get(tuple(argv), fallback)


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str
    responses: Mapping[tuple[str, ...], CommandResult]  # what each command prints
    tools: tuple[str, ...] | None = None  # None means all tools
    rules: RedactionRules = field(default_factory=RedactionRules)  # what to hide
    expect_tools: tuple[str, ...] = ()  # must all be called
    forbid_tools: tuple[str, ...] = ()  # none may be called
    expect_any: tuple[str, ...] = ()  # the answer must contain at least one
    forbid: tuple[str, ...] = ()  # the answer must contain none


@dataclass(frozen=True)
class Outcome:
    failures: list[str]
    answer: str
    tools_called: tuple[str, ...]
    seconds: float

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(outcome.passed for outcome in self.outcomes)


def tools_called(result: AgentResult) -> tuple[str, ...]:
    return tuple(
        call["function"]["name"]
        for message in result.messages
        for call in message.get("tool_calls", [])
    )


def check(scenario: Scenario, result: AgentResult) -> list[str]:
    """What went wrong in this run, as a list of short reasons."""
    called = tools_called(result)
    failures = [f"did not call {n}" for n in scenario.expect_tools if n not in called]
    failures += [f"called {n}" for n in scenario.forbid_tools if n in called]

    answer = result.answer.lower()
    if scenario.expect_any and not any(
        t.lower() in answer for t in scenario.expect_any
    ):
        failures.append(f"answer has none of {list(scenario.expect_any)}")
    failures += [
        f"answer contains {t!r}" for t in scenario.forbid if t.lower() in answer
    ]
    if not result.finished:
        failures.append("hit the iteration limit")
    return failures


def run_once(scenario: Scenario, client: ChatClient) -> Outcome:
    tools = tuple(
        t for t in ALL_TOOLS if scenario.tools is None or t.name in scenario.tools
    )
    redactor = Redactor(scenario.rules)
    agent = Agent(client, ScriptedTarget(scenario.responses), tools, redactor=redactor)
    started = time.monotonic()
    result = agent.run(scenario.question)
    return Outcome(
        failures=check(scenario, result),
        answer=result.answer,
        tools_called=tools_called(result),
        seconds=time.monotonic() - started,
    )


def evaluate(
    scenarios: Sequence[Scenario],
    client: ChatClient,
    runs: int,
    on_run: Callable[[str, int, int], None] | None = None,
) -> list[ScenarioResult]:
    results = []
    for scenario in scenarios:
        result = ScenarioResult(scenario)
        for i in range(1, runs + 1):
            if on_run:
                on_run(scenario.name, i, runs)
            result.outcomes.append(run_once(scenario, client))
        results.append(result)
    return results


def format_report(
    results: Sequence[ScenarioResult], model: str, runs: int, verbose: bool
) -> str:
    width = max((len(r.scenario.name) for r in results), default=0)
    lines = [f"model: {model}, {runs} runs per scenario", ""]
    total = passed = 0
    for result in results:
        total += len(result.outcomes)
        passed += result.passed
        seconds = sum(o.seconds for o in result.outcomes) / max(len(result.outcomes), 1)
        lines.append(
            f"{result.scenario.name:<{width}}  {result.passed}/{len(result.outcomes)}  ({seconds:.1f}s per run)"
        )
        reasons = Counter(f for o in result.outcomes for f in o.failures)
        lines += [
            f"{'':<{width}}    - {reason} (x{count})"
            for reason, count in reasons.items()
        ]
        if verbose:
            lines += [
                f"{'':<{width}}    answer: {o.answer!r}"
                for o in result.outcomes
                if not o.passed
            ]
    percent = round(100 * passed / total) if total else 0
    lines += ["", f"total: {passed}/{total} ({percent}%)"]
    return "\n".join(lines)
