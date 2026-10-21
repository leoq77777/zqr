import json
import sys
from datetime import datetime

import pandas as pd
import pytest

from quant_research.controller.agent import (
    AgentRoundResult,
    CandidateSubmission,
    CommandRunner,
    ManualRunner,
    ScriptedRunner,
    TokenUsage,
    claude_command,
    usage_from_output,
)
from quant_research.controller.budget import BEIJING, Budget, ModelPricing, pricing_preset
from quant_research.controller.controller import ResearchController, SessionConfig, SessionState
from quant_research.controller.tools import tool_backtest
from quant_research.data.bars import to_epoch
from quant_research.strategy.sandbox import SandboxConfig
from tests.conftest import make_mandate


def _cfg(dataset, acceptance=None, budget=None, **kw):
    mandate_kw = {"budget": budget} if budget else {}
    return SessionConfig(mandate=make_mandate(dataset, acceptance, **mandate_kw),
                         sandbox=SandboxConfig(isolation="subprocess"), **kw)


def test_session_runs_rounds_records_everything_and_allows_no_strategy(paths, store, dataset):
    seen_tasks = []

    def agent(task):
        seen_tasks.append(task)
        assert "holdout" not in task.research_data_path or "splits" in task.research_data_path
        return AgentRoundResult(candidates=[
            CandidateSubmission(template="sma_cross", parameters={"fast": 5 * task.round, "slow": 60},
                                hypothesis="trend persistence"),
            CandidateSubmission(template="sma_cross", parameters={"fast": 100, "slow": 10}),  # invalid
        ], report=f"round {task.round} notes", usage=TokenUsage(input_tokens=1000, output_tokens=200))

    ctl = ResearchController(paths, store, ScriptedRunner(agent))
    sid = ctl.create_session(_cfg(dataset, acceptance={"min_primary": 99}))
    summary = ctl.run(sid)
    sess = store.get_session(sid)
    assert sess["state"] == SessionState.BUDGET_EXHAUSTED and "round budget" in sess["stop_reason"]
    assert len(summary["candidates"]) == 6 and summary["best"] is None
    verdicts = {c["verdict"] for c in summary["candidates"]}
    assert verdicts == {"rejected"}
    assert sess["usage"]["input_tokens"] == 3000
    # history from earlier rounds is passed to later rounds
    assert len(seen_tasks[2].history) == 4
    exps = store.list_experiments(limit=1000, session_id=sid)
    assert any(e["status"] == "rejected" for e in exps)
    llm = [e["llm_suggestion"] for e in exps if e["llm_suggestion"]]
    assert llm and llm[0]["hypothesis"] in ("trend persistence", "")
    report = (paths.reports / sid / "report.md").read_text()
    assert "No candidate met the acceptance criteria" in report


def test_accepts_candidate_and_stops_when_agent_done(paths, store, dataset):
    def agent(task):
        return AgentRoundResult(candidates=[CandidateSubmission(template="buy_and_hold")], done=True)

    ctl = ResearchController(paths, store, ScriptedRunner(agent))
    sid = ctl.create_session(_cfg(dataset))
    summary = ctl.run(sid)
    assert store.get_session(sid)["state"] == SessionState.COMPLETED
    best = summary["best"]
    assert best["strategy_ref"] == "template:buy_and_hold" and best["plan_retrospective"]
    cand = store.get_candidate(best["candidate_id"])
    assert cand["state"] == "validated" and cand["mandate_id"].startswith("mandate-")
    assert [e["to_state"] for e in store.candidate_events(cand["id"])] == ["draft", "development", "validated"]


def test_plugin_candidates_require_docker_unless_allowed(paths, store, dataset):
    def agent(task):
        d = __import__("pathlib").Path(task.candidate_dir)
        (d / "s.py").write_text("def target_positions(bars, params):\n    return [1.0] * len(bars)\n")
        return AgentRoundResult(candidates=[CandidateSubmission(plugin="s.py"),
                                            CandidateSubmission(plugin="../../escape.py")], done=True)

    ctl = ResearchController(paths, store, ScriptedRunner(agent))
    sid = ctl.create_session(_cfg(dataset))
    summary = ctl.run(sid)
    assert "docker" in summary["candidates"][0]["reasons"][0]

    sid2 = ctl.create_session(_cfg(dataset, allow_weak_isolation=True))
    summary2 = ctl.run(sid2)
    assert summary2["candidates"][0]["strategy_ref"].startswith("plugin:")
    assert "escapes" in summary2["candidates"][1]["reasons"][0]


def test_cost_budget_and_manual_resume(paths, store, dataset):
    ctl = ResearchController(paths, store, ManualRunner())
    cfg = _cfg(dataset, budget=Budget(max_rounds=5, max_model_cost_usd=0.01, patience_rounds=5),
               pricing=ModelPricing(model="m", input_per_mtok=5, output_per_mtok=5))
    sid = ctl.create_session(cfg)
    ctl.run(sid)
    assert store.get_session(sid)["state"] == SessionState.WAITING_FOR_AGENT
    round_dir = paths.sessions / sid / "round_001"
    assert (round_dir / "task.md").exists()
    (round_dir / "submission.json").write_text(json.dumps({
        "candidates": [{"template": "momentum", "parameters": {"lookback": 24}}],
        "usage": {"input_tokens": 1000, "output_tokens": 0}}))
    ctl.run(sid)  # resumes, processes round 1, then waits for round 2
    sess = store.get_session(sid)
    assert sess["usage"]["rounds"] == 1 and sess["state"] == SessionState.WAITING_FOR_AGENT
    (paths.sessions / sid / "round_002" / "submission.json").write_text(json.dumps({
        "candidates": [], "usage": {"input_tokens": 1000, "output_tokens": 0}}))
    ctl.run(sid)
    sess = store.get_session(sid)
    assert sess["state"] == SessionState.BUDGET_EXHAUSTED and "cost" in sess["stop_reason"]


def test_stop_request_and_tools_respect_permissions(paths, store, dataset, tmp_path):
    ctl = ResearchController(paths, store, ManualRunner())
    sid = ctl.create_session(_cfg(dataset, budget=Budget(max_experiments=1)))
    ctl.run(sid)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"template": "sma_cross", "parameters": {"fast": 5, "slow": 20}}))
    with pytest.raises(PermissionError):
        tool_backtest(paths, store, sid, spec, "holdout")
    with pytest.raises(PermissionError, match="dev segment"):
        tool_backtest(paths, store, sid, spec, "validation")
    assert tool_backtest(paths, store, sid, spec, "dev")["status"] == "completed"
    with pytest.raises(Exception, match="budget"):
        tool_backtest(paths, store, sid, spec, "dev")
    ctl.request_stop(sid)
    assert store.get_session(sid)["state"] == SessionState.STOPPED
    with pytest.raises(Exception, match="tools are disabled"):
        tool_backtest(paths, store, sid, spec, "dev")


def test_command_runner_scrubs_secrets(paths, store, dataset, monkeypatch):
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s3cret")
    monkeypatch.setenv("AGENT_API_KEY", "agent-key")
    script = (
        "import json, os, pathlib; d = pathlib.Path('.');"
        "json.dump({'candidates': [], 'done': True, 'report': json.dumps("
        "{'secret': os.environ.get('EXCHANGE_API_SECRET'), 'agent': os.environ.get('AGENT_API_KEY'),"
        " 'session': os.environ.get('ZQR_SESSION')})}, open(d / 'submission.json', 'w'))"
    )
    import sys

    runner = CommandRunner([sys.executable, "-c", script], pass_env=["AGENT_API_KEY"])
    ctl = ResearchController(paths, store, runner)
    sid = ctl.create_session(_cfg(dataset))
    summary = ctl.run(sid)
    env_seen = json.loads(summary["agent_reports"][0]["report"])
    assert env_seen == {"secret": None, "agent": "agent-key", "session": sid}


def test_agent_sees_only_dev_data(paths, store, dataset):
    _, split = dataset
    tasks = []
    ctl = ResearchController(paths, store, ScriptedRunner(lambda t: tasks.append(t) or AgentRoundResult(done=True)))
    ctl.run(ctl.create_session(_cfg(dataset)))
    task = tasks[0]
    assert task.allowed_segments == ["dev"] and "limited to the dev segment" in task.prompt()
    bars = pd.read_parquet(task.research_data_path)
    assert to_epoch(bars["bar_open_time"]).max() < split.dev[1]


CLAUDE_JSON = {"type": "result", "result": "done", "total_cost_usd": 0.5,
               "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7},
               "modelUsage": {"deepseek-flash": {"inputTokens": 100, "cacheReadInputTokens": 50,
                                                 "cacheCreationInputTokens": 0, "outputTokens": 30},
                              "deepseek-small": {"inputTokens": 1, "outputTokens": 2}}}


def test_usage_from_agent_output():
    u = usage_from_output(json.dumps(CLAUDE_JSON))
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (101, 50, 32) and "deepseek-flash" in u.model
    u = usage_from_output(json.dumps({k: v for k, v in CLAUDE_JSON.items() if k != "modelUsage"}))
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (10, 5, 7)
    assert usage_from_output("plain text") is None and usage_from_output("[1, 2]") is None
    cmd = claude_command("sonnet")
    assert cmd[:2] == ["claude", "-p"] and "--output-format" in cmd and cmd[-2:] == ["--model", "sonnet"]


def test_command_runner_measures_usage_over_self_report(paths, store, dataset):
    script = (
        "import json; json.dump({'candidates': [], 'done': True, 'usage': {'input_tokens': 0, 'output_tokens': 0}},"
        f" open('submission.json', 'w')); print(json.dumps({CLAUDE_JSON!r}))"
    )
    pricing = ModelPricing(model="x", input_per_mtok=1_000_000, output_per_mtok=0)  # $1 per input token
    ctl = ResearchController(paths, store, CommandRunner([sys.executable, "-c", script]))
    sid = ctl.create_session(_cfg(dataset, pricing=pricing))
    ctl.run(sid)
    usage = store.get_session(sid)["usage"]
    assert (usage["input_tokens"], usage["cache_read_tokens"], usage["output_tokens"]) == (101, 50, 32)
    # cache hits fall back to the input price when no cache price is given
    assert usage["model_cost_known"] and usage["model_cost_usd"] == pytest.approx(151)


def test_deepseek_pricing_cache_and_peak_schedule():
    p = pricing_preset("deepseek-flash", cny_per_usd=7.0)
    peak = datetime(2026, 10, 12, 10, 0, tzinfo=BEIJING).timestamp()  # Monday 10:00 Beijing
    lunch = datetime(2026, 10, 12, 12, 30, tzinfo=BEIJING).timestamp()
    weekend = datetime(2026, 10, 11, 10, 0, tzinfo=BEIJING).timestamp()  # Sunday
    # tokens from a real round: 63,811 uncached, 1,724,672 cache hits, 19,166 output
    cny = (63_811 * 2 + 1_724_672 * 0.04 + 19_166 * 8) / 1e6
    assert p.cost(63_811, 19_166, 1_724_672, ts=peak) == pytest.approx(cny / 7.0)
    assert p.cost(63_811, 19_166, 1_724_672, ts=lunch) == pytest.approx(cny / 7.0 / 2)
    assert p.cost(63_811, 19_166, 1_724_672, ts=weekend) == pytest.approx(cny / 7.0 / 2)
    flat = ModelPricing(model="m", input_per_mtok=2, output_per_mtok=8)
    assert flat.cost(1_000_000, 0, 1_000_000) == pytest.approx(4.0)
    with pytest.raises(ValueError):
        pricing_preset("nope")


def test_deepseek_pricing_over_a_span_is_peak_if_any_part_is():
    p = pricing_preset("deepseek-flash", cny_per_usd=7.0)

    def at(day, hour, minute=0):
        return datetime(2026, 10, day, hour, minute, tzinfo=BEIJING).timestamp()

    peak, off = 2 / 7.0, 1 / 7.0  # 1M uncached input tokens, Monday 2026-10-12 onwards
    assert p.cost(1_000_000, 0, ts=at(12, 17, 30), until=at(12, 18, 30)) == pytest.approx(peak)  # ends off-peak
    assert p.cost(1_000_000, 0, ts=at(12, 11, 59), until=at(12, 13)) == pytest.approx(peak)
    assert p.cost(1_000_000, 0, ts=at(12, 12, 10), until=at(12, 13, 50)) == pytest.approx(off)  # lunch only
    assert p.cost(1_000_000, 0, ts=at(12, 18, 30), until=at(13, 8, 59)) == pytest.approx(off)  # overnight
    assert p.cost(1_000_000, 0, ts=at(12, 18, 30), until=at(13, 9)) == pytest.approx(peak)
    assert p.cost(1_000_000, 0, ts=at(16, 19), until=at(19, 8)) == pytest.approx(off)  # Friday night to Monday
    assert p.cost(1_000_000, 0, ts=at(16, 19), until=at(19, 9, 30)) == pytest.approx(peak)
