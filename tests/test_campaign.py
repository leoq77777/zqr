"""Campaigns (release 4d): store and quotas, budget reservation, the research loop on campaigns, gateway tools,
conclusions and the archive write-back, the generator, the report and the CLI."""

import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quant_research.bench.adapter import AgentRun
from quant_research.campaign import budget
from quant_research.campaign import store as cstore
from quant_research.campaign.model import CampaignSpec
from quant_research.campaign.store import CampaignError, CampaignStore
from quant_research.controller.agent import TokenUsage
from quant_research.controller.controller import SessionState
from quant_research.desk import research as rl
from quant_research.desk.config import check_path, load_config, save_config
from quant_research.gateway import ToolGateway
from quant_research.gateway.service import GatewayError, PolicyRefusal
from quant_research.hypothesis import gates
from quant_research.hypothesis.store import HypothesisError, HypothesisStore
from quant_research.strategy.sandbox import SandboxConfig
from tests.test_hypothesis import _hyp, _reference

PLUGIN = """
def target_positions(bars, params):
    ma = bars["close"].rolling(params["n"]).mean()
    return (bars["close"] > ma).astype(float)
"""


def _spec(**kw):
    body = {"product": "BTC-USD", "family": "tsmom", "goal": "Find a short-lookback tsmom variant for range markets.",
            "prior": "Short lookbacks react faster to reversals.",
            "citations": [{"kind": "published", "ref": "Moskowitz, Ooi, Pedersen (2012)"}]}
    return CampaignSpec.model_validate(body | kw)


def _queued(paths, store, **kw) -> str:
    cs = CampaignStore(store)
    cid = cs.add(paths, _spec(**kw), {"kind": "human", "note": "test"}, "human")["id"]
    cs.transition(cid, "queued", "human", "looks fine")
    return cid


class FakeAgent:
    """Plays a campaign session through the real tool gateway. Script actions: explore (one dev experiment and a
    marginal evaluation), register (also registers a template variant), conclude (also writes the conclusion),
    null (no notes), env_fail, die."""

    name = "fake"

    def __init__(self, paths, store, script=("explore",), model="deepseek-v4-flash", tokens=20_000):
        self.paths, self.store, self.script, self.model, self.tokens = paths, store, list(script), model, tokens
        self.usage_model = None
        self.calls, self.briefs, self.outputs, self.reports, self.refusals = 0, [], [], [], []

    def run(self, workspace, mcp_config, timeout_s, network=None, label=None, transcript_dir=None):
        self.calls += 1
        action = self.script[min(self.calls, len(self.script)) - 1]
        if action == "env_fail":
            return AgentRun(adapter="fake", harness_version="fake", exit_code=1, timed_out=False, wall_seconds=0.1,
                            usage=None, stderr_tail="Cannot connect to the Docker daemon")
        if action == "die":
            raise SystemExit("desk process killed")
        sid = next(s["id"] for s in self.store.list_sessions() if Path(s["workspace"]) == Path(workspace))
        self.briefs.append((workspace / "TASK.md").read_text())
        gw = ToolGateway(self.paths, self.store, sid)
        (workspace / "s.py").write_text(PLUGIN)
        plan = gw.submit_plan({"hypothesis": "price above its moving average persists", "mechanism": "trend",
                               "falsification": "dev sharpe below zero", "artifact": "s.py",
                               "parameter_ranges": {"n": {"values": [24]}}, "planned_experiments": 1})
        exp = gw.submit_experiment("/workspace/s.py", {"n": 24 + self.calls}, plan["plan_id"],
                                   idempotency_key=f"k{self.calls}")
        self.store.log_session_event(sid, "tool_call", {"tool": "submit_experiment",
                                                        "args": {"idempotency_key": f"k{self.calls}"}})
        eid = exp["experiment_id"]
        self.outputs.append(exp)
        try:
            self.outputs.append(gw.marginal_eval(eid))
        except PolicyRefusal as exc:
            self.refusals.append(str(exc))
        self.outputs += [gw.portfolio_members(), gw.history(), gw.get_experiment(eid)]
        with contextlib.suppress(GatewayError):
            self.outputs.append(gw.family_archive("tsmom"))
        if action == "register":
            self.outputs.append(gw.register_hypothesis(
                "Short lookbacks hedge the slow sleeves.", [{"kind": "experiment", "ref": eid}],
                [{"artifact": "momentum", "parameters": {"lookback": 48}}]))
        conclusion = {"verdict": "refuted", "summary": "The variant adds nothing the sleeves lack.",
                      "evidence": [eid], "open_questions": ["Does a 96 h lookback behave the same?"]}
        doc = {"research_id": label, "task_id": "campaign", "hypothesis": "short lookbacks hedge",
               "preregistered_plan": {}, "experiment_ids": [eid], "strategy_artifacts": [],
               "decision": "continue", "limitations": [], "sources": [], "unresolved": [], "final_artifact": None,
               "conclusion": conclusion if action == "conclude" else None}
        if action != "null":
            doc["notes"] = f"session {self.calls}: ran {eid}"
        (workspace / "research.json").write_text(json.dumps(doc))
        self.reports.append(gw.build_report())
        used = self.usage_model or self.model
        return AgentRun(adapter="fake", harness_version="fake", exit_code=0, timed_out=False, wall_seconds=1.0,
                        usage=TokenUsage(model=used, input_tokens=self.tokens, output_tokens=3_000),
                        extra={"models": [used]})


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 12, 8, tzinfo=UTC).timestamp()

    def __call__(self):
        return self.t


def _loop(paths, store, agent, **cfg_kw):
    cfg = load_config(paths).model_copy(update={"model": "deepseek-v4-flash", **cfg_kw})
    clock = Clock()
    return rl.ResearchLoop(paths, store, cfg, agent, sandbox=SandboxConfig(isolation="subprocess"),
                           allow_weak_isolation=True, isolate_network=False, require_check=False,
                           clock=clock, sleep=lambda s: None), clock


def _archive(paths):
    from quant_research.memory.archive import _write

    _write(paths, "tsmom", {"family": "tsmom", "name": "Time-series momentum", "thesis": "Trends persist.",
                            "sleeve": True, "splits": {}, "conclusions": []})


def _force_gates(monkeypatch, gate1=True, gate2=True):
    real = gates.appraisal_test
    monkeypatch.setattr(gates, "appraisal_test", lambda c, p, k: real(c, p, k) | {"passed": gate1})
    monkeypatch.setattr(gates, "gate2_checks", lambda r, c: {"sharpe_not_lower": gate2,
                                                              "drawdown_within_2_points": True,
                                                              "turnover_within_25_percent": True})


# -- D1 store ------------------------------------------------------------------------------------------------

def test_campaign_states_need_the_right_actor_and_one_runs_at_a_time(paths, store, dataset):
    _reference(paths, store, dataset)
    cs = CampaignStore(store)
    row = cs.add(paths, _spec(), {"kind": "human", "note": "x"}, "human")
    assert row["state"] == "proposed" and row["split_id"] == dataset[1].split_id
    with pytest.raises(CampaignError, match="needs a note"):
        cs.transition(row["id"], "queued", "human", " ")
    auto = cs.add(paths, _spec(family="ewma_trend", goal="Auto-queue a generator campaign for the floor."),
                  {"kind": "generator", "key": "t"}, "controller")
    cs.transition(auto["id"], "queued", "controller")
    assert cs.require(auto["id"])["state"] == "queued"
    cs.transition(auto["id"], "cancelled", "human", "not this test")
    with pytest.raises(CampaignError, match="cannot become running"):
        cs.transition(row["id"], "running", "controller")
    cs.transition(row["id"], "queued", "human", "ok")
    other = _queued(paths, store, family="trend_filter")
    assert cs.next_queued()["id"] == row["id"]
    cs.transition(row["id"], "running", "controller")
    with pytest.raises(CampaignError, match="one campaign runs at a time"):
        cs.transition(other, "running", "controller")
    cs.transition(row["id"], "cancelled", "human", "changed my mind")
    assert [e["event"] for e in cs.events(row["id"])] == ["proposed", "queued", "running", "cancelled"]
    assert cs.next_queued()["id"] == other


@pytest.mark.parametrize("change, message", [
    ({"family": "vol_overlay"}, "not a sleeve family"),
    ({"model": "gpt-x"}, "has no price"),
    ({"citations": [{"kind": "experiment", "ref": "exp-nope"}]}, "does not exist"),
    ({"product": "ETH-USD"}, "no desk mandate"),
])
def test_campaign_refusals(paths, store, dataset, change, message):
    _reference(paths, store, dataset)
    with pytest.raises(CampaignError, match=message):
        CampaignStore(store).add(paths, _spec(**change), {"kind": "human"}, "human")
    assert CampaignStore(store).add(paths, _spec(family="exploration"), {"kind": "human"}, "human")


def test_a_closed_holdout_refuses_holdout_runs_but_not_campaigns(paths, store, dataset):
    from quant_research.validation.holdout import closed_refusal

    _reference(paths, store, dataset)
    store.close_holdout(dataset[1].split_id, "used up")
    cid = _queued(paths, store)
    agent = FakeAgent(paths, store)
    loop, _ = _loop(paths, store, agent)
    assert loop.step()["ran"] and agent.calls == 1
    assert CampaignStore(store).sessions(cid)[0]["failure_class"] == "none"
    assert "closed" in closed_refusal(store, dataset[1].split_id)


def test_caps_grow_with_top_ups_up_to_the_maximum(paths, store, dataset):
    _reference(paths, store, dataset)
    cs = CampaignStore(store)
    cid = _queued(paths, store)
    assert cs.caps(cid) == {"usd": 1.0, "sessions": 4, "experiments": 40}
    assert cs.topup(cid, "gate1") and not cs.topup(cid, "gate1")
    assert cs.caps(cid) == {"usd": 3.0, "sessions": 6, "experiments": 60}
    cs.topup(cid, "gate2")
    assert cs.caps(cid) == {"usd": 5.0, "sessions": 8, "experiments": 80}
    with pytest.raises(CampaignError):
        cs.topup(cid, "more")


# -- D4 loop, D5 tools, D6 conclusion: end to end --------------------------------------------------------------

def test_campaign_end_to_end_registration_gates_conclusion_and_archive(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    _archive(paths)
    _force_gates(monkeypatch)
    monkeypatch.setattr("quant_research.campaign.generate.generate", lambda *a, **k: [])
    cid = _queued(paths, store)
    agent = FakeAgent(paths, store, ["explore", "register", "conclude"])
    loop, _ = _loop(paths, store, agent)
    cs = CampaignStore(store)

    res = loop.step()["result"]
    assert res["failure_class"] == "none" and res["campaign_id"] == cid and res["reserved_usd"] == 0.25
    assert cs.require(cid)["state"] == "running" and agent.reports[-1]["valid"], agent.reports
    assert "Find a short-lookback tsmom variant" in agent.briefs[0] and "(first session)" in agent.briefs[0]
    assert cs.usage(cid) == {"usd": res["cost_usd"], "sessions": 1, "experiments": 2}  # one run + one marginal

    res = loop.step()["result"]
    assert res["campaign"]["gates"]["state"] == "admitted"
    assert cs.caps(cid) == {"usd": 5.0, "sessions": 8, "experiments": 80}
    assert "session 1: ran" in agent.briefs[1]
    hid = res["campaign"]["gates"]["hypothesis_id"]
    assert HypothesisStore(store).get(hid)["campaign_id"] == cid

    res = loop.step()["result"]
    assert "**admitted**" in agent.briefs[2]
    end = res["campaign"]["end"]
    assert end["state"] == "concluded" and end["citations_ok"] and end["archive_appended"]
    assert end["written_by"] == "session"
    archive = json.loads((paths.library / "archives" / "tsmom.json").read_text())
    entry = archive["conclusions"][-1]
    assert entry["session_id"] == f"campaign:{cid}" and entry["gates"] == "admitted"
    assert entry["decision"] == "refuted" and entry["unresolved"] == ["Does a 96 h lookback behave the same?"]
    assert "Gates: admitted." in (paths.library / "archives" / "tsmom.md").read_text()
    ledger = rl.read_ledger(paths)
    assert [(r["gates"], r["campaign_end"]) for r in ledger] == [(None, None), ("admitted", None),
                                                                 (None, "concluded")]

    # agents never see validation numbers: scan every tool output and brief
    g2 = HypothesisStore(store).event(hid, "gate2")["evidence"]
    secrets = [r["id"] for r in store.query("SELECT id FROM experiments WHERE segment != 'dev'")]
    secrets += [f"{v:.4f}" for side in ("reference", "candidate") for v in g2[side].values()]
    payload = json.dumps(agent.outputs, default=str) + "".join(agent.briefs)
    assert secrets and not [s for s in secrets if s in payload]

    out = loop.step()
    assert not out["ran"] and out["status"] == "idle" and agent.calls == 3


def test_session_cap_exhausts_the_campaign_with_a_controller_conclusion(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr("quant_research.campaign.generate.generate", lambda *a, **k: [])
    cid = _queued(paths, store)
    agent = FakeAgent(paths, store, ["explore"])
    loop, _ = _loop(paths, store, agent)
    for _ in range(4):
        res = loop.step()["result"]
    assert "last session" in agent.briefs[3] and "last session" not in agent.briefs[2]
    end = res["campaign"]["end"]
    assert end["state"] == "exhausted" and end["written_by"] == "controller"
    from quant_research.memory.conclusions import conclusions

    rec = next(c for c in conclusions(store) if c["session_id"] == f"campaign:{cid}")
    assert rec["body"]["decision"] == "inconclusive" and "session cap" in rec["body"]["reason"]
    assert "session 4: ran" in rec["body"]["hypothesis"]
    assert not loop.step()["ran"] and agent.calls == 4


def test_family_quota_ends_campaigns_and_refuses_registration(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr(cstore, "FAMILY_QUOTA", 2)
    cid = _queued(paths, store)
    loop, _ = _loop(paths, store, FakeAgent(paths, store))
    res = loop.step()["result"]
    cs = CampaignStore(store)
    assert cs.quota(paths, dataset[1].split_id, "tsmom") | {"room": 0.0} == cs.quota(paths, dataset[1].split_id,
                                                                                       "tsmom")
    assert res["campaign"]["end"]["state"] == "exhausted" and "family quota" in cs.event(cid, "exhausted")["data"][
        "reason"]
    with pytest.raises(CampaignError, match="at its quota"):
        cs.add(paths, _spec(), {"kind": "human"}, "human")
    with pytest.raises(HypothesisError, match="no room for 1 variant"):
        HypothesisStore(store).register(paths, _hyp(), "human")
    assert cs.quota(paths, dataset[1].split_id, "exploration")["quota"] == 0.25 * 2 * 4


def test_family_quota_is_enforced_per_action(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr(cstore, "FAMILY_QUOTA", 1)
    cid = _queued(paths, store)
    agent = FakeAgent(paths, store)
    loop, _ = _loop(paths, store, agent)
    loop.step()
    cs = CampaignStore(store)
    assert cs.sessions(cid)[0]["allowance"] == 1
    assert agent.refusals == ["the campaign's trial cap or its family's quota is reached"]
    assert cs.quota(paths, dataset[1].split_id, "tsmom")["used"] == 1


def test_family_quota_counts_the_parent_split(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    loop, _ = _loop(paths, store, FakeAgent(paths, store))
    loop.step()
    cs, sid = CampaignStore(store), dataset[1].split_id
    used = cs.quota(paths, sid, "tsmom")["used"]
    assert used == 2 and cs.quota(paths, "split-revalidated", "tsmom")["used"] == 0
    store.record_lineage("split-revalidated", sid, "A13.8")
    assert cs.quota(paths, "split-revalidated", "tsmom")["used"] == used


def test_registration_reserves_room_for_every_variant(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr(cstore, "FAMILY_QUOTA", 1)
    two = [{"strategy_ref": "template:momentum", "parameters": {"lookback": n}} for n in (48, 96)]
    with pytest.raises(HypothesisError, match="no room for 2 variant"):
        HypothesisStore(store).register(paths, _hyp(variants=two), "human")
    assert HypothesisStore(store).register(paths, _hyp(), "human")["state"] == "registered"


def test_idle_without_a_campaign_makes_no_model_call(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr("quant_research.campaign.generate.generate", lambda *a, **k: [])
    agent = FakeAgent(paths, store)
    loop, _ = _loop(paths, store, agent)
    CampaignStore(store).add(paths, _spec(), {"kind": "human"}, "human")  # proposed, not approved
    out = loop.step()
    assert out["status"] == "idle" and agent.calls == 0


def test_first_session_uses_the_design_model(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr("quant_research.campaign.generate.generate", lambda *a, **k: [])
    _queued(paths, store)
    agent = FakeAgent(paths, store)
    loop, _ = _loop(paths, store, agent, design_model="deepseek-v4-pro")
    res = loop.step()["result"]
    assert agent.model == "deepseek-v4-pro" and res["reserved_usd"] == 0.75


def test_generator_campaigns_auto_queue_when_the_floor_is_empty(paths, store, dataset, monkeypatch):
    _reference(paths, store, dataset)
    monkeypatch.setattr("quant_research.campaign.generate.generate", lambda *a, **k: [])
    cs = CampaignStore(store)
    cid = cs.add(paths, _spec(), {"kind": "generator", "key": "regime:2017"}, "controller")["id"]
    human = cs.add(paths, _spec(family="trend_filter", goal="A human-added campaign stays proposed."),
                   {"kind": "human", "note": "x"}, "human")["id"]
    agent = FakeAgent(paths, store)
    loop, _ = _loop(paths, store, agent)
    out = loop.step()
    assert out["ran"] and cs.require(cid)["state"] == "running" and cs.require(human)["state"] == "proposed"
    assert agent.calls == 1


# -- D3 budget -----------------------------------------------------------------------------------------------

def test_day_month_and_session_caps_are_ceilings(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    agent = FakeAgent(paths, store)
    loop, clock = _loop(paths, store, agent, daily_cost_cap_usd=20.0, max_sessions_per_day=200)
    day = rl.utc_day(clock.t)
    rl.ledger_path(paths).write_text(json.dumps({"day": day, "cost_usd": 4.8}) + "\n")
    out = loop.step()
    assert out["status"] == "cost_cap" and "daily cap" in out["reason"] and agent.calls == 0
    assert 0 < out["wait"] <= 16 * 3600 + 1
    rl.ledger_path(paths).write_text("".join(json.dumps({"day": day, "cost_usd": 0.0}) + "\n" for _ in range(12)))
    out = loop.step()
    assert out["status"] == "cost_cap" and "sessions per day (12)" in out["reason"]
    rl.ledger_path(paths).write_text(json.dumps({"day": "2026-10-01", "cost_usd": 83.2}) + "\n")
    out = loop.step()
    assert out["status"] == "cost_cap" and "monthly cap" in out["reason"]
    assert datetime.fromtimestamp(clock.t + out["wait"], tz=UTC).strftime("%Y-%m-%d") == "2026-11-01"
    rl.ledger_path(paths).write_text("")
    assert loop.step()["ran"]


def test_run_wakes_when_the_clock_passes_resume_at(paths, store, dataset):
    """A long cost-cap wait is until resume_at, not a counted number of 5 s sleeps."""
    _reference(paths, store, dataset)
    _queued(paths, store)
    agent = FakeAgent(paths, store)
    clock = Clock()
    n = {"sleeps": 0}

    def sleep(seconds):
        n["sleeps"] += 1
        if n["sleeps"] == 1:
            clock.t = rl.load_state(paths)["resume_at"] + 60
        else:
            clock.t += seconds

    cfg = load_config(paths).model_copy(update={"model": "deepseek-v4-flash"})
    loop = rl.ResearchLoop(paths, store, cfg, agent, sandbox=SandboxConfig(isolation="subprocess"),
                           allow_weak_isolation=True, isolate_network=False, require_check=False,
                           clock=clock, sleep=sleep)
    rl.ledger_path(paths).write_text(json.dumps({"day": rl.utc_day(clock.t), "cost_usd": 4.8,
                                                "topic": "campaign"}) + "\n")
    loop.run(max_sessions=1)
    assert agent.calls == 1 and n["sleeps"] == 1


def test_campaign_dollar_cap_refuses_a_reservation_that_does_not_fit(paths, store, dataset):
    _reference(paths, store, dataset)
    cid = _queued(paths, store)
    cs = CampaignStore(store)
    cs.transition(cid, "running", "controller")
    cs.start_session(cid, "sess-old", "run-old", 10, 0.25, False)
    cs.finish_session("sess-old", 0.9, 0, "spent most of it", "none")
    agent = FakeAgent(paths, store)
    loop, _ = _loop(paths, store, agent)
    out = loop.step()
    assert not out["ran"] and out["campaign"]["end"]["state"] == "exhausted" and agent.calls == 0
    assert "dollar cap" in cs.event(cid, "exhausted")["data"]["reason"]


def test_overrun_and_unpriced_calls_stop_the_loop_until_cleared(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    agent = FakeAgent(paths, store, tokens=1_000_000)  # about 0.29 USD against a 0.25 reservation
    loop, _ = _loop(paths, store, agent)
    assert loop.step()["ran"]
    assert "above its reservation" in budget.active_stop(paths)["reason"]
    out = loop.step()
    assert out["status"] == "budget_stop" and agent.calls == 1
    with pytest.raises(ValueError, match="needs a note"):
        budget.clear_stop(paths, "")
    assert budget.clear_stop(paths, "reserve raised")["status"] == "cleared"
    agent.tokens, agent.usage_model = 20_000, "mystery-model"
    res = loop.step()["result"]
    assert res["cost_source"] == "unpriced" and res["cost_usd"] == res["reserved_usd"] == 0.25
    assert "mystery-model" in budget.active_stop(paths)["reason"]
    budget.clear_stop(paths, "priced")
    loop.cfg = loop.cfg.model_copy(update={"model": "mystery-model"})
    out = loop.step()
    assert out["status"] == "blocked" and "has no price" in out["reason"] and agent.calls == 2
    loop.cfg = loop.cfg.model_copy(update={"campaign_models": {"mystery-model": "deepseek-flash"}})
    assert loop.step()["result"]["cost_source"] == "priced_usage"


# -- failures, recovery, stop (from the topic loop's tests) ---------------------------------------------------

def test_missing_notes_check_and_credentials(paths, store, dataset, monkeypatch, tmp_path):
    _reference(paths, store, dataset)
    cid = _queued(paths, store)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")}
    monkeypatch.setattr(os, "environ", env)
    creds = tmp_path / "desk.env"
    monkeypatch.setattr(rl, "CREDENTIAL_FILE", creds)
    agent = FakeAgent(paths, store, ["null", "explore"])
    loop, _ = _loop(paths, store, agent)
    assert loop.step()["ran"]
    assert "notes: a non-empty string is required" in " ".join(agent.reports[0]["problems"])
    assert CampaignStore(store).sessions(cid)[0]["notes"] is None
    loop.require_check = True
    out = loop.step()
    assert not out["ran"] and "desk check" in out["reason"]
    check_path(paths).write_text(json.dumps({"isolation_ok": True}))
    out = loop.step()
    assert not out["ran"] and "no model credential" in out["reason"]
    creds.write_text("ANTHROPIC_AUTH_TOKEN=test-token\nUNRELATED=x\n")
    assert loop.step()["ran"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-token" and "UNRELATED" not in env


def test_environment_failures_back_off_and_do_not_use_up_sessions(paths, store, dataset):
    _reference(paths, store, dataset)
    cid = _queued(paths, store)
    agent = FakeAgent(paths, store, ["env_fail", "env_fail", "env_fail", "explore"])
    loop, clock = _loop(paths, store, agent, backoff_base_seconds=100, backoff_max_seconds=250)
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
    clock.t += 251
    assert loop.step()["status"] == "running"
    assert rl.load_state(paths)["consecutive_failures"] == 0
    assert CampaignStore(store).usage(cid)["sessions"] == 1


def test_recover_closes_sessions_of_a_dead_desk_process(paths, store, dataset):
    _reference(paths, store, dataset)
    cid = _queued(paths, store)
    loop, _ = _loop(paths, store, FakeAgent(paths, store, ["die"]))
    with pytest.raises(SystemExit):
        loop.step()
    run_id = rl.load_state(paths)["current_run"]
    assert run_id and not (rl.sessions_dir(paths) / run_id / "result.json").exists()
    out = rl.recover_desk_runs(paths, store, loop.cfg)
    assert [r["run_id"] for r in out] == [run_id] and out[0]["failure_class"] == "harness.crash"
    res = json.loads((rl.sessions_dir(paths) / run_id / "result.json").read_text())
    assert res["audit_ok"] and res["campaign_id"] == cid
    assert SessionState(store.get_session(res["session_id"])["state"]) != SessionState.RUNNING
    assert CampaignStore(store).sessions(cid)[0]["failure_class"] == "harness.crash"
    assert rl.load_state(paths)["current_run"] is None


def test_stop_request_ends_the_running_session_and_the_loop(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    agent = FakeAgent(paths, store, ["env_fail"])
    loop, _ = _loop(paths, store, agent)
    real_run = agent.run

    def run_then_stop(*a, **kw):
        loop.request_stop()  # what SIGTERM does mid-session
        return real_run(*a, **kw)

    agent.run = run_then_stop
    loop.run()
    res = rl.read_ledger(paths)[-1]
    assert res["failure_class"] == "desk.stopped" and agent.calls == 1
    assert rl.load_state(paths)["status"] == "stopped"


# -- D5 gateway refusals -------------------------------------------------------------------------------------

def test_campaign_tools_refuse_outside_their_scope(paths, store, dataset):
    from quant_research.controller.agent import ManualRunner
    from quant_research.controller.controller import ResearchController, SessionConfig
    from quant_research.research.mandate import Mandate

    _reference(paths, store, dataset)
    mandate = Mandate.model_validate(store.get_mandate(load_config(paths).mandates["BTC-USD"])["body"])
    sid = ResearchController(paths, store, ManualRunner()).create_session(
        SessionConfig(mandate=mandate, sandbox=SandboxConfig(isolation="subprocess"), allow_weak_isolation=True))
    gw = ToolGateway(paths, store, sid)
    with pytest.raises(PolicyRefusal, match="campaign sessions only"):
        gw.register_hypothesis("p", [{"kind": "published", "ref": "x"}], [{"artifact": "momentum"}])
    with pytest.raises(GatewayError, match="invalid family"):
        gw.family_archive("../secrets")
    with pytest.raises(GatewayError, match="no archive"):
        gw.family_archive("tsmom")
    with pytest.raises(GatewayError, match="unknown experiment"):
        gw.marginal_eval("exp-nope")
    assert gw.portfolio_members()["reference_id"].startswith("pv-")

    cs = CampaignStore(store)
    cid = _queued(paths, store, family="exploration")
    cs.transition(cid, "running", "controller")
    cs.start_session(cid, sid, "run-x", 1, 0.25, False)
    with pytest.raises(PolicyRefusal, match="exploration campaigns"):
        gw.register_hypothesis("p", [{"kind": "published", "ref": "x"}], [{"artifact": "momentum"}])
    store.update_session(sid, state=SessionState.RUNNING)
    gw.submit_experiment("momentum", {"lookback": 24}, idempotency_key="a")
    second = gw.submit_experiment("momentum", {"lookback": 30}, idempotency_key="b")
    assert second["status"] == "blocked" and gw.history()["remaining_experiments"] == 0


# -- D2 generator --------------------------------------------------------------------------------------------

def _diag(paths, store, dataset, **kw):
    ref = _reference(paths, store, dataset)
    exp = store.query("SELECT id FROM experiments WHERE kind='portfolio'")[0]["id"]
    years = [{"period": "part", "from": f"{y}-01-01T00:00:00Z", "total_return": tr, "exposure": 0.5,
              "buy_and_hold": {"total_return": bh}}
             for y, tr, bh in ((2021, 0.10, 0.60), (2022, -0.05, -0.30), (2023, 0.0, 0.40))]
    archives = {"tsmom": {"splits": {dataset[1].split_id: {
        "cost": {"sharpe_1x": 1.0, "sharpe_2x": 0.4, "experiment_id": exp},
        "representative": {"experiment_id": exp}, "correlation": {"ewma_trend": 0.8}}}}}
    d = {"product": "BTC-USD", "split_id": dataset[1].split_id, "reference_id": ref, "reference_experiment": exp,
         "years": years, "family_contribution": {"tsmom": 0.3, "ewma_trend": 0.1, "none": -0.01},
         "archives": archives, "sleeves": ["channel_breakout", "ewma_trend", "trend_filter", "tsmom"]} | kw
    return d


def test_generator_ranks_deterministically_mixes_exploration_and_skips_live_keys(paths, store, dataset, monkeypatch):
    from quant_research.campaign import generate

    d = _diag(paths, store, dataset)
    monkeypatch.setattr(generate, "diagnostics", lambda *a: d)
    first = generate.candidates(paths, store, "BTC-USD")
    assert first == generate.candidates(paths, store, "BTC-USD")
    keys = {c["key"]: c for c in first}
    assert set(keys) == {"regime:2021", "regime:2023", "concentration", "cost:tsmom"}
    # log shortfalls 0.1823 (2023) and 0.1671 (2021) go round the families with no P&L share first
    assert (keys["regime:2023"]["gap"], keys["regime:2023"]["family"]) == (0.1823, "channel_breakout")
    assert (keys["regime:2021"]["gap"], keys["regime:2021"]["family"]) == (0.1671, "trend_filter")
    assert keys["cost:tsmom"]["gap"] == 0.6 and keys["concentration"]["family"] == "exploration"
    assert [c["score"] for c in first] == sorted((c["score"] for c in first), reverse=True)
    batch = generate.batch(paths, store, "BTC-USD", n=2)
    assert [c["family"] == "exploration" for c in batch].count(True) == 1
    written = generate.generate(paths, store, "BTC-USD", n=6)
    assert len(written) == 4 and all(CampaignStore(store).require(w["id"])["state"] == "proposed" for w in written)
    assert generate.generate(paths, store, "BTC-USD", n=6) == []  # every key is live
    cites = CampaignStore(store).require(written[0]["id"])["body"]["citations"]
    assert cites[0] == {"kind": "experiment", "ref": d["reference_experiment"]}


def test_generator_leaves_out_families_at_quota(paths, store, dataset, monkeypatch):
    from quant_research.campaign import generate

    d = _diag(paths, store, dataset)
    monkeypatch.setattr(generate, "diagnostics", lambda *a: d)
    real = CampaignStore.quota
    monkeypatch.setattr(CampaignStore, "quota", lambda self, p, s, f: real(self, p, s, f) | (
        {"room": 0.0} if f == "tsmom" else {}))
    assert "cost:tsmom" not in {c["key"] for c in generate.candidates(paths, store, "BTC-USD")}


def test_open_question_filters_drop_a6_blind_and_gate2_retries():
    from quant_research.campaign.generate import (
        gate2_retry_open_question,
        open_question_allowed,
        unanswerable_open_question,
    )

    assert unanswerable_open_question(
        "Which gate-2 component failed (Sharpe, drawdown or turnover) - needed before any redesign")
    assert unanswerable_open_question("What was the validation Sharpe of the registered variant?")
    assert unanswerable_open_question(
        "Can the deep drawdown be reduced without giving up 2017, the binding gate-2 constraint?")
    assert not unanswerable_open_question(
        "Is the BTC hourly trend premium already fully spanned by the reference's tsmom members?")
    assert gate2_retry_open_question(
        "Would the cadence-720 hysteresis variant (exp-746025337a2a) survive gate 2, given its halved turnover?")
    assert gate2_retry_open_question(
        "Would a wider-band variant cut turnover enough to pass a gate-2-style no-harm test?")
    assert not gate2_retry_open_question(
        "Is the trend premium already fully captured by the reference's existing members?")
    assert not open_question_allowed(
        "Which gate-2 component failed (Sharpe, drawdown or turnover)?", "tsmom", set())
    assert not open_question_allowed(
        "Would the cadence-720 variant survive gate 2?", "tsmom", {"tsmom"})
    assert open_question_allowed(
        "Would the cadence-720 variant survive gate 2?", "tsmom", set())  # no prior Gate 2 failure
    assert open_question_allowed(
        "Is the trend premium already fully spanned by the reference?", "tsmom", {"tsmom"})


def test_generator_skips_unanswerable_and_gate2_retry_open_questions(paths, store, dataset, monkeypatch):
    from quant_research.campaign import generate
    from quant_research.memory.conclusions import add_campaign_conclusion

    d = _diag(paths, store, dataset)
    monkeypatch.setattr(generate, "diagnostics", lambda *a: d)
    monkeypatch.setattr(generate, "gate2_failed_families", lambda *a: {"tsmom"})
    questions = [
        "Which gate-2 component failed (Sharpe, drawdown or turnover)?",
        "Would a wider-band variant cut turnover enough to pass a gate-2-style no-harm test?",
        "Is the BTC hourly trend premium already fully spanned by the reference's tsmom members?",
        "Was 2023's relative shortfall a tsmom-specific regime effect?",
    ]
    add_campaign_conclusion(store, "cmp-filter-test", "tsmom", "BTC-USD", dataset[1].split_id, {
        "hypothesis": "h", "preregistered_plan": "p", "decision": "inconclusive", "limitations": [],
        "unresolved": questions, "experiment_ids": [], "results": {}})
    keys = {c["key"]: c for c in generate.candidates(paths, store, "BTC-USD")}
    open_keys = sorted(k for k in keys if k.startswith("open:"))
    assert open_keys == ["open:campaign:cmp-filter-test:2", "open:campaign:cmp-filter-test:3"]
    assert "fully spanned" in keys[open_keys[0]]["spec"]["goal"]
    assert "2023" in keys[open_keys[1]]["spec"]["goal"]


def test_generator_runs_on_the_real_reference(paths, store, dataset):
    from quant_research.campaign import generate

    _reference(paths, store, dataset)
    d = generate.diagnostics(paths, store, "BTC-USD")
    assert d["reference_experiment"] and d["sleeves"]
    assert isinstance(generate.candidates(paths, store, "BTC-USD"), list)


# -- D7 report, CLI ------------------------------------------------------------------------------------------

def test_report_shows_campaigns_budget_and_quotas(paths, store, dataset):
    from quant_research.desk.report import research_section

    _reference(paths, store, dataset)
    _queued(paths, store)
    loop, clock = _loop(paths, store, FakeAgent(paths, store))
    loop.step()
    lines, _ = research_section(paths, store, rl.utc_day(clock.t))
    text = "\n".join(lines)
    assert "- Budget: " in text and "of 5.0 USD today" in text and "of 83.33 USD this month" in text
    assert "| campaign | product | family | state |" in text and "| running | 1/4 | 2/40 |" in text
    assert "- Quotas on " in text and "tsmom 2/100" in text
    assert "| run | product | campaign |" in text


def test_campaign_cli(paths, store, dataset, capsys, tmp_path):
    import yaml

    from quant_research.cli import main

    _reference(paths, store, dataset)
    home = str(paths.root)
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(_spec().model_dump(mode="json")))
    main(["--home", home, "campaign", "add", "--file", str(f), "--note", "my idea"])
    cid = json.loads(capsys.readouterr().out)["id"]
    main(["--home", home, "campaign", "approve", cid, "--note", "go"])
    assert json.loads(capsys.readouterr().out) == [{"id": cid, "state": "queued"}]
    main(["--home", home, "campaign", "list", "--state", "queued"])
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [cid]
    main(["--home", home, "campaign", "show", cid])
    shown = json.loads(capsys.readouterr().out)
    assert shown["caps"]["usd"] == 1.0 and shown["source"] == {"kind": "human", "note": "my idea"}
    main(["--home", home, "campaign", "quotas", "--product", "BTC-USD"])
    assert {q["family"] for q in json.loads(capsys.readouterr().out)} >= {"tsmom", "exploration"}
    main(["--home", home, "campaign", "budget"])
    assert json.loads(capsys.readouterr().out)["limits"]["daily_usd"] == 5.0
    main(["--home", home, "campaign", "cancel", cid, "--note", "no"])
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"
    with pytest.raises(SystemExit, match="cannot become queued"):
        main(["--home", home, "campaign", "approve", cid, "--note", "again"])


def test_desk_config_defaults_follow_d7_and_p2(paths):
    cfg = load_config(paths)
    assert (cfg.daily_cost_cap_usd, cfg.monthly_cost_cap_usd, cfg.session_reserve_usd,
            cfg.max_sessions_per_day) == (5.0, 83.33, 0.25, 12)
    save_config(paths, cfg.model_copy(update={"daily_cost_cap_usd": 20.0}))
    assert budget.limits(load_config(paths))["daily_usd"] == 5.0
    with pytest.raises(ValueError, match="unknown pricing presets"):
        type(cfg).model_validate({"campaign_models": {"m": "claude-opus"}})


def test_the_brief_shows_the_archive_of_the_campaigns_product_with_its_split_first():
    from quant_research.campaign.brief import archive_view

    md = ("# Trend (`tsmom`)\n\nThesis.\n\n## BTC-USD (`split-old`)\n\nold btc\n\n## ETH-USD (`split-e`)\n\neth\n\n"
          "## BTC-USD (`split-new`)\n\nnew btc\n\n## Conclusions\n\n- `s1` BTC-USD trend: continue.\n"
          "- `s2` ETH-USD trend: stop.\n")
    view = archive_view(md, "BTC-USD", "split-new")
    assert view.index("new btc") < view.index("old btc") and "eth" not in view.lower().replace("thesis", "")
    assert "`s1` BTC-USD" in view and "`s2`" not in view and view.startswith("# Trend")


def test_a_cli_cost_estimate_without_a_price_is_still_unpriced(paths, store):
    from types import SimpleNamespace

    loop, _ = _loop(paths, store, FakeAgent(paths, store))
    usage = TokenUsage(model="mystery-model", input_tokens=1000, output_tokens=100)
    run = SimpleNamespace(extra={"models": ["mystery-model"]}, cli_cost_usd=0.12)
    assert loop._cost(run, usage, 0.25) == (0.12, "unpriced")
    assert loop._cost(run, TokenUsage(model="mystery-model"))[1] == "no_tokens"
    run.cli_cost_usd = None
    assert loop._cost(run, usage, 0.25) == (0.25, "unpriced")
    run.extra = {"models": ["deepseek-v4-flash"]}
    assert loop._cost(run, usage)[1] == "priced_usage"


def test_a_run_without_usage_is_booked_at_its_reservation(paths, store, dataset):
    _reference(paths, store, dataset)
    _queued(paths, store)
    loop, _ = _loop(paths, store, FakeAgent(paths, store))
    assert loop._cost(None, None, 0.25, maybe_called=True) == (0.25, "cost_unknown")
    assert loop._cost(None, None, 0.25, maybe_called=False) == (0.0, "no_tokens")
    run_dir = rl.sessions_dir(paths) / "r"
    (run_dir / rl.TRANSCRIPT_DIR).mkdir(parents=True)
    assert not loop._maybe_called(run_dir, [], crash=True)
    (run_dir / rl.TRANSCRIPT_DIR / "t.jsonl").write_text("{}\n")
    assert loop._maybe_called(run_dir, [], crash=True)
    loop.isolate_network = True
    assert not loop._maybe_called(run_dir, [], crash=False)
    assert loop._maybe_called(run_dir, [{"kind": "proxy", "allowed": True, "host": "api"}], crash=False)
    loop.isolate_network = False
    loop.adapter = FakeAgent(paths, store, ["env_fail"])
    loop._maybe_called = lambda *a: True
    res = loop.step()["result"]
    assert res["cost_source"] == "cost_unknown" and res["cost_usd"] == 0.25
    stop = budget.active_stop(paths)
    assert "reported no usage" in stop["reason"] and loop.step()["status"] == "budget_stop"
