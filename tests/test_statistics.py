import math

import numpy as np
import pytest

from quant_research.controller.agent import AgentRoundResult, CandidateSubmission, ScriptedRunner
from quant_research.controller.budget import Budget
from quant_research.controller.controller import ResearchController, SessionConfig
from quant_research.data.snapshot import freeze_dataset
from quant_research.data.validate import validate_bars
from quant_research.evaluation.diagnosis import diagnose, significance_label
from quant_research.evaluation.statistics import deflate, expected_max_sharpe, probabilistic_sharpe, sharpe_statistics
from quant_research.experiments.gatekeeper import GateContext, check
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.strategy.plugin import register_plugin
from quant_research.strategy.sandbox import SandboxConfig
from quant_research.validation.splits import create_split
from tests.conftest import HOUR, T0, make_mandate, make_raw

PPY = 24 * 365


def _req(dataset, **kw):
    manifest, split = dataset
    base = {"strategy_ref": "template:sma_cross", "parameters": {"fast": 10, "slow": 50},
            "dataset_id": manifest["dataset_id"], "split_id": split.split_id}
    return ExperimentRequest(**{**base, **kw})


# -- statistics ---------------------------------------------------------------------------------------
def test_probabilistic_sharpe_known_value():
    # sr=0.1/bar, n=101, normal returns: z = 0.1*10/sqrt(1 + 0.5*0.01)
    assert probabilistic_sharpe(0.1, 0.0, 101, 0.0, 3.0) == pytest.approx(0.8407, abs=1e-3)
    assert probabilistic_sharpe(0.0, 0.0, 101, 0.0, 3.0) == pytest.approx(0.5)
    # fat tails and negative skew make the same Sharpe less convincing
    assert probabilistic_sharpe(0.1, 0.0, 101, -1.0, 10.0) < probabilistic_sharpe(0.1, 0.0, 101, 0.0, 3.0)


def test_more_trials_raise_the_bar():
    assert expected_max_sharpe(1, 0.01) == 0.0
    assert 0 < expected_max_sharpe(10, 0.01) < expected_max_sharpe(1000, 0.01)
    r = np.random.default_rng(0).normal(0.0005, 0.01, 2000)
    stats = sharpe_statistics(r, PPY)
    few, many = deflate(stats, 1, [], PPY), deflate(stats, 500, [], PPY)
    assert few["dsr"] == pytest.approx(stats["psr_0"])
    assert many["dsr"] < few["dsr"]
    assert many["expected_max_sharpe_annual"] > 0
    assert "null estimator" in many["trial_sharpe_variance_source"]
    assert "empirical" in deflate(stats, 6, [0.1, -0.5, 1.2, 0.3, 0.8, -1.0], PPY)["trial_sharpe_variance_source"]


def test_bootstrap_is_deterministic_and_brackets_estimate():
    r = np.random.default_rng(1).normal(0.0003, 0.01, 1500)
    a, b = sharpe_statistics(r, PPY), sharpe_statistics(r, PPY)
    assert a == b
    point = a["sharpe_per_bar"] * math.sqrt(PPY)
    assert a["sharpe_ci_05"] < point < a["sharpe_ci_95"]
    assert a["total_return_ci_05"] < a["total_return_ci_95"]
    assert 0 <= a["prob_sharpe_le_0"] <= 1


# -- diagnosis ----------------------------------------------------------------------------------------
def _attr(**kw):
    regime = {"fraction": 0.5, "strategy_log_return": 0.05, "asset_log_return": 0.05, "sharpe": 0.5}
    base = {"gross_total_return": 0.3, "net_total_return": 0.25, "cost_drag": 0.05, "gross_sharpe": 1.0,
            "beta": 0.3, "alpha_annual": 0.1, "round_trips_per_year": 10, "months": 12,
            "positive_month_fraction": 0.6, "regimes": {k: dict(regime) for k in
                                                         ("trend_up", "trend_down", "vol_high", "vol_low")}}
    return {**base, **kw}


def _labels(metrics=None, attribution=None, bh_sharpe=0.0):
    m = {"total_return": 0.1, "sharpe": 1.0, "n_round_trips": 30, "max_drawdown": -0.1, **(metrics or {})}
    return {x["label"] for x in diagnose(m, attribution or _attr(), {"buy_and_hold": {"sharpe": bh_sharpe}})}


def test_diagnosis_labels():
    assert _labels() == set()
    assert "no_gross_edge" in _labels(attribution=_attr(gross_sharpe=-0.2, gross_total_return=-0.1))
    assert "cost_dominated" in _labels({"total_return": -0.05}, _attr(cost_drag=0.35))
    assert "overtrading" in _labels(attribution=_attr(round_trips_per_year=200, cost_drag=0.12))
    assert "mostly_beta" in _labels(attribution=_attr(beta=0.95, alpha_annual=-0.01))
    assert "underperforms_buy_and_hold" in _labels(bh_sharpe=2.0)
    assert "too_few_trades" in _labels({"n_round_trips": 3})
    assert "deep_drawdown" in _labels({"max_drawdown": -0.6})
    reg = _attr()["regimes"]
    reg["trend_up"]["strategy_log_return"], reg["trend_down"]["strategy_log_return"] = 0.4, -0.1
    assert "regime_dependent" in _labels(attribution=_attr(regimes=reg))
    conc = diagnose({"total_return": 0.1, "sharpe": 1.0, "n_round_trips": 30, "max_drawdown": -0.1},
                    _attr(best_month_share_of_log_return=0.8), {"buy_and_hold": {"sharpe": 0.0}})
    assert [d["evidence"] for d in conc] == ["best month 80% of total log return"]
    for d in diagnose({"total_return": 0.1, "sharpe": 1.0, "n_round_trips": 3, "max_drawdown": -0.1}, _attr(),
                      {"buy_and_hold": {"sharpe": 0.0}}):
        assert d["evidence"] and d["suggestion"]
    assert significance_label({"dsr": 0.95}) is None
    assert significance_label({"dsr": 0.3, "n_trials": 40, "psr_0": 0.7})["label"] == "not_significant"


def test_experiment_records_statistics_attribution_and_dsr(paths, store, dataset):
    res = run_experiment(paths, store, _req(dataset, parameters={"fast": 3, "slow": 8}))
    assert res.status == "completed"
    s, a = res.statistics, res.diagnosis["attribution"]
    assert s["n_trials"] == 1 and 0 <= s["dsr"] <= 1 and "sharpe_ci_05" in s
    assert a["cost_drag"] > 0 and a["net_total_return"] == pytest.approx(res.metrics["total_return"])
    row = store.get_experiment(res.experiment_id)
    assert row["statistics"]["dsr"] == s["dsr"] and row["diagnosis"]["labels"] == res.diagnosis["labels"]
    # each further trial on the split deflates more
    later = run_experiment(paths, store, _req(dataset, parameters={"fast": 4, "slow": 8}))
    assert later.statistics["n_trials"] == 2


# -- gatekeeper ---------------------------------------------------------------------------------------
def test_gate_duplicate_is_reused_and_counted(paths, store, dataset):
    first = run_experiment(paths, store, _req(dataset))
    again = run_experiment(paths, store, _req(dataset))
    assert again.status == "duplicate" and again.duplicate_of == first.experiment_id
    assert again.metrics == first.metrics and again.has_result
    assert store.trial_count(split_id=dataset[1].split_id) == 2
    assert again.statistics["n_trials"] == 2


def test_gate_rejects_invalid_spec_and_counts_it(paths, store, dataset):
    res = run_experiment(paths, store, _req(dataset, parameters={"fast": 50, "slow": 10}))
    assert res.status == "rejected" and res.failure_reason.startswith("gate:")
    assert store.trial_count(split_id=dataset[1].split_id) == 1


def test_gate_blocks_without_counting(paths, store, dataset, tmp_path):
    split_id = dataset[1].split_id
    req = _req(dataset)
    held = check(paths, store, req, GateContext(caller="agent_tool"), segment="holdout")
    assert held.status == "blocked" and "holdout" in held.reasons[0]
    wf = check(paths, store, req, GateContext(caller="controller"), segment="wf0")
    assert wf.status == "blocked"

    stop = tmp_path / "STOP"
    stop.write_text("x")
    assert run_experiment(paths, store, req, gate=GateContext(stop_file=stop)).status == "blocked"

    sid = "sess_budget"
    run_experiment(paths, store, req, session_id=sid)
    over = run_experiment(paths, store, _req(dataset, parameters={"fast": 5, "slow": 50}), session_id=sid,
                          gate=GateContext(session_id=sid, max_session_experiments=1))
    assert over.status == "blocked" and "budget" in over.failure_reason

    capped = run_experiment(paths, store, _req(dataset, parameters={"fast": 6, "slow": 50}), session_id=sid,
                            gate=GateContext(session_id=sid, max_trials_per_strategy=1))
    assert capped.status == "blocked" and "trial cap" in capped.failure_reason

    code = tmp_path / "s.py"
    code.write_text("def target_positions(bars, params):\n    return (bars['close'] > 0).astype(float)\n")
    plugin = _req(dataset, strategy_ref=f"plugin:{register_plugin(paths, code)}", parameters={})
    weak = run_experiment(paths, store, plugin, sandbox=SandboxConfig(isolation="subprocess"),
                          gate=GateContext(allow_weak_isolation=False))
    assert weak.status == "blocked" and "docker" in weak.failure_reason

    # only the first completed run counts; every blocked attempt is recorded but not a trial
    assert store.trial_count(split_id=split_id) == 1
    assert sum(e["status"] == "blocked" for e in store.list_experiments(limit=100)) == 4


def test_gate_rejects_unacknowledged_gaps(paths, store):
    raw = make_raw(3000, seed=2)
    raw = raw.drop(index=range(100, 103))  # gap inside dev
    bars, rep = validate_bars(raw, "coinbase", "BTC-USD", "ONE_HOUR", T0, T0 + 3000 * HOUR)
    manifest = freeze_dataset(paths, bars, rep)
    split = create_split(paths, manifest["dataset_id"])
    req = ExperimentRequest(strategy_ref="template:buy_and_hold", dataset_id=manifest["dataset_id"],
                            split_id=split.split_id)
    assert run_experiment(paths, store, req).status == "rejected"
    assert run_experiment(paths, store, req.model_copy(update={"allow_gaps": True})).status == "completed"


# -- controller integration ---------------------------------------------------------------------------
def test_controller_feeds_back_diagnosis_and_applies_dsr(paths, store, dataset):
    manifest, split = dataset
    tasks = []

    def agent(task):
        tasks.append(task)
        return AgentRoundResult(candidates=[CandidateSubmission(template="sma_cross",
                                                                parameters={"fast": 2 + task.round, "slow": 6})])

    mandate = make_mandate(dataset, {"min_dsr": 0.999, "dsr_waiver": None},
                           budget=Budget(max_rounds=2, max_candidates=10, patience_rounds=5))
    cfg = SessionConfig(mandate=mandate, sandbox=SandboxConfig(isolation="subprocess"))
    ctl = ResearchController(paths, store, ScriptedRunner(agent))
    summary = ctl.run(ctl.create_session(cfg))
    first = summary["candidates"][0]
    assert first["labels"] and "validation_dsr" in first and "cost_drag" in first
    assert first["verdict"] == "rejected" and any("deflated sharpe" in r for r in first["reasons"])
    second_task = tasks[1]
    assert second_task.feedback and second_task.feedback[0]["suggestion"]
    assert second_task.trials_on_split >= 2
    assert "Failure patterns diagnosed so far" in second_task.prompt()
