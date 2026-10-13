"""M3: mandate, preregistered plans, candidate lifecycle, tool gateway, holdout governance, trial accounting."""

import json
import os

import pytest
from pydantic import ValidationError

from quant_research.controller.agent import AgentRoundResult, CandidateSubmission, ManualRunner, ScriptedRunner
from quant_research.controller.budget import Budget
from quant_research.controller.controller import ResearchController, SessionConfig
from quant_research.gateway import GatewayError, ToolGateway
from quant_research.research.mandate import Mandate
from quant_research.research.plans import ResearchPlan, plan_status
from quant_research.research.registry import RegistryError, freeze, transition
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.holdout import HoldoutError, run_candidate_holdout
from tests.conftest import make_mandate

PLAN = {"hypothesis": "short-term trend persists on hourly bars", "mechanism": "slow information diffusion",
        "falsification": "validation sharpe below zero", "artifact": "sma_cross",
        "parameter_ranges": {"fast": {"values": [5, 10]}, "slow": {"min": 20, "max": 80}},
        "planned_experiments": 2}


def _session(paths, store, dataset, runner=None, **mandate_kw):
    ctl = ResearchController(paths, store, runner or ManualRunner())
    cfg = SessionConfig(mandate=make_mandate(dataset, **mandate_kw), sandbox=SandboxConfig(isolation="subprocess"))
    return ctl, ctl.create_session(cfg)


# -- mandate -------------------------------------------------------------------------------------------
def test_mandate_requires_explicit_thresholds_and_is_content_addressed(dataset):
    m = make_mandate(dataset)
    body = m.model_dump(mode="json")
    with pytest.raises(ValidationError, match="dsr_waiver"):
        Mandate.model_validate({**body, "acceptance": {**body["acceptance"], "dsr_waiver": None}})
    no_primary = {k: v for k, v in body["acceptance"].items() if k != "min_primary"}
    with pytest.raises(ValidationError):
        Mandate.model_validate({**body, "acceptance": no_primary})
    with pytest.raises(ValidationError):
        Mandate.model_validate({**body, "surprise": 1})
    with pytest.raises(ValidationError):
        m.question = "changed"  # frozen
    assert Mandate.model_validate(body).mandate_id() == m.mandate_id()
    assert make_mandate(dataset, {"min_primary": 0.1}).mandate_id() != m.mandate_id()


# -- plans ---------------------------------------------------------------------------------------------
def test_plan_status_planned_unplanned_out_of_plan():
    plan = ResearchPlan.model_validate(PLAN)
    assert plan_status(None, "sma_cross", {}, 0) == ("unplanned", None)
    assert plan_status(plan, "sma_cross", {"fast": 5, "slow": 50}, 0) == ("planned", None)
    status, why = plan_status(plan, "sma_cross", {"fast": 7, "slow": 50}, 0)
    assert status == "out_of_plan" and "fast=7" in why
    assert plan_status(plan, "momentum", {}, 0)[0] == "out_of_plan"
    assert "already used" in plan_status(plan, "sma_cross", {"fast": 5, "slow": 50}, 2)[1]
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate({**PLAN, "hypothesis": "short"})


# -- lifecycle -----------------------------------------------------------------------------------------
def _validated_candidate(paths, store, dataset, **mandate_kw):
    agent = ScriptedRunner(lambda t: AgentRoundResult(candidates=[CandidateSubmission(template="buy_and_hold")],
                                                      done=True))
    ctl, sid = _session(paths, store, dataset, agent, **mandate_kw)
    summary = ctl.run(sid)
    return summary["candidates"][0]["candidate_id"], sid


def test_lifecycle_actor_rules_freeze_and_holdout_limit(paths, store, dataset):
    cid, _ = _validated_candidate(paths, store, dataset)
    with pytest.raises(RegistryError, match="not allowed for controller"):
        transition(store, cid, "frozen", "controller")
    with pytest.raises(RegistryError, match="illegal"):
        transition(store, cid, "paper_approved", "human")
    cand = freeze(paths, store, cid, "reviewed")
    assert cand["state"] == "frozen"
    art = paths.artifacts / "candidates" / cid
    assert {p.name for p in art.iterdir()} >= {"identity.json", "evidence.json", "events.json"}
    assert not os.access(art / "identity.json", os.W_OK)
    with pytest.raises(RegistryError):
        transition(store, cid, "final_evaluated", "human")  # only the evaluator decides
    out = run_candidate_holdout(paths, store, cid)
    assert out["candidate"]["state"] in ("final_evaluated", "failed_final")
    assert out["candidate"]["evidence"]["holdout_experiment"] == out["result"].experiment_id
    with pytest.raises(HoldoutError, match="only frozen"):
        run_candidate_holdout(paths, store, cid)


def test_holdout_viewing_limit_spans_candidates(paths, store, dataset):
    cid, _ = _validated_candidate(paths, store, dataset)
    freeze(paths, store, cid, "")
    run_candidate_holdout(paths, store, cid)
    agent = ScriptedRunner(lambda t: AgentRoundResult(
        candidates=[CandidateSubmission(template="momentum", parameters={"lookback": 24})], done=True))
    ctl, sid = _session(paths, store, dataset, agent)
    cid2 = ctl.run(sid)["best"]["candidate_id"]
    freeze(paths, store, cid2, "")
    with pytest.raises(HoldoutError, match="already viewed 1"):
        run_candidate_holdout(paths, store, cid2)


def test_human_can_retire_but_not_validate(paths, store, dataset):
    _, sid = _validated_candidate(paths, store, dataset, acceptance={"min_primary": 99})
    cand = store.list_candidates(session_id=sid)[0]
    assert cand["state"] == "rejected"
    with pytest.raises(RegistryError, match="already rejected"):
        transition(store, cand["id"], "retired", "human")
    cid, _ = _validated_candidate(paths, store, dataset, question="another question")
    assert transition(store, cid, "retired", "human")["state"] == "retired"


# -- trial accounting ----------------------------------------------------------------------------------
def test_sensitivity_runs_recorded_but_not_trials(paths, store, dataset):
    _, split = dataset
    cid, sid = _validated_candidate(paths, store, dataset,
                                    acceptance={"require_costs_x2": True, "require_delay": True})
    exps = store.list_experiments(limit=100, session_id=sid)
    kinds = sorted(e["kind"] for e in exps)
    assert kinds.count("sensitivity") == 2
    assert store.trial_count(session_id=sid) == len(exps) - 2
    sens = [e for e in exps if e["kind"] == "sensitivity"]
    assert {e["engine_config"]["execution_delay_bars"] for e in sens} == {0, 1}
    assert store.get_candidate(cid)["evidence"]["experiments"].keys() >= {"costs_x2", "delay_1_adverse"}


def test_walk_forward_design_counts_one_trial(paths, store, dataset):
    plan = {**PLAN, "parameter_ranges": {"fast": {"values": [5, 10]}, "slow": {"values": [40]}}}

    def agent(task):
        pid = ToolGateway(paths, store, task.session_id).submit_plan(plan)["plan_id"]
        return AgentRoundResult(candidates=[CandidateSubmission(template="sma_cross", plan_id=pid,
                                                                parameters={"fast": 5, "slow": 40})], done=True)

    ctl, sid = _session(paths, store, dataset, ScriptedRunner(agent),
                        evaluation_design={"kind": "walk_forward_oos", "train_bars": 400, "test_bars": 200})
    row = ctl.run(sid)["candidates"][0]
    assert row["plan_status"] == "planned" and not row["plan_retrospective"]
    wf = store.get_experiment(row["validation_experiment"])
    assert wf["kind"] == "walkforward" and wf["policy_id"]
    folds = store.list_experiments(limit=500, session_id=sid, kind="walkforward_fold")
    assert len(folds) >= 4 and all(":wf" in f["segment"] for f in folds)
    assert store.trial_count(session_id=sid) == 2  # the dev run and the walk-forward procedure
    store.update_candidate(row["candidate_id"], state="frozen")
    with pytest.raises(HoldoutError, match="walk-forward"):
        run_candidate_holdout(paths, store, row["candidate_id"])


# -- gateway -------------------------------------------------------------------------------------------
def test_gateway_idempotency_paths_and_sanitized_output(paths, store, dataset):
    _, sid = _session(paths, store, dataset)
    gw = ToolGateway(paths, store, sid)
    pid = gw.submit_plan(PLAN)["plan_id"]
    first = gw.submit_experiment("sma_cross", {"fast": 5, "slow": 50}, pid, "k1")
    assert first["status"] == "completed" and not first["replayed"] and first["plan"]["status"] == "planned"
    again = gw.submit_experiment("sma_cross", {"fast": 5, "slow": 50}, pid, "k1")
    assert again["replayed"] and again["experiment_id"] == first["experiment_id"]
    assert len(store.list_experiments(limit=50, session_id=sid)) == 1
    assert store.plan_usage(pid) == 1
    with pytest.raises(GatewayError, match="different request"):
        gw.submit_experiment("sma_cross", {"fast": 10, "slow": 50}, pid, "k1")
    off = gw.submit_experiment("sma_cross", {"fast": 7, "slow": 50}, pid, "k2")
    assert off["plan"]["status"] == "out_of_plan"
    over = gw.submit_experiment("sma_cross", {"fast": 10, "slow": 60}, pid, "k2b")  # third of 2 planned
    assert over["plan"]["status"] == "out_of_plan" and "already used" in over["plan"]["deviation"]
    assert gw.submit_experiment("momentum", {"lookback": 24}, None, "k3")["plan"]["status"] == "unplanned"
    with pytest.raises(GatewayError, match="escapes|inside"):
        gw.submit_experiment("../../evil.py", {}, None, "k4")
    with pytest.raises(GatewayError, match="inside the session workspace"):
        gw.submit_experiment("/etc/passwd.py", {}, None, "k5")
    assert gw.submit_experiment("momentum", {"lookback": 12}, None, "k4")["status"] == "completed"
    with pytest.raises(GatewayError, match="idempotency_key"):
        gw.submit_experiment("momentum", {}, None, None)

    text = json.dumps(gw.get_experiment(first["experiment_id"]), default=str)
    assert str(paths.root) not in text and "result_dir" not in text
    assert json.dumps(gw.history(), default=str).count(str(paths.root)) == 0
    info = gw.inspect_dataset()
    assert list(info["allowed_segments"]) == ["dev"] and str(paths.root) not in json.dumps(info)
    with pytest.raises(GatewayError, match="mandate"):
        gw.inspect_dataset("other-dataset")


def test_gateway_plugin_from_container_path_and_session_isolation(paths, store, dataset):
    _, sid = _session(paths, store, dataset)
    gw = ToolGateway(paths, store, sid)
    (gw.workspace / "s.py").write_text("def generate_exposures(history, parameters):\n"
                                       "    return [1.0] * len(history)\n")
    res = gw.submit_experiment("/workspace/s.py", {}, None, "p1")
    assert res["identity"]["artifact"] == "s.py"
    assert res["status"] == "blocked" and "docker" in res["error"]  # weak isolation not allowed by default
    _, other = _session(paths, store, dataset, question="other session")
    with pytest.raises(GatewayError, match="unknown experiment"):
        ToolGateway(paths, store, other).get_experiment(res["experiment_id"])


def test_gateway_compare_and_report_validation(paths, store, dataset):
    _, sid = _session(paths, store, dataset)
    gw = ToolGateway(paths, store, sid)
    a = gw.submit_experiment("sma_cross", {"fast": 5, "slow": 50}, None, "a")
    b = gw.submit_experiment("momentum", {"lookback": 24}, None, "b")
    rows = gw.compare_experiments([a["experiment_id"], b["experiment_id"]])["rows"]
    assert {r["experiment_id"] for r in rows} == {a["experiment_id"], b["experiment_id"]}
    store._update("experiments", "id", b["experiment_id"], {"policy_id": "policy-other"})
    with pytest.raises(GatewayError, match="policy_id"):
        gw.compare_experiments([a["experiment_id"], b["experiment_id"]])

    sharpe = a["metrics"]["sharpe"]
    doc = {"conclusion": "x", "experiments": [{"experiment_id": a["experiment_id"], "metrics": {"sharpe": sharpe}}]}
    (gw.workspace / "research.json").write_text(json.dumps(doc))
    assert gw.build_report() == {"valid": True, "problems": []}
    doc["experiments"][0]["metrics"]["sharpe"] = sharpe + 1
    doc["experiments"].append({"experiment_id": "exp-made-up"})
    (gw.workspace / "research.json").write_text(json.dumps(doc))
    problems = gw.build_report()["problems"]
    assert any("recorded" in p for p in problems) and any("exp-made-up" in p for p in problems)


def test_gateway_budget_and_stop(paths, store, dataset):
    ctl, sid = _session(paths, store, dataset, budget=Budget(max_rounds=3, max_experiments=1))
    gw = ToolGateway(paths, store, sid)
    assert gw.submit_experiment("momentum", {"lookback": 24}, None, "x1")["status"] == "completed"
    assert gw.submit_experiment("momentum", {"lookback": 12}, None, "x2")["status"] == "blocked"
    ctl.request_stop(sid)
    with pytest.raises(GatewayError, match="disabled"):
        gw.submit_plan(PLAN)
