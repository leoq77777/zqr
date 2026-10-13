import pandas as pd
import pytest

from quant_research.data.bars import to_epoch
from quant_research.experiments.replay import replay_experiment
from quant_research.experiments.runner import ExperimentRequest, run_experiment, run_many
from quant_research.experiments.store import ExperimentStore
from quant_research.report.markdown import write_experiment_report
from quant_research.strategy.compute import compute_targets
from quant_research.strategy.plugin import register_plugin
from quant_research.strategy.spec import StrategySpec
from quant_research.validation.holdout import HoldoutError, run_holdout, split_viewing_cap
from quant_research.validation.robustness import run_robustness
from quant_research.validation.splits import SplitError, load_research_segment
from quant_research.validation.walkforward import walk_forward

GOOD_PLUGIN = """
import pandas as pd
def target_positions(bars, params):
    ma = bars["close"].rolling(params["n"]).mean()
    return (bars["close"] > ma).astype(float)
"""

PEEKING_PLUGIN = """
def target_positions(bars, params):
    return (bars["close"].shift(-1) > bars["close"]).astype(float)
"""

CRASHING_PLUGIN = """
def target_positions(bars, params):
    raise RuntimeError("boom")
"""


def _req(manifest, split, **kw):
    base = {"strategy_ref": "template:sma_cross", "parameters": {"fast": 10, "slow": 50},
            "dataset_id": manifest["dataset_id"], "split_id": split.split_id}
    return ExperimentRequest(**{**base, **kw})


def _plugin(paths, tmp_path, name, code):
    f = tmp_path / f"{name}.py"
    f.write_text(code)
    return f"plugin:{register_plugin(paths, f)}"


def test_holdout_is_physically_separated(paths, dataset):
    manifest, split = dataset
    research = pd.read_parquet(paths.splits / split.split_id / "research.parquet")
    assert to_epoch(research["bar_open_time"]).max() < split.holdout[0]
    with pytest.raises(SplitError):
        load_research_segment(paths, split.split_id, "holdout")
    val = load_research_segment(paths, split.split_id, "validation")
    assert val.window == split.validation


def test_experiment_records_identity_and_replays(paths, store, dataset):
    manifest, split = dataset
    res = run_experiment(paths, store, _req(manifest, split, segment="validation"))
    assert res.status == "completed"
    rec = store.get_experiment(res.experiment_id)
    assert rec["data_hash"] == manifest["data_hash"] and rec["candidate_hash"] and rec["engine_version"]
    assert rec["checks"]["lookahead_ok"]
    assert "buy_and_hold" in rec["benchmarks"]
    rep = replay_experiment(paths, store, res.experiment_id)
    assert rep["reproducible"], rep["differences"]
    report = write_experiment_report(paths, store, res.experiment_id)
    assert (report.parent / "equity.png").exists() and "Metrics" in report.read_text()


def test_invalid_params_are_recorded_as_rejected(paths, store, dataset):
    manifest, split = dataset
    res = run_experiment(paths, store, _req(manifest, split, parameters={"fast": 50, "slow": 10}))
    assert res.status == "rejected"
    assert store.get_experiment(res.experiment_id)["status"] == "rejected"
    assert store.trial_count(split_id=split.split_id) == 1


def test_plugin_runs_in_sandbox_and_lookahead_is_rejected(paths, store, dataset, tmp_path):
    manifest, split = dataset
    good_ref = _plugin(paths, tmp_path, "good", GOOD_PLUGIN)
    good = run_experiment(paths, store, _req(manifest, split, strategy_ref=good_ref, parameters={"n": 20}))
    assert good.status == "completed", good.failure_reason
    peek = run_experiment(paths, store, _req(manifest, split, strategy_ref=_plugin(paths, tmp_path, "peek",
                                                                                   PEEKING_PLUGIN), parameters={}))
    assert peek.status == "rejected" and "lookahead" in peek.failure_reason
    crash = run_experiment(paths, store, _req(manifest, split, strategy_ref=_plugin(paths, tmp_path, "crash",
                                                                                    CRASHING_PLUGIN), parameters={}))
    assert crash.status == "failed" and "boom" in crash.failure_reason


def test_plugin_matches_equivalent_template(paths, dataset, tmp_path):
    from quant_research.validation.splits import load_research_segment as load

    _, split = dataset
    code = """
def target_positions(bars, params):
    c = bars["close"]
    return (c.rolling(5).mean() > c.rolling(20).mean()).astype(float)
"""
    ref = _plugin(paths, tmp_path, "sma", code)
    bars = load(paths, split.split_id, "dev").bars
    a = compute_targets(StrategySpec(strategy_ref=ref), bars, paths).targets
    b = compute_targets(StrategySpec(strategy_ref="template:sma_cross", parameters={"fast": 5, "slow": 20}),
                        bars).targets
    assert (a == b).all()


def test_holdout_requires_freeze_and_tracks_viewings(paths, store, dataset):
    manifest, split = dataset
    req = _req(manifest, split)
    with pytest.raises(HoldoutError):
        run_holdout(paths, store, req, max_viewings=10)
    spec = req.spec.normalized()
    store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
    res, prior = run_holdout(paths, store, req, max_viewings=10)
    assert res.status == "completed" and prior == 0
    rec = store.get_experiment(res.experiment_id)
    assert (rec["window_start"], rec["window_end"]) == tuple(split.holdout)
    again, prior2 = run_holdout(paths, store, req, max_viewings=10)
    assert again.status == "duplicate" and prior2 == 0  # the same candidate again is not a new viewing
    assert len(store.holdout_runs(split.split_id)) == 1
    assert replay_experiment(paths, store, res.experiment_id)["reproducible"]
    # holdout runs do not inflate research trial counts
    assert store.trial_count(split_id=split.split_id) == 0


def test_holdout_dsr_counts_viewings_not_research_trials(paths, store, dataset):
    manifest, split = dataset
    for fast in (5, 8, 12):
        run_experiment(paths, store, _req(manifest, split, parameters={"fast": fast, "slow": 50}))
    req = _req(manifest, split)
    spec = req.spec.normalized()
    store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
    first, _ = run_holdout(paths, store, req, max_viewings=10)
    assert store.trial_count(split_id=split.split_id) == 3
    assert first.statistics["n_trials"] == 1 and first.statistics["dsr"] == pytest.approx(first.statistics["psr_0"])
    second, _ = run_holdout(paths, store, req, max_viewings=10)
    assert second.statistics["n_trials"] == 2


def test_holdout_cap_applies_to_every_path_and_overrides_are_recorded(paths, store, dataset):
    manifest, split = dataset
    reqs = [_req(manifest, split, parameters={"fast": f, "slow": 50}) for f in (5, 8, 12)]
    for r in reqs:
        spec = r.spec.normalized()
        store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
    run_holdout(paths, store, reqs[0], max_viewings=2)
    run_holdout(paths, store, reqs[1], max_viewings=2)
    again, _ = run_holdout(paths, store, reqs[0], max_viewings=2)
    assert again.status == "duplicate" and len(store.holdout_runs(split.split_id)) == 2
    with pytest.raises(HoldoutError, match="already viewed 2"):
        run_holdout(paths, store, reqs[2], max_viewings=2)
    run_holdout(paths, store, reqs[2], max_viewings=2, override_reason="manual check")
    assert len(store.holdout_runs(split.split_id)) == 3
    assert store.holdout_overrides(split.split_id)[0]["reason"] == "manual check"


def test_split_viewing_cap_uses_the_tightest_mandate(store, dataset):
    from tests.conftest import make_mandate

    _, split = dataset
    assert split_viewing_cap(store, split.split_id) == 10
    for cap in (4, 2):
        m = make_mandate(dataset, holdout={"max_viewings": cap, "min_primary": 0.0, "min_total_return": 0.0},
                         question=f"cap {cap}")
        store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    assert split_viewing_cap(store, split.split_id) == 2


def test_parallel_matches_serial(paths, store, dataset):
    manifest, split = dataset
    reqs = [_req(manifest, split, parameters={"fast": f, "slow": s}) for f, s in [(5, 20), (10, 40), (20, 80)]]
    serial = run_many(paths, store, reqs, workers=1)
    parallel = run_many(paths, ExperimentStore(paths.root / "other.db"), reqs, workers=2)
    assert {r.status for r in serial} == {r.status for r in parallel} == {"completed"}
    assert [r.metrics for r in serial] == [r.metrics for r in parallel]
    again = run_many(paths, store, reqs, workers=2)
    assert {r.status for r in again} == {"duplicate"}
    assert [r.duplicate_of for r in again] == [r.experiment_id for r in serial]


def test_robustness_and_walkforward(paths, store, dataset):
    manifest, split = dataset
    rob = run_robustness(paths, store, _req(manifest, split), subperiods=3)
    assert rob["cost_doubled"]["total_fees"] > rob["base"]["total_fees"]
    assert len(rob["neighbors"]) == 4 and len(rob["subperiods"]) == 3
    wf = walk_forward(paths, store, _req(manifest, split), {"fast": [5, 10], "slow": [30, 60]},
                      train_bars=600, test_bars=300)
    assert wf["folds"] and all(f["selected"] for f in wf["folds"])
    assert "oos_sharpe" in wf


def test_interrupted_experiments_become_aborted(paths, store, dataset):
    manifest, split = dataset
    store.start_experiment(kind="backtest", strategy_ref="template:cash", parameters={},
                           dataset_id=manifest["dataset_id"])
    assert store.mark_interrupted() == 1
    assert store.list_experiments()[0]["status"] == "aborted"
