import pandas as pd
import pytest

from quant_research.data.bars import to_epoch
from quant_research.experiments.replay import ReplayError, replay_experiment
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
    with pytest.raises(ReplayError, match="not replayed"):  # an uncounted viewing otherwise
        replay_experiment(paths, store, res.experiment_id)
    assert len(store.holdout_runs(split.split_id)) == 1
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


def test_a_closed_holdout_refuses_new_viewings_but_repeats_answer(paths, store, dataset, monkeypatch):
    from quant_research.validation import holdout as holdout_mod

    manifest, split = dataset
    reqs = [_req(manifest, split, parameters={"fast": f, "slow": 50}) for f in (5, 8)]
    for r in reqs:
        spec = r.spec.normalized()
        store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
    run_holdout(paths, store, reqs[0], max_viewings=10)
    store.close_holdout(split.split_id, "seen by the human")
    assert store.close_holdout(split.split_id, "again")["note"] == "seen by the human"
    assert run_holdout(paths, store, reqs[0], max_viewings=10)[0].status == "duplicate"
    real_load = holdout_mod.load_holdout_data
    monkeypatch.setattr(holdout_mod, "load_holdout_data", lambda *a: pytest.fail("holdout data loaded"))
    with pytest.raises(HoldoutError, match="is closed \\(seen by the human\\)"):
        run_holdout(paths, store, reqs[1], max_viewings=10)
    assert len(store.holdout_runs(split.split_id)) == 1
    monkeypatch.setattr(holdout_mod, "load_holdout_data", real_load)
    res, prior = run_holdout(paths, store, reqs[1], max_viewings=10, override_reason="human check")
    assert res.status == "completed" and prior == 1
    assert [o["reason"] for o in store.holdout_overrides(split.split_id)] == ["human check"]
    notes = store.get_experiment(res.experiment_id)["notes"]
    assert "is closed" in notes and "contaminated" in notes


def test_close_holdout_command(paths, store, dataset, capsys):
    import json

    from quant_research.cli import main

    split_id = dataset[1].split_id
    with pytest.raises(SystemExit, match="unknown split"):
        main(["--home", str(paths.root), "close-holdout", "--split", "split-nope", "--note", "x"])
    with pytest.raises(SystemExit, match="needs a note"):
        main(["--home", str(paths.root), "close-holdout", "--split", split_id, "--note", " "])
    main(["--home", str(paths.root), "close-holdout", "--split", split_id, "--note", "holdout seen (A13.3)"])
    out = json.loads(capsys.readouterr().out)
    assert out["split_id"] == split_id and out["note"] == "holdout seen (A13.3)" and out["viewings"] == 0
    assert store.holdout_closure(split_id)["note"] == "holdout seen (A13.3)"


def test_over_the_cap_only_an_exact_repeat_runs_without_an_override(paths, store, dataset, monkeypatch):
    from quant_research.backtest.engine import EngineConfig
    from quant_research.validation import holdout as holdout_mod

    manifest, split = dataset
    reqs = [_req(manifest, split, parameters={"fast": f, "slow": 50}) for f in (5, 8)]
    for r in reqs:
        spec = r.spec.normalized()
        store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
        run_holdout(paths, store, r, max_viewings=2)
    assert run_holdout(paths, store, reqs[0], max_viewings=2)[0].status == "duplicate"
    changed = reqs[0].model_copy(update={"engine_config": EngineConfig(initial_cash=20_000.0)})
    n_exp = len(store.list_experiments(limit=100))
    real_load = holdout_mod.load_holdout_data
    monkeypatch.setattr(holdout_mod, "load_holdout_data", lambda *a: pytest.fail("holdout data loaded"))
    with pytest.raises(HoldoutError, match="already viewed 2 time\\(s\\); the limit is 2"):
        run_holdout(paths, store, changed, max_viewings=2)
    assert len(store.list_experiments(limit=100)) == n_exp
    assert len(store.holdout_runs(split.split_id)) == 2 and not store.holdout_overrides(split.split_id)
    monkeypatch.setattr(holdout_mod, "load_holdout_data", real_load)
    res, _ = run_holdout(paths, store, changed, max_viewings=2, override_reason="cost check")
    assert res.status == "completed" and len(store.holdout_runs(split.split_id)) == 3
    assert [o["reason"] for o in store.holdout_overrides(split.split_id)] == ["cost check"]


def test_a_tighter_mandate_on_the_split_caps_every_holdout_call(paths, store, dataset):
    from tests.conftest import make_mandate

    manifest, split = dataset
    m = make_mandate(dataset)  # max_viewings 1
    store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    reqs = [_req(manifest, split, parameters={"fast": f, "slow": 50}) for f in (5, 8)]
    for r in reqs:
        spec = r.spec.normalized()
        store.freeze_candidate(spec.candidate_hash(), spec.strategy_ref, spec.parameters)
    run_holdout(paths, store, reqs[0], max_viewings=5)
    with pytest.raises(HoldoutError, match="the limit is 1"):
        run_holdout(paths, store, reqs[1], max_viewings=5)


def test_split_viewing_cap_uses_the_tightest_mandate(store, dataset):
    from tests.conftest import make_mandate

    _, split = dataset
    assert split_viewing_cap(store, split.split_id) == 10
    for cap in (4, 2):
        m = make_mandate(dataset, holdout={"max_viewings": cap, "min_primary": 0.0, "min_total_return": 0.0},
                         question=f"cap {cap}")
        store.put_mandate(m.mandate_id(), m.model_dump(mode="json"))
    assert split_viewing_cap(store, split.split_id) == 2


def test_split_viewing_cap_reads_old_mandate_bodies(store, dataset):
    _, split = dataset
    sid = split.split_id
    for i, body in enumerate([{"split_id": sid, "holdout": {"max_viewings": 3}}, {"split_id": sid},
                              {"split_id": sid, "holdout": None}, {"split_id": sid, "holdout": {"max_viewings": "1"}},
                              {"split_id": sid, "holdout": {"max_viewings": True}}, ["not", "a", "mandate"]]):
        store.put_mandate(f"old-{i}", body)
    assert split_viewing_cap(store, sid) == 3


def test_holdout_test_cli_errors_are_one_line(paths, dataset):
    from quant_research.cli import main

    manifest, split = dataset
    home = ["--home", str(paths.root)]
    with pytest.raises(SystemExit, match="candidate is not frozen"):
        main([*home, "holdout-test", "--dataset", manifest["dataset_id"], "--split", split.split_id,
              "--template", "sma_cross", "--params", '{"fast": 10, "slow": 50}'])
    with pytest.raises(SystemExit, match="only for the --dataset/--split path"):
        main([*home, "holdout-test", "--candidate", "cand-x", "--override-reason", "why"])


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


def test_paper_replay_stops_at_the_protected_holdout(paths, dataset, capsys):
    import json

    from quant_research.cli import main
    from quant_research.timeutil import parse_time, to_iso
    from quant_research.validation.splits import holdout_cutoff

    manifest, split = dataset
    assert holdout_cutoff(paths, "BTC-USD", manifest["granularity"]) == split.holdout[0]
    assert holdout_cutoff(paths, "ETH-USD", manifest["granularity"]) is None
    main(["--home", str(paths.root), "paper", "replay", "--dataset", manifest["dataset_id"], "--name", "r",
          "--template", "sma_cross", "--params", '{"fast": 5, "slow": 20}', "--last-bars", "100",
          "--warmup-bars", "50"])
    out = json.loads(capsys.readouterr().out)
    assert out["holdout_cutoff"] == to_iso(split.holdout[0])
    assert parse_time(out["last_bar_open"]) < split.holdout[0]


def test_only_the_holdout_gate_reads_holdout_data():
    """Holdout bars are written by `create_split` and read only by `validation/holdout.py` (`run_holdout`)."""
    from pathlib import Path

    import quant_research

    root = Path(quant_research.__file__).parent
    allowed = {"paths.py", "validation/splits.py", "validation/holdout.py"}
    readers = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                     if ("paths.holdout" in p.read_text() or "full.parquet" in p.read_text())
                     and str(p.relative_to(root)) not in allowed)
    assert readers == []
