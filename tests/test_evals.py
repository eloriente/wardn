import pytest
from fakes import ScriptedClient

from wardn import cli
from wardn.agent import AgentResult
from wardn.evals import (
    Outcome,
    Scenario,
    ScenarioResult,
    ScriptedTarget,
    check,
    evaluate,
    format_report,
    tools_called,
)
from wardn.ollama import ChatResponse, ToolCall
from wardn.scenarios import DF, SCENARIOS, out, select_scenarios
from wardn.target import DEFAULT_RULES, Allowlist
from wardn.tools import ALL_TOOLS


def result(answer="", tools=(), finished=True):
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": n, "arguments": {}}}],
        }
        for n in tools
    ]
    return AgentResult(answer, messages, 1, finished)


def scenario(**kwargs):
    return Scenario(name="s", question="q", responses={}, **kwargs)


def test_tools_called_lists_every_call_in_order():
    assert tools_called(result(tools=("a", "b", "a"))) == ("a", "b", "a")


def test_check_passes_when_everything_matches():
    s = scenario(expect_tools=("disk_usage",), expect_any=("47%",), forbid=("broken",))
    assert check(s, result("It is 47% full.", tools=("disk_usage",))) == []


@pytest.mark.parametrize(
    ("kwargs", "run", "expected"),
    [
        ({"expect_tools": ("disk_usage",)}, result("x"), "did not call disk_usage"),
        (
            {"forbid_tools": ("disk_usage",)},
            result("x", ("disk_usage",)),
            "called disk_usage",
        ),
        ({"expect_any": ("47%", "full")}, result("fine"), "answer has none of"),
        (
            {"forbid": ("not installed",)},
            result("It is NOT INSTALLED."),
            "answer contains",
        ),
        ({}, result("x", finished=False), "hit the iteration limit"),
    ],
)
def test_check_reports_each_kind_of_failure(kwargs, run, expected):
    failures = check(scenario(**kwargs), run)
    assert len(failures) == 1
    assert expected in failures[0]


def test_expect_any_is_case_insensitive():
    assert check(scenario(expect_any=("Radarr",)), result("radarr is restarting")) == []


def test_scripted_target_answers_known_commands_and_fails_unknown_ones():
    target = ScriptedTarget({DF: out("data")})
    assert target.run(["df", "-h"]).stdout == "data"
    missing = target.run(["free", "-m"])
    assert missing.exit_code == 127


def test_evaluate_runs_the_agent_several_times():
    s = Scenario(
        name="disk",
        question="disks?",
        responses={
            DF: out(
                "Filesystem Size Used Avail Use% Mounted on\n/dev/a 1T 1G 1T 47% /\n"
            )
        },
        expect_tools=("disk_usage",),
        expect_any=("47%",),
    )
    ok = [
        ChatResponse("", [ToolCall("disk_usage", {})]),
        ChatResponse("The disk is 47% full."),
    ]
    bad = [ChatResponse("I will check later.")]
    client = ScriptedClient(*ok, *bad)

    seen = []
    (outcome,) = evaluate([s], client, runs=2, on_run=lambda *a: seen.append(a))

    assert seen == [("disk", 1, 2), ("disk", 2, 2)]
    assert outcome.passed == 1
    assert outcome.outcomes[1].failures == [
        "did not call disk_usage",
        "answer has none of ['47%']",
    ]


def test_report_summarizes_pass_rates_and_failure_reasons():
    good = Outcome([], "fine", ("disk_usage",), 2.0)
    bad = Outcome(["did not call disk_usage"], "I will check", (), 4.0)
    results = [ScenarioResult(scenario(), [good, bad, bad])]

    report = format_report(results, "m", runs=3, verbose=False)

    assert "model: m, 3 runs per scenario" in report
    assert "1/3" in report
    assert "did not call disk_usage (x2)" in report
    assert "total: 1/3 (33%)" in report
    assert "answer:" not in report


def test_verbose_report_shows_failed_answers():
    bad = Outcome(["x"], "I will check", (), 1.0)
    report = format_report([ScenarioResult(scenario(), [bad])], "m", 1, verbose=True)
    assert "answer: 'I will check'" in report


def test_scenario_names_are_unique():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("s", SCENARIOS, ids=lambda s: s.name)
def test_scenario_is_well_formed(s):
    known_tools = {tool.name for tool in ALL_TOOLS}
    named = set(s.expect_tools) | set(s.forbid_tools) | set(s.tools or ())
    assert named <= known_tools
    assert set(s.expect_tools) <= set(s.tools or known_tools)
    allowlist = Allowlist(DEFAULT_RULES)
    for command in s.responses:
        allowlist.check(
            list(command)
        )  # a fixture for a command nobody can run is a typo


def test_select_scenarios():
    assert select_scenarios([]) == SCENARIOS
    assert [s.name for s in select_scenarios(["disk_ok"])] == ["disk_ok"]
    with pytest.raises(ValueError, match="unknown scenarios"):
        select_scenarios(["nope"])


def test_eval_command_needs_no_target_or_config(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_evaluate(scenarios, client, runs, on_run):
        seen.update(count=len(scenarios), runs=runs)
        return []

    monkeypatch.setattr(cli, "evaluate", fake_evaluate)
    code = cli.main(["--config", str(tmp_path / "missing.toml"), "eval", "--runs", "3"])

    assert code == 0
    assert seen == {"count": len(SCENARIOS), "runs": 3}
    assert "total: 0/0" in capsys.readouterr().out


def test_eval_command_rejects_unknown_scenario(tmp_path, capsys):
    code = cli.main(["--config", str(tmp_path / "missing.toml"), "eval", "nope"])
    assert code == 2
    assert "unknown scenarios" in capsys.readouterr().err
