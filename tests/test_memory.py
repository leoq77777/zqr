"""Research memory (contracts v2 appendix A): return archive, residuals, clusters, families, conclusions, archives."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from quant_research.experiments.gatekeeper import GateContext
from quant_research.experiments.runner import ExperimentRequest, run_experiment
from quant_research.memory.returns import ReturnArchive, backfill, daily_returns


def _req(dataset, **kw):
    manifest, split = dataset
    base = {"strategy_ref": "template:sma_cross", "parameters": {"fast": 10, "slow": 50},
            "dataset_id": manifest["dataset_id"], "split_id": split.split_id}
    return ExperimentRequest(**{**base, **kw})


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- daily returns (A1) ----------------------------------------------------------------------------------------

def test_daily_returns_use_the_last_bar_of_each_utc_day():
    times = pd.date_range("2021-01-01", periods=50, freq="h", tz="UTC")
    eq = pd.DataFrame({"time": times, "equity": 10_001.0 + np.arange(50)})
    r = daily_returns(eq, 10_000.0)
    assert list(r.index.strftime("%Y-%m-%d")) == ["2021-01-01", "2021-01-02", "2021-01-03"]
    assert r.iloc[0] == pytest.approx(10_024 / 10_000 - 1)
    assert r.iloc[1] == pytest.approx(10_048 / 10_024 - 1)
    assert r.iloc[2] == pytest.approx(10_050 / 10_048 - 1)


def test_the_dev_window_gives_1267_days_with_a_partial_last_day():
    times = pd.to_datetime(1609459200 + 3600 * np.arange(30_402), unit="s", utc=True)
    r = daily_returns(pd.DataFrame({"time": times, "equity": 10_000.0}), 10_000.0)
    assert len(r) == 1267 and str(r.index[-1].date()) == "2024-06-20"
    assert (times.normalize() == times[-1].normalize()).sum() == 18


# -- archive and backfill (A1) ---------------------------------------------------------------------------------

def _trials(paths, store, dataset, tmp_path):
    ok = [run_experiment(paths, store, _req(dataset, parameters={"fast": f, "slow": 60})).experiment_id
          for f in (5, 10, 20)]
    rejected = run_experiment(paths, store, _req(dataset, parameters={"fast": 50, "slow": 10})).experiment_id
    stop = tmp_path / "STOP"
    stop.write_text("")
    blocked = run_experiment(paths, store, _req(dataset, parameters={"fast": 7, "slow": 60}),
                             gate=GateContext(stop_file=stop))
    assert blocked.status == "blocked"
    run_experiment(paths, store, _req(dataset, segment="validation"))
    run_experiment(paths, store, _req(dataset, parameters={"fast": 5, "slow": 60}), kind="sensitivity")
    return ok, rejected


def test_new_dev_trials_are_archived_when_they_finish(paths, store, dataset, tmp_path):
    ok, rejected = _trials(paths, store, dataset, tmp_path)
    split = dataset[1].split_id
    arc = ReturnArchive(paths, store)
    assert list(arc.load(split).columns) == sorted(ok)
    index = {r["experiment_id"]: r for r in arc.index(split)}
    assert set(index) == {*ok, rejected}
    assert index[rejected]["no_returns"] == 1 and index[ok[0]]["no_returns"] == 0
    assert index[ok[0]]["n_days"] == len(arc.load(split))


def test_backfill_rebuilds_the_same_archive_and_is_idempotent(paths, store, dataset, tmp_path):
    ok, rejected = _trials(paths, store, dataset, tmp_path)
    split = dataset[1].split_id
    arc = ReturnArchive(paths, store)
    before, rows = _sha(arc.path(split)), arc.index(split)
    arc.path(split).unlink()
    store.execute("DELETE FROM return_index")
    counts = backfill(paths, store)
    assert counts[split] == {"added": 3, "no_returns": 1, "missing_equity": 0, "present": 0}
    assert _sha(arc.path(split)) == before
    assert [r["experiment_id"] for r in arc.index(split)] == [r["experiment_id"] for r in rows]
    assert backfill(paths, store)[split]["present"] == 4
    assert _sha(arc.path(split)) == before


# -- residuals (A2) --------------------------------------------------------------------------------------------

DAYS = pd.date_range("2021-01-01", periods=1267, freq="D", name="day")


def _market(seed=0):
    return pd.Series(np.random.default_rng(seed).normal(0.001, 0.035, len(DAYS)), index=DAYS)


def test_market_returns_are_close_to_close_on_the_day_grid():
    from quant_research.memory.residuals import market_returns

    t = pd.date_range("2021-01-01", periods=48, freq="h", tz="UTC")
    bars = pd.DataFrame({"bar_open_time": t, "open": 100.0 + np.arange(48), "close": 101.0 + np.arange(48)})
    m = market_returns(bars, (int(t[0].timestamp()), int(t[-1].timestamp()) + 3600))
    assert m.iloc[0] == pytest.approx(124 / 100 - 1) and m.iloc[1] == pytest.approx(148 / 124 - 1)


def test_residuals_remove_the_market_component():
    from quant_research.memory.residuals import residuals

    m = _market()
    noise = np.random.default_rng(1).normal(0, 0.01, len(DAYS))
    resid, flags = residuals(pd.DataFrame({"a": 0.0002 + 0.6 * m + noise}), m, winsorise=False)
    assert flags.loc["a", "beta"] == pytest.approx(0.6, abs=0.02)
    assert abs(np.corrcoef(resid["a"], m)[0, 1]) < 1e-9


def test_winsorising_clips_at_three_standard_deviations_once():
    from quant_research.memory.residuals import residuals

    m = _market()
    r = np.random.default_rng(2).normal(0, 0.01, len(DAYS))
    r[10] = 0.5
    raw, _ = residuals(pd.DataFrame({"a": r}, index=DAYS), m, winsorise=False)
    w, flags = residuals(pd.DataFrame({"a": r}, index=DAYS), m)
    eps = raw["a"]
    assert w["a"].max() == pytest.approx(eps.mean() + 3 * eps.std(ddof=1))
    assert (w["a"] == eps).sum() == len(DAYS) - 1


def test_flat_and_passive_series_are_flagged():
    from quant_research.memory.residuals import residuals

    m = _market()
    trend = np.where(np.random.default_rng(3).random(len(DAYS)) > 0.5, m, 0.0)
    _, flags = residuals(pd.DataFrame({"cash": 0.0, "hold": m, "trend": trend}), m)
    assert flags["flat"].tolist() == [True, False, False]
    assert flags["passive"].tolist() == [False, True, False]


# -- clustering and K (A3) -------------------------------------------------------------------------------------

def _planted(seed=0):
    """3 groups of 10 trials (residual correlation ~0.8 within, ~0 across), 5 independent, 1 flat, 1 passive."""
    rng = np.random.default_rng(seed + 100)  # not the market's stream, or group 0's factor would be the market
    m = _market(seed)
    cols = {}
    for g in range(3):
        f = rng.normal(0, 0.01, len(DAYS))
        for j in range(10):
            cols[f"exp-g{g}-{j:02d}"] = 0.5 * m + f + rng.normal(0, 0.005, len(DAYS))
    for j in range(5):
        cols[f"exp-solo-{j}"] = 0.3 * m + rng.normal(0, 0.01, len(DAYS))
    cols["exp-zflat"] = np.zeros(len(DAYS))
    cols["exp-zhold"] = m.to_numpy()
    return pd.DataFrame(cols, index=DAYS), m


def test_planted_clusters_are_recovered_and_k_counts_special_trials_once():
    from quant_research.memory.clusters import cluster
    from quant_research.memory.residuals import residuals

    returns, m = _planted()
    labels, k = cluster(*residuals(returns, m), no_returns=["exp-failed-1", "exp-failed-2"])
    assert k == {"0.3": 3 + 5 + 1 + 1 + 2, "0.5": 12, "0.7": 12}
    lab = labels["0.7"]
    assert {lab[f"exp-g1-{j:02d}"] for j in range(10)} == {"exp-g1-00"}
    assert lab["exp-zflat"] == "flat" and lab["exp-zhold"] == "passive" and lab["exp-failed-1"] == "exp-failed-1"


def test_labels_are_canonical_and_recomputation_is_identical():
    from quant_research.memory.clusters import cluster, input_hash
    from quant_research.memory.residuals import residuals

    returns, m = _planted()
    shuffled = returns[list(np.random.default_rng(9).permutation(returns.columns))]
    assert cluster(*residuals(returns, m), []) == cluster(*residuals(shuffled, m), [])
    assert input_hash(returns, [], True) == input_hash(shuffled, [], True)


def test_clustering_is_recorded_once_per_input(paths, store, dataset, tmp_path):
    from quant_research.memory import clusters

    ok, rejected = _trials(paths, store, dataset, tmp_path)
    split = dataset[1].split_id
    c = clusters.compute(paths, store, split)
    assert c.counts["trials"] == 4 and c.counts["no_returns"] == 1
    cid = clusters.record(store, c)
    assert clusters.record(store, clusters.compute(paths, store, split)) == cid
    assert clusters.effective_k(store, split) == c.k["0.7"] >= 2
    assert set(clusters.members(store, cid, 0.7)) == {*ok, rejected}
