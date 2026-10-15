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
