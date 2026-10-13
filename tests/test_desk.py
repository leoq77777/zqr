"""Research desk: standing authorization, automatic promotion, research loop."""

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quant_research.bench.adapter import AgentRun
from quant_research.controller.agent import AgentRoundResult, CandidateSubmission, ScriptedRunner, TokenUsage
from quant_research.controller.controller import ResearchController, SessionConfig, SessionState
from quant_research.desk import promotion
from quant_research.desk import research as rl
from quant_research.desk.config import DeskConfig, check_path
from quant_research.gateway import ToolGateway
from quant_research.paper.fleet import load_roster, save_roster
from quant_research.research import authorization as auth_mod
from quant_research.research.authorization import AuthorizationError
from quant_research.research.registry import RegistryError, freeze, transition
from quant_research.strategy.sandbox import SandboxConfig
from tests.conftest import make_mandate

EASY_HOLDOUT = {"max_viewings": 3, "min_primary": -100.0, "min_total_return": -1.0}


def _validated(paths, store, dataset, template="buy_and_hold", params=None, **mandate_kw):
    agent = ScriptedRunner(lambda t: AgentRoundResult(
        candidates=[CandidateSubmission(template=template, parameters=params or {})], done=True))
    ctl = ResearchController(paths, store, agent)
    cfg = SessionConfig(mandate=make_mandate(dataset, **mandate_kw), sandbox=SandboxConfig(isolation="subprocess"))
    summary = ctl.run(ctl.create_session(cfg))
    return summary["candidates"][0]["candidate_id"]


def _authorize(paths, **kw):
    args = {"products": ["BTC-USD"], "max_paper_strategies": 2, "days": 7} | kw
    auth = auth_mod.create(**args)
    auth_mod.save(promotion.authorization_path(paths), auth)
    return auth


def test_authorization_seal_expiry_scope_and_product():
    now = time.time()
    auth = auth_mod.create(["BTC-USD"], 3, days=1, now=now)
    auth.check(now=now + 10, transition=("validated", "frozen"), product="BTC-USD")
    with pytest.raises(AuthorizationError, match="expired"):
        auth.check(now=now + 86401)
    with pytest.raises(AuthorizationError, match="not an authorized product"):
        auth.check(now=now, product="ETH-USD")
    with pytest.raises(AuthorizationError, match="outside the authorization scope"):
        auth.check(now=now, transition=("paper_approved", "paper_active"))
    tampered = auth.model_copy(update={"max_paper_strategies": 99})
    with pytest.raises(AuthorizationError, match="hash"):
        tampered.check(now=now)


def test_desk_actor_needs_a_valid_authorization(paths, store, dataset):
    cid = _validated(paths, store, dataset)
    with pytest.raises(RegistryError, match="standing authorization"):
        freeze(paths, store, cid, "", actor="desk")
    expired = auth_mod.create(["BTC-USD"], 1, days=1, now=time.time() - 2 * 86400)
    with pytest.raises(RegistryError, match="expired"):
        freeze(paths, store, cid, "", actor="desk", authorization=expired)
    with pytest.raises(RegistryError, match="not allowed for controller"):
        freeze(paths, store, cid, "", actor="controller")
    auth = auth_mod.create(["BTC-USD"], 1, days=1)
    cand = freeze(paths, store, cid, "", actor="desk", authorization=auth)
    assert cand["state"] == "frozen" and cand["evidence"]["authorization_hash"] == auth.hash
    event = store.candidate_events(cid)[-1]
    assert event["actor"] == "desk"
    with pytest.raises(RegistryError, match="not allowed for desk"):
        transition(store, cid, "final_evaluated", "desk", authorization=auth)  # only the evaluator decides


def test_promotion_runs_to_the_roster_and_activates(paths, store, dataset):
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, cid)["stopped"].startswith("no standing authorization")
    assert store.get_candidate(cid)["state"] == "validated"
    _authorize(paths)
    out = promotion.promote(paths, store, cid)
    assert out["stopped"] == "done", out
    assert out["steps"] == ["frozen", "final_evaluated", "paper_approved", "roster"]
    entry = load_roster(promotion.roster_path(paths)).entries[-1]
    assert entry.candidate_id == cid and entry.source == "candidate" and entry.product == "BTC-USD"
    promotion.activate_hook(paths)(entry)
    assert store.get_candidate(cid)["state"] == "paper_active"


def test_promotion_respects_product_cap_and_holdout_limit(paths, store, dataset):
    _authorize(paths, products=["ETH-USD"])
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert "not an authorized product" in promotion.promote(paths, store, cid)["stopped"]

    _authorize(paths, max_paper_strategies=1)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    cid2 = _validated(paths, store, dataset, template="momentum", params={"lookback": 24}, holdout=EASY_HOLDOUT)
    out = promotion.promote(paths, store, cid2)
    assert "authorization cap" in out["stopped"] and out["state"] == "validated"  # no holdout viewing spent

    _authorize(paths, max_paper_strategies=3)
    one_view = {**EASY_HOLDOUT, "max_viewings": 1}
    cid3 = _validated(paths, store, dataset, template="momentum", params={"lookback": 48}, holdout=one_view,
                      question="a different question")
    out = promotion.promote(paths, store, cid3)
    assert "already viewed" in out["stopped"] and out["state"] == "frozen"


def test_promotion_resumes_after_a_slot_frees(paths, store, dataset):
    _authorize(paths, max_paper_strategies=1)
    first = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, first)["stopped"] == "done"
    second = _validated(paths, store, dataset, template="momentum", params={"lookback": 24}, holdout=EASY_HOLDOUT)
    out = promotion.promote(paths, store, second)
    assert "authorization cap" in out["stopped"] and out["state"] == "validated"
    viewings = len(store.holdout_runs(dataset[1].split_id))
    rp = promotion.roster_path(paths)
    roster = load_roster(rp)
    roster.entries[0].enabled = False
    save_roster(rp, roster)
    out = promotion.promote(paths, store, second)
    assert out["stopped"] == "done" and out["state"] == "paper_approved"
    assert len(store.holdout_runs(dataset[1].split_id)) == viewings + 1


def test_promotion_is_idempotent_from_paper_approved(paths, store, dataset):
    _authorize(paths)
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    rp = promotion.roster_path(paths)
    roster = load_roster(rp)
    roster.entries = [e for e in roster.entries if e.candidate_id != cid]  # crashed before the roster write
    save_roster(rp, roster)
    viewings = len(store.holdout_runs(dataset[1].split_id))
    out = promotion.promote(paths, store, cid)
    assert out["stopped"] == "done" and out["steps"] == ["roster"]
    assert load_roster(rp).entries[-1].candidate_id == cid
    assert len(store.holdout_runs(dataset[1].split_id)) == viewings


# -- research loop -------------------------------------------------------------------------------------
PLUGIN = """
def target_positions(bars, params):
    ma = bars["close"].rolling(params["n"]).mean()
    return (bars["close"] > ma).astype(float)
"""


class FakeAgent:
    """Plays the agent through the real tool gateway: plan, one dev experiment, research.json."""

    name = "fake"

    def __init__(self, paths, store, behaviour="submit"):
        self.paths, self.store, self.behaviour, self.calls = paths, store, behaviour, 0

    def run(self, workspace, mcp_config, timeout_s, network=None, label=None, transcript_dir=None):
        self.calls += 1
        if self.behaviour == "env_fail":
            return AgentRun(adapter="fake", harness_version="fake", exit_code=1, timed_out=False, wall_seconds=0.1,
                            usage=None, stderr_tail="Cannot connect to the Docker daemon")
        if self.behaviour == "die":
            raise SystemExit("desk process killed")
        sid = next(s["id"] for s in self.store.list_sessions() if Path(s["workspace"]) == Path(workspace))
        assert "Earlier candidates" in (workspace / "TASK.md").read_text()
        gw = ToolGateway(self.paths, self.store, sid)
        (workspace / "s.py").write_text(PLUGIN)
        plan = gw.submit_plan({"hypothesis": "price above its moving average persists", "mechanism": "trend",
                               "falsification": "dev sharpe below zero", "artifact": "s.py",
                               "parameter_ranges": {"n": {"values": [24]}}, "planned_experiments": 1})
        self.store.log_session_event(sid, "tool_refused", {"tool": "submit_plan", "error": "invalid plan",
                                                           "policy": False})
        key = f"k{self.calls}"
        exp = gw.submit_experiment("/workspace/s.py", {"n": 24}, plan["plan_id"], idempotency_key=key)
        self.store.log_session_event(sid, "tool_call", {"tool": "submit_experiment",
                                                        "args": {"idempotency_key": key}})
        final = None if self.behaviour == "null" else {
            "path": "s.py", "sha256": hashlib.sha256(PLUGIN.encode()).hexdigest(), "parameters": {"n": 24},
            "experiment_id": exp["experiment_id"]}
        (workspace / "research.json").write_text(json.dumps({"research_id": label, "final_artifact": final}))
        return AgentRun(adapter="fake", harness_version="fake", exit_code=0, timed_out=False, wall_seconds=1.0,
                        usage=TokenUsage(model="deepseek-v4-flash", input_tokens=20_000, output_tokens=3_000),
                        extra={"models": ["deepseek-v4-flash"]})


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 12, 8, tzinfo=UTC).timestamp()

    def __call__(self):
        return self.t


def _loop(paths, store, dataset, agent, **cfg_kw):
    m = make_mandate(dataset, holdout=EASY_HOLDOUT)
    store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    cfg = DeskConfig(products=["BTC-USD"], mandates={"BTC-USD": m.mandate_id()}, **cfg_kw)
    clock = Clock()
    return rl.ResearchLoop(paths, store, cfg, agent, sandbox=SandboxConfig(isolation="subprocess"),
                           allow_weak_isolation=True, isolate_network=False, require_check=False,
                           clock=clock, sleep=lambda s: None), clock


def test_research_session_validates_and_promotes_to_the_roster(paths, store, dataset):
    _authorize(paths)
    loop, _ = _loop(paths, store, dataset, FakeAgent(paths, store))
    out = loop.step()
    res = out["result"]
    assert out["ran"] and res["failure_class"] == "none", res
    assert res["verdict"] == "accepted" and res["audit_ok"] and not res["violations"]
    assert res["promotion"]["stopped"] == "done"
    assert res["cost_usd"] > 0 and res["cost_source"] == "priced_usage" and res["experiments"] == 1
    cand = store.get_candidate(res["candidate_id"])
    assert cand["state"] == "paper_approved" and cand["evidence"]["authorization_hash"]
    entry = load_roster(promotion.roster_path(paths)).entries[-1]
    assert entry.candidate_id == res["candidate_id"] and entry.strategy_ref.startswith("plugin:")
    sess = store.get_session(res["session_id"])
    assert SessionState(sess["state"]) == SessionState.COMPLETED
    ledger = rl.read_ledger(paths)
    assert [r["run_id"] for r in ledger] == [res["run_id"]]
    assert (rl.sessions_dir(paths) / res["run_id"] / "submission" / "s.py").exists()

    out2 = loop.step()  # the identical artifact again keeps its first verdict
    assert out2["result"]["verdict"].startswith("resubmission")


def test_null_submission_missing_check_and_credentials(paths, store, dataset, monkeypatch, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")}
    monkeypatch.setattr(os, "environ", env)
    creds = tmp_path / "desk.env"
    monkeypatch.setattr(rl, "CREDENTIAL_FILE", creds)
    loop, _ = _loop(paths, store, dataset, FakeAgent(paths, store, "null"))
    assert loop.step()["result"]["verdict"] == "no_candidate"
    loop.require_check = True
    out = loop.step()
    assert not out["ran"] and "desk check" in out["reason"]
    check_path(paths).write_text(json.dumps({"isolation_ok": True}))
    out = loop.step()
    assert not out["ran"] and "no model credential" in out["reason"]
    creds.write_text("ANTHROPIC_AUTH_TOKEN=test-token\nUNRELATED=x\n")
    assert loop.step()["ran"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-token" and "UNRELATED" not in env


def test_environment_failures_pause_with_exponential_backoff(paths, store, dataset):
    agent = FakeAgent(paths, store, "env_fail")
    loop, clock = _loop(paths, store, dataset, agent, backoff_base_seconds=100, backoff_max_seconds=250)
    out = loop.step()
    assert out["status"] == "paused" and out["result"]["failure_class"] == "environment.docker"
    assert rl.load_state(paths)["resume_at"] == pytest.approx(clock.t + 100)
    assert not loop.step()["ran"] and agent.calls == 1
    clock.t += 101
    loop.step()
    assert rl.load_state(paths)["resume_at"] == pytest.approx(clock.t + 200)
    clock.t += 201
    loop.step()
    assert rl.load_state(paths)["resume_at"] == pytest.approx(clock.t + 250)  # capped
    agent.behaviour = "null"
    clock.t += 251
    assert loop.step()["status"] == "running"
    assert rl.load_state(paths)["consecutive_failures"] == 0


def test_daily_cost_cap_stops_new_sessions_until_tomorrow(paths, store, dataset):
    agent = FakeAgent(paths, store, "null")
    loop, clock = _loop(paths, store, dataset, agent, daily_cost_cap_usd=1.0)
    rl.ledger_path(paths).write_text(json.dumps({"day": rl.utc_day(clock.t), "cost_usd": 1.2}) + "\n")
    out = loop.step()
    assert not out["ran"] and out["status"] == "cost_cap" and agent.calls == 0
    assert 0 < out["wait"] <= 16 * 3600 + 1
    clock.t += out["wait"]
    assert loop.step()["ran"]


def test_recover_closes_sessions_of_a_dead_desk_process(paths, store, dataset):
    loop, _ = _loop(paths, store, dataset, FakeAgent(paths, store, "die"))
    with pytest.raises(SystemExit):
        loop.step()
    run_id = rl.load_state(paths)["current_run"]
    assert run_id and not (rl.sessions_dir(paths) / run_id / "result.json").exists()
    out = rl.recover_desk_runs(paths, store, loop.cfg)
    assert [r["run_id"] for r in out] == [run_id] and out[0]["failure_class"] == "harness.crash"
    res = json.loads((rl.sessions_dir(paths) / run_id / "result.json").read_text())
    assert res["audit_ok"]
    assert SessionState(store.get_session(res["session_id"])["state"]) != SessionState.RUNNING
    assert rl.load_state(paths)["current_run"] is None


def test_stop_request_ends_the_running_session_and_the_loop(paths, store, dataset):
    agent = FakeAgent(paths, store, "env_fail")
    loop, _ = _loop(paths, store, dataset, agent)
    real_run = agent.run

    def run_then_stop(*a, **kw):
        loop.request_stop()  # what SIGTERM does mid-session
        return real_run(*a, **kw)

    agent.run = run_then_stop
    loop.run()
    res = rl.read_ledger(paths)[-1]
    assert res["failure_class"] == "desk.stopped" and agent.calls == 1
    assert rl.load_state(paths)["status"] == "stopped"


def test_promotion_carries_the_validated_capital_and_costs(paths, store, dataset):
    engine = {"initial_cash": 20_000.0, "costs": {"fee_rate": 0.001, "slippage_bps": 2.0}}
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config=engine)
    _authorize(paths)
    assert promotion.promote(paths, store, cid)["stopped"] == "done"
    entry = load_roster(promotion.roster_path(paths)).entries[-1]
    assert (entry.initial_cash, entry.fee_rate, entry.slippage_bps) == (20_000.0, 0.001, 2.0)


def test_promotion_refuses_settings_paper_cannot_reproduce(paths, store, dataset):
    cid = _validated(paths, store, dataset, holdout=EASY_HOLDOUT, engine_config={"execution_delay_bars": 1})
    _authorize(paths)
    out = promotion.promote(paths, store, cid)
    assert "execution_delay_bars=1" in out["stopped"] and out["state"] == "validated"
